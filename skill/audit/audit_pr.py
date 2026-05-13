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
import tempfile
from dataclasses import dataclass
from typing import Optional, Tuple

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

    By convention the audit command is invoked via `opencode run "/audit <wl-id>"` and must be run
    with the worktree as cwd so the audit skill can inspect the code. If `opencode` is not available
    the function will attempt to fall back to a direct `wl` invocation if appropriate.
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


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('target', help='Worklog id (WL-...) or GitHub PR (URL or owner/repo#pr)')
    p.add_argument('--dry-run', action='store_true', help='Do not perform destructive actions')
    p.add_argument('--run-checkout', action='store_true', help='Perform ephemeral checkout and build/test (not recommended without --dry-run)')
    p.add_argument('--run-audit', action='store_true', help='Run audit after checkout/build')
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
        wl = extract_wl_id(prinfo.title) or extract_wl_id(prinfo.body)
        if wl:
            print(f"Found WL id in PR metadata: {wl}")
        else:
            print("No WL id found in PR title/body. Operator must be prompted (not implemented in scaffold).")

        build_cmd = detect_build_command()
        print(f"Suggested build/test command: {build_cmd}")

        worktree = create_ephemeral_checkout(owner, repo, number, prinfo.head_ref, dry_run=args.dry_run or not args.run_checkout)
        print(f"Ephemeral checkout path: {worktree}")

        if args.run_checkout:
            cmd = build_cmd or 'echo no-detected-build-cmd'
            rc, log = run_build_test(worktree, cmd, dry_run=args.dry_run)
            print(f"Build/test exit code: {rc}, log: {log}")

            if args.run_audit and wl:
                arc, alog = run_audit_in_worktree(worktree, wl, dry_run=args.dry_run)
                print(f"Audit exit code: {arc}, log: {alog}")
                # attempt to record audit text (in a real run the audit skill outputs structured content)
                with open(alog) as fh:
                    content = fh.read()
                recorded = record_audit_text(wl, content, dry_run=args.dry_run)
                print(f"Recorded audit to WL: {recorded}")

        if not args.dry_run and not args.run_checkout:
            print("Note: --run-checkout not provided; nothing further executed.")

        return 0
    else:
        print("Could not fetch PR metadata via gh. Operator intervention required.")
        return 3


if __name__ == '__main__':
    sys.exit(main())
