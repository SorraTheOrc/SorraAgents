"""
Lightweight CLI and helpers to extend the audit skill to accept a Worklog id or a GitHub PR reference.
This is a scaffold implementation covering:
- parsing input (wl id vs PR ref)
- resolving a WL id from PR title/body text
- fetching PR metadata via `gh` if available (falling back to a simple URL parse)
- preparing an ephemeral git worktree checkout (dry-run in this scaffold)
- selecting canonical build/test commands heuristically

Unit tests should mock subprocess/gh interactions.

This scaffold intentionally does not perform destructive actions (no real checkout/merge) unless explicitly invoked by higher-level code with proper confirmation.
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
    This function is a best-effort helper and will return None if `gh` is unavailable or the call fails.
    """
    try:
        out = subprocess.check_output([
            'gh', 'api', f"repos/{owner}/{repo}/pulls/{number}", '--jq', '{name: .title, body: .body, head: .head.ref}'
        ], stderr=subprocess.DEVNULL, text=True)
        # gh --jq might not produce JSON in older versions; attempt fallback to `gh api --silent` and parse
        try:
            data = json.loads(out)
            title = data.get('name', '')
            body = data.get('body', '')
            head = data.get('head', '')
        except Exception:
            # fallback parsing: gh --jq returned a jq-like dict string
            # simple recovery: try to run without --jq
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


def prepare_worktree_checkout(owner: str, repo: str, pr_number: int, head_ref: str) -> str:
    """Create an ephemeral worktree path for the PR. Returns path.
    This scaffold only returns a proposed path and does not perform the checkout by default.
    Callers should implement concrete checkout under proper guard.
    """
    tmpdir = os.path.abspath(os.path.join('.opencode', 'tmp', f'pr-{owner}-{repo}-{pr_number}'))
    return tmpdir


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('target', help='Worklog id (WL-...) or GitHub PR (URL or owner/repo#pr)')
    p.add_argument('--dry-run', action='store_true', help='Do not perform destructive actions')
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

        worktree = prepare_worktree_checkout(owner, repo, number, prinfo.head_ref)
        print(f"Ephemeral worktree path: {worktree}")

        if not args.dry_run:
            print("Dry-run disabled: in a full implementation we would now fetch the PR, create a worktree, run build/tests and the audit.")

        return 0
    else:
        print("Could not fetch PR metadata via gh. Operator intervention required.")
        return 3


if __name__ == '__main__':
    sys.exit(main())
