"""
Lightweight CLI and helpers to extend the audit skill to accept a Worklog id or a GitHub PR reference.
This module adds:
- parsing input (wl id vs PR ref)
- resolving a WL id from PR title/body text
- fetching PR metadata via `gh` if available
- preparing an ephemeral git checkout (clone+checkout) for PR heads
- selecting canonical build/test commands heuristically and running them in isolation

The implementation is conservative: destructive actions (network, clone, checkout, command runs)
are skipped when `dry_run=True`. Unit tests should mock subprocess/gh interactions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

WL_ID_RE = re.compile(r"\b([A-Z]+-[0-9A-Z]+)\b")
PR_URL_RE = re.compile(r"https?://github.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
OWNER_REPO_REF = re.compile(r"(?P<owner>[^/]+)/(?P<repo>[^#]+)#(?P<number>\d+)")


@dataclass
class PRInfo:
    owner: str
    repo: str
    number: int
    title: str = ""
    body: str = ""
    head_ref: str = ""


def parse_input_ref(ref: str) -> Optional[Tuple[str, str, int]]:
    """Parse a GitHub PR ref from a string. Returns (owner, repo, number) or None."""
    m = PR_URL_RE.search(ref)
    if m:
        return m.group('owner'), m.group('repo'), int(m.group('number'))
    m = OWNER_REPO_REF.search(ref)
    if m:
        return m.group('owner'), m.group('repo'), int(m.group('number'))
    return None


def extract_wl_id(text: str) -> Optional[str]:
    """Return first WL id-like token found in text."""
    if not text:
        return None
    m = WL_ID_RE.search(text)
    if m:
        return m.group(1)
    return None


def gh_get_pr(owner: str, repo: str, number: int) -> Optional[PRInfo]:
    """Fetch PR metadata using gh api; returns PRInfo or None on failure.
    Best-effort: returns None if gh is unavailable or call fails.
    """
    try:
        out = subprocess.check_output([
            'gh', 'api', f"repos/{owner}/{repo}/pulls/{number}", '--jq', '{name: .title, body: .body, head: .head.ref}'
        ], stderr=subprocess.DEVNULL, text=True)
        try:
            data = json.loads(out)
            title = data.get('name', '')
            body = data.get('body', '')
            head = data.get('head', '')
        except Exception:
            raw = subprocess.check_output(['gh', 'api', f"repos/{owner}/{repo}/pulls/{number}"], text=True)
            data = json.loads(raw)
            title = data.get('title', '')
            body = data.get('body', '')
            head = data.get('head', {}).get('ref', '')
        return PRInfo(owner=owner, repo=repo, number=number, title=title, body=body, head_ref=head)
    except Exception:
        return None


def detect_build_command(path: str = '.') -> Optional[str]:
    """Heuristic detection of build/test command for repository at path."""
    if os.path.exists(os.path.join(path, 'package.json')):
        return 'npm test'
    if os.path.exists(os.path.join(path, 'pyproject.toml')) or os.path.exists(os.path.join(path, 'requirements.txt')):
        return 'pytest'
    if os.path.exists(os.path.join(path, 'Makefile')):
        return 'make test'
    return None


def create_ephemeral_checkout(owner: str, repo: str, pr_number: int, head_ref: Optional[str] = None, dry_run: bool = True) -> str:
    """Create an ephemeral checkout for the PR head and return path.

    Implementation notes:
    - If dry_run is True the function returns the proposed path without network ops.
    - If dry_run is False the function attempts a minimal clone into a tempdir and checks out the PR head.
    - The function is conservative and cleans up partially created dirs on error.
    """
    base = os.path.abspath(os.path.join('.pi', 'tmp'))
    os.makedirs(base, exist_ok=True)
    dest = os.path.join(base, f'pr-{owner}-{repo}-{pr_number}')

    if dry_run:
        return dest

    # If dest exists, remove it to ensure a clean clone
    if os.path.exists(dest):
        shutil.rmtree(dest)

    try:
        # Clone shallow (no checkout) to save time
        repo_url = f"https://github.com/{owner}/{repo}.git"
        subprocess.check_call(['git', 'clone', '--depth', '1', '--no-checkout', repo_url, dest])
        # Fetch the PR head ref using the pull ref namespace
        # Try GitHub pull refs: refs/pull/NUMBER/head
        subprocess.check_call(['git', 'fetch', 'origin', f'refs/pull/{pr_number}/head:pr-{pr_number}'], cwd=dest)
        # Checkout the fetched ref
        subprocess.check_call(['git', 'checkout', f'pr-{pr_number}'], cwd=dest)
        return dest
    except Exception as e:
        # cleanup
        if os.path.exists(dest):
            shutil.rmtree(dest)
        raise RuntimeError(f"Failed to create ephemeral checkout: {e}")


def run_build_test(path: str, build_cmd: str, timeout: int = 600, dry_run: bool = True) -> Tuple[int, str]:
    """Run the build/test command in path, capture logs, and return (exit_code, log_path).

    In dry_run mode the function returns (0, <proposed-log-path>) without running commands.
    """
    logs_dir = os.path.abspath(os.path.join('.pi', 'tmp', 'logs'))
    os.makedirs(logs_dir, exist_ok=True)
    safe_name = os.path.basename(path).replace('/', '_')
    log_path = os.path.join(logs_dir, f'{safe_name}.log')

    if dry_run:
        with open(log_path, 'w') as f:
            f.write(f'DRY-RUN: would run `{build_cmd}` in {path}\n')
        return 0, log_path

    # Run the command
    try:
        # Use shell=True for convenience with compound commands like "npm test"
        proc = subprocess.run(build_cmd, shell=True, cwd=path, capture_output=True, text=True, timeout=timeout)
        with open(log_path, 'w') as f:
            f.write('STDOUT:\n')
            f.write(proc.stdout or '')
            f.write('\nSTDERR:\n')
            f.write(proc.stderr or '')
        return proc.returncode, log_path
    except subprocess.TimeoutExpired as e:
        with open(log_path, 'w') as f:
            f.write(f'Timeout after {timeout}s\n')
        return 124, log_path
    except Exception as e:
        with open(log_path, 'w') as f:
            f.write(f'Error running build: {e}\n')
        return 2, log_path


def run_audit_in_worktree(path: str, wl_id: str, timeout: int = 600, dry_run: bool = True) -> Tuple[int, str]:
    """Run the audit command against the given worktree and return (exit_code, log_path).

    By convention the audit command is invoked via `pi run "/audit <wl-id>"` and must be run
    with the worktree as cwd so the audit skill can inspect the code. If `pi` is not available
    the function returns an executable-not-found code in the audit log.
    """
    logs_dir = os.path.abspath(os.path.join('.pi', 'tmp', 'logs'))
    os.makedirs(logs_dir, exist_ok=True)
    safe_name = os.path.basename(path).replace('/', '_')
    log_path = os.path.join(logs_dir, f'audit-{safe_name}.log')

    if dry_run:
        with open(log_path, 'w') as f:
            f.write(f'DRY-RUN: would run `pi run "/audit {wl_id}"` in {path}\n')
        return 0, log_path

    cmd = ['pi', 'run', f"/audit {wl_id}"]
    try:
        proc = subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=timeout)
        with open(log_path, 'w') as f:
            f.write('STDOUT:\n')
            f.write(proc.stdout or '')
            f.write('\nSTDERR:\n')
            f.write(proc.stderr or '')
        return proc.returncode, log_path
    except FileNotFoundError:
        # opencode not installed; record error
        with open(log_path, 'w') as f:
            f.write('pi CLI not found in PATH\n')
        return 127, log_path
    except subprocess.TimeoutExpired:
        with open(log_path, 'w') as f:
            f.write(f'Timeout after {timeout}s\n')
        return 124, log_path
    except Exception as e:
        with open(log_path, 'w') as f:
            f.write(f'Error running audit: {e}\n')
        return 2, log_path


def record_audit_text(wl_id: str, audit_text: str, dry_run: bool = True) -> bool:
    """Record the structured audit text on the work item using the `wl` CLI.

    In dry_run mode the function writes a local file under .pi/tmp and returns True.
    """
    if dry_run:
        outpath = os.path.abspath(os.path.join('.pi', 'tmp', f'audit-{wl_id}.txt'))
        os.makedirs(os.path.dirname(outpath), exist_ok=True)
        with open(outpath, 'w') as f:
            f.write(audit_text)
        return True

    try:
        subprocess.check_call(['wl', 'update', wl_id, '--audit-text', audit_text])
        return True
    except Exception:
        return False


def create_wl_from_pr(pr: PRInfo, dry_run: bool = True) -> Optional[str]:
    """Create a WL work item from PR metadata and return the new WL id."""
    title = f"Audit PR flow follow-up: {pr.title or f'PR #{pr.number}'}"
    description = (
        f"Created from GitHub PR {pr.owner}/{pr.repo}#{pr.number}.\n\n"
        f"PR title: {pr.title}\n\n"
        f"PR body:\n{pr.body}\n"
    )

    if dry_run:
        return "SA-DRYRUN"

    try:
        out = subprocess.check_output([
            'wl', 'create', '--title', title, '--description', description,
            '--issue-type', 'task', '--priority', 'medium', '--json'
        ], text=True)
        data = json.loads(out)
        return data.get('workItem', {}).get('id')
    except Exception:
        return None


def resolve_wl_for_pr(pr: PRInfo, explicit_wl: Optional[str] = None, allow_create: bool = False, dry_run: bool = True) -> Tuple[Optional[str], str]:
    """Resolve WL id for a PR using explicit id, title/body extraction, or optional create flow.

    Returns (wl_id, resolution_note).
    """
    if explicit_wl:
        return explicit_wl, 'provided-explicitly'

    wl = extract_wl_id(pr.title) or extract_wl_id(pr.body)
    if wl:
        return wl, 'resolved-from-pr-metadata'

    if allow_create:
        created = create_wl_from_pr(pr, dry_run=dry_run)
        if created:
            return created, 'created-from-pr'
        return None, 'create-failed'

    return None, 'unresolved-needs-user-input'


def gh_get_pr_checks(owner: str, repo: str, number: int) -> Dict[str, Any]:
    """Get PR check/merge readiness summary using gh.

    Returns dict with keys:
      checks_ok (bool|None), merge_state (str), raw (dict)
    """
    try:
        out = subprocess.check_output([
            'gh', 'pr', 'view', str(number), '--repo', f'{owner}/{repo}', '--json',
            'mergeStateStatus,statusCheckRollup'
        ], text=True)
        data = json.loads(out)
        rollup = data.get('statusCheckRollup') or []
        # If no checks are present, return None (unknown)
        if not rollup:
            checks_ok = None
        else:
            conclusions = [
                (item.get('conclusion') or '').upper()
                for item in rollup if isinstance(item, dict)
            ]
            checks_ok = all(c in ('SUCCESS', 'NEUTRAL', 'SKIPPED') for c in conclusions if c)
        return {
            'checks_ok': checks_ok,
            'merge_state': data.get('mergeStateStatus', ''),
            'raw': data,
        }
    except Exception:
        return {'checks_ok': None, 'merge_state': '', 'raw': {}}


def merge_pr(owner: str, repo: str, number: int, dry_run: bool = True) -> bool:
    """Merge PR via gh. Requires explicit confirmation by caller."""
    if dry_run:
        return True
    try:
        subprocess.check_call(['gh', 'pr', 'merge', str(number), '--repo', f'{owner}/{repo}', '--merge'])
        return True
    except Exception:
        return False


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('target', help='Worklog id (WL-...) or GitHub PR (URL or owner/repo#pr)')
    p.add_argument('--dry-run', action='store_true', help='Do not perform destructive actions')
    p.add_argument('--run-checkout', action='store_true', help='Perform ephemeral checkout and build/test (not recommended without --dry-run)')
    p.add_argument('--run-audit', action='store_true', help='Run audit after checkout/build')
    p.add_argument('--wl-id', help='Explicit WL id to use for PR flow')
    p.add_argument('--allow-create-wl', action='store_true', help='Allow creating a WL item when PR metadata has no WL id')
    p.add_argument('--offer-merge', action='store_true', help='Print merge offer when checks pass')
    p.add_argument('--confirm-merge', action='store_true', help='Actually perform merge after offer and checks pass')
    args = p.parse_args(argv)

    tgt = args.target
    # If it's a WL id, run audit against current codebase (existing behavior)
    if WL_ID_RE.fullmatch(tgt):
        print(f"Detected WL id: {tgt}. Running audit against current checkout (not implemented in scaffold).")
        return 0

    pr = parse_input_ref(tgt)
    if not pr:
        print("Input is neither a WL id nor a recognized GitHub PR reference.")
        return 2

    owner, repo, number = pr
    print(f"Resolved PR ref: {owner}/{repo}#{number}")

    prinfo = gh_get_pr(owner, repo, number)
    if prinfo:
        print(f"PR title: {prinfo.title}")
        wl, wl_resolution = resolve_wl_for_pr(
            prinfo,
            explicit_wl=args.wl_id,
            allow_create=args.allow_create_wl,
            dry_run=args.dry_run,
        )
        if wl:
            print(f"WL resolution: {wl_resolution} -> {wl}")
        else:
            print(
                "WL resolution: unresolved-needs-user-input. "
                "Please provide --wl-id or rerun with --allow-create-wl to create one."
            )

        build_cmd = detect_build_command()
        print(f"Suggested build/test command: {build_cmd}")

        worktree = create_ephemeral_checkout(owner, repo, number, prinfo.head_ref, dry_run=args.dry_run or not args.run_checkout)
        print(f"Ephemeral checkout path: {worktree}")

        build_ok = False
        audit_ok = False
        if args.run_checkout:
            cmd = build_cmd or 'echo no-detected-build-cmd'
            rc, log = run_build_test(worktree, cmd, dry_run=args.dry_run)
            print(f"Build/test exit code: {rc}, log: {log}")
            build_ok = (rc == 0)

            if args.run_audit and wl:
                arc, alog = run_audit_in_worktree(worktree, wl, dry_run=args.dry_run)
                print(f"Audit exit code: {arc}, log: {alog}")
                with open(alog) as fh:
                    content = fh.read()
                recorded = record_audit_text(wl, content, dry_run=args.dry_run)
                print(f"Recorded audit to WL: {recorded}")
                audit_ok = (arc == 0 and recorded)
                if not audit_ok:
                    print(
                        "Audit did not pass. Still on PR branch/worktree context. "
                        "Please provide next steps."
                    )

        checks = gh_get_pr_checks(owner, repo, number)
        if args.offer_merge:
            checks_ok = checks.get('checks_ok')
            if build_ok and audit_ok and (checks_ok in (True, None)):
                print(
                    "Code appears ready. Offer: merge this PR into main and push. "
                    "Run with --confirm-merge to proceed."
                )
                if args.confirm_merge:
                    merged = merge_pr(owner, repo, number, dry_run=args.dry_run)
                    print(f"Merge executed: {merged}")
            else:
                print(
                    "Merge offer withheld: build/tests and audit must pass, and checks must be green/unknown."
                )

        if not args.dry_run and not args.run_checkout:
            print("Note: --run-checkout not provided; nothing further executed.")

        return 0
    else:
        print("Could not fetch PR metadata via gh. Operator intervention required.")
        return 3


if __name__ == '__main__':
    sys.exit(main())
