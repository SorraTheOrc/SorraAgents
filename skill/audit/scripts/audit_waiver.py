#!/usr/bin/env python3
"""Record (and list) producer-accepted false positives for the audit runner.

A medium/low ruff finding the audit classified ``confident-false-positive``
(``candidate false positive — producer decision required``) gets a tracking
chore on every audit run unless the producer accepts it. Accepting it once
records a *waiver* on the audited item; a waived finding then never creates a
chore and never blocks closure or re-arms ``needsProducerReview``
(SA-0MVZNECSD005XB42 AC3).

Storage: the worklog's ``auditWaiver`` column is a single-slot audit-gap
waiver object (``{reason, author, waivedAt}``) and the CLI has no flag for a
per-finding list, so this helper records the waiver as a comment on the
item:

    AUDIT-FP-WAIVER: {"rule": "F401", "file": "src/app.py", "line": 12,
                      "justification": "deliberate re-export", "author": "..."}

Usage:
  audit_waiver.py add <issue-id> --rule F401 --file src/app.py \
      [--line 12] --justification "deliberate re-export" [--author NAME] \
      [--worklog-dir PATH] [--json]
  audit_waiver.py list <issue-id> [--worklog-dir PATH] [--json]

Omit ``--line`` to accept the rule for the whole file (survives line drift).
Both subcommands print JSON with ``--json`` and exit non-zero on failure.
"""  # noqa: EXE001
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# Self-contained bootstrap (F2, SA-0MSWJ9Z02005DP8X): the skills root is
# exactly ``parents[2]``; skill modules import as top-level packages, so a
# copy of the tree works from any depth. Without it the
# ``audit.scripts.audit_runner`` import below fails when this script is run
# directly from another cwd.
SKILLS_ROOT = Path(__file__).resolve().parents[2]
if str(SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLS_ROOT))

from audit.scripts.audit_runner import (
    AUDIT_WAIVER_MARKER,
    _parse_audit_waivers,
    _resolve_worklog_flags,
)

Runner = Callable[..., subprocess.CompletedProcess]


def _run_wl(runner: Runner, cmd: list[str],
            worklog_dir: str | None) -> dict:
    """Run a ``wl`` command and return its parsed JSON payload."""
    full_cmd = list(cmd)
    if full_cmd and full_cmd[0] == "wl":
        full_cmd[1:1] = _resolve_worklog_flags(full_cmd, explicit_dir=worklog_dir)
    proc = runner(full_cmd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed ({' '.join(full_cmd)}): "
            f"{(proc.stderr or '').strip()}"
        )
    try:
        data = json.loads(proc.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON from wl: {exc}") from exc
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            f"Worklog command failed: {data.get('error', 'unknown error')}"
        )
    return data if isinstance(data, dict) else {}


def _default_runner(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)


def waiver_comment(rule: str, file: str, line: int | None,
                   justification: str, author: str = "") -> str:
    """Render the comment body recording one accepted false positive."""
    payload: dict = {"rule": rule, "file": file, "line": line or 0}
    if justification:
        payload["justification"] = justification
    if author:
        payload["author"] = author
    return f"{AUDIT_WAIVER_MARKER} {json.dumps(payload, sort_keys=True)}"


def add_waiver(issue_id: str, *, rule: str, file: str, line: int | None = None,
               justification: str = "", author: str = "",
               worklog_dir: str | None = None,
               runner: Runner | None = None) -> dict:
    """Record an accepted false positive on *issue_id*; returns the waiver."""
    if not rule or not file:
        raise ValueError("a waiver needs both --rule and --file")
    comment = waiver_comment(rule, file, line, justification, author)
    cmd = ["wl", "comment", "add", issue_id, "--comment", comment]
    if author:
        cmd += ["--author", author]
    cmd.append("--json")
    _run_wl(runner or _default_runner, cmd, worklog_dir)
    return {
        "issueId": issue_id,
        "rule": rule,
        "file": file,
        "line": line or 0,
        "justification": justification,
        "author": author,
        "marker": comment,
    }


def list_waivers(issue_id: str, *, worklog_dir: str | None = None,
                 runner: Runner | None = None) -> list[dict]:
    """Every waiver currently recorded on *issue_id*."""
    data = _run_wl(
        runner or _default_runner,
        ["wl", "show", issue_id, "--json"],
        worklog_dir,
    )
    comments = data.get("comments") or []
    return _parse_audit_waivers(comments)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record or list producer-accepted false positives (audit "
            "waivers) on a work item."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="record one accepted false positive")
    add.add_argument("issue_id", help="work item the audit reported the finding on")
    add.add_argument("--rule", required=True, help="linter rule code, e.g. F401")
    add.add_argument("--file", required=True, help="repository-relative file path")
    add.add_argument("--line", type=int, default=0,
                     help="line number (omit to accept the rule for the file)")
    add.add_argument("--justification", default="",
                     help="why the finding is a false positive")
    add.add_argument("--author", default="", help="who accepted it")
    add.add_argument("--worklog-dir", default=None,
                     help="explicit .worklog directory")
    add.add_argument("--json", action="store_true", help="machine-readable output")

    lst = sub.add_parser("list", help="list recorded waivers")
    lst.add_argument("issue_id")
    lst.add_argument("--worklog-dir", default=None)
    lst.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "add":
            waiver = add_waiver(
                args.issue_id, rule=args.rule, file=args.file,
                line=args.line or None,
                justification=args.justification, author=args.author,
                worklog_dir=args.worklog_dir,
            )
            if args.json:
                print(json.dumps({"success": True, "waiver": waiver}, indent=2))
            else:
                print(
                    f"Accepted {waiver['rule']} in {waiver['file']}"
                    f":{waiver['line'] or '*'} on {waiver['issueId']}."
                )
            return 0
        waivers = list_waivers(
            args.issue_id, worklog_dir=args.worklog_dir,
        )
        if args.json:
            print(json.dumps(
                {"success": True, "issueId": args.issue_id, "waivers": waivers},
                indent=2,
            ))
        elif not waivers:
            print(f"No accepted false positives recorded on {args.issue_id}.")
        else:
            for w in waivers:
                print(
                    f"- {w['rule']} in {w['file']}:{w['line'] or '*'}"
                    f" — {w.get('justification') or '(no justification)'}"
                )
        return 0
    except (RuntimeError, ValueError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
