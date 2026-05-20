#!/usr/bin/env python3
"""Persist an audit report to a Worklog work item using the `wl` CLI.

Usage:
  persist_audit.py --issue-id <id>            # read report from stdin (if piped)
  persist_audit.py --issue-id <id> --report "<text>"
  persist_audit.py --issue-id <id> --file path/to/file

The script calls:
  wl update <issue-id> --audit-text "<report>" --json

Returns non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable


def persist_audit(issue_id: str, report_text: str, wl_bin: str = "wl", runner: Callable = None) -> int:
    """Persist the given report_text to the work item using wl update.

    Returns the wl subprocess return code (0 on success).
    """
    if runner is None:
        runner = subprocess.run

    # Build the command as an argv list to avoid shell quoting pitfalls.
    cmd = [wl_bin, "update", issue_id, "--audit-text", report_text, "--json"]

    proc = runner(cmd, check=False, text=True, capture_output=True)

    # If wl returned non-zero, bubble up the failure and print diagnostics.
    if getattr(proc, "returncode", 1) != 0:
        stderr = getattr(proc, "stderr", "") or ""
        print(f"wl update failed (rc={getattr(proc, 'returncode', 'unknown')}): {stderr.strip()}", file=sys.stderr)
        return int(getattr(proc, "returncode", 1) or 1)

    # Try to parse JSON output and detect explicit failures
    stdout = getattr(proc, "stdout", "") or ""
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("error") or data.get("message") or "unknown"
            print(f"wl update reported failure: {err}", file=sys.stderr)
            return 1
    except json.JSONDecodeError:
        # If wl didn't produce JSON, that's tolerated; just proceed.
        pass

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Persist an audit report to a Worklog work item using wl")
    p.add_argument("--issue-id", "-i", required=True, help="Worklog issue id to persist the audit to")
    p.add_argument("--report", "-r", help="Direct audit report text (if not provided, read from stdin or --file)")
    p.add_argument("--file", "-f", type=Path, help="Path to a file containing the audit report")
    p.add_argument("--wl-bin", default="wl", help="Path to the wl CLI (default: wl)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    report_text = ""

    # Priority: --report > --file > stdin (piped)
    if args.report:
        report_text = args.report
    elif args.file:
        try:
            report_text = args.file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Failed to read file {args.file}: {exc}", file=sys.stderr)
            return 2
    else:
        # If stdin is not a tty, read it. Otherwise error.
        if not sys.stdin.isatty():
            report_text = sys.stdin.read()
        else:
            print("No report provided: pass --report or --file or pipe text to stdin", file=sys.stderr)
            return 2

    # Normalize to str and ensure not empty
    if report_text is None:
        report_text = ""
    report_text = str(report_text)

    if not report_text.strip():
        print("Empty report text; nothing to persist", file=sys.stderr)
        return 2

    rc = persist_audit(args.issue_id, report_text, wl_bin=args.wl_bin)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
