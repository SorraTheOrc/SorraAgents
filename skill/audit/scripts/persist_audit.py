#!/usr/bin/env python3
"""Persist an audit report to a Worklog work item using the `wl audit-set` CLI.

Usage:
  persist_audit.py --issue-id <id>            # read report from stdin (if piped)
  persist_audit.py --issue-id <id> --report "<text>"
  persist_audit.py --issue-id <id> --file path/to/file

The script calls:
  wl audit-set <issue-id> --ready-to-close <yes|no> --summary <text> --raw-output "<report>" --json

Returns non-zero on failure.
"""  # noqa: EXE001
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


def _extract_ready_to_close(report_text: str) -> bool:
    """Extract the Ready to close: Yes/No value from the report text."""
    for line in report_text.splitlines():
        if line.strip().lower().startswith("ready to close:"):
            return "yes" in line.lower()
    return False


# Work-item id pattern: 2-5 uppercase letters, dash, then base62-ish suffix
# (e.g. ``SA-0MSAS108O009DYKT``, ``OSL-0MSABC7SB001NVUN``).
_WORK_ITEM_ID_RE = r"\b[A-Z]{2,5}-[A-Z0-9]{12,20}\b"


def extract_work_item_ids(report_text: str) -> list[str]:
    """Extract all work-item IDs mentioned in *report_text*.

    Returns the list of unique IDs in order of first appearance.
    """
    seen: list[str] = []
    for m in re.finditer(_WORK_ITEM_ID_RE, report_text or ""):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


def check_report_identity(issue_id: str, report_text: str) -> tuple[str | None, str | None]:
    """Verify the report text belongs to *issue_id*.

    Returns ``(error, warning)`` where:

    - *error* is set (non-None) when the report must be rejected, or None
      when it may be persisted.
    - *warning* is set when the report should be persisted but carries a
      cautionary note (e.g. it does not name any work item).

    The check is conservative:

    - A report mentioning the target ID is always accepted, even when it
      also mentions other work-item IDs (e.g. child-audit sections that
      reference child IDs while the overall audit targets the parent).
    - A report that mentions one or more work-item IDs but NOT the target
      ID is rejected (clear cross-work-item contamination).
    - A report mentioning no work-item ID at all is accepted but produces a
      warning: the audit should identify the work item it audits, but the
      absence of any ID does not "clearly reference a different work item"
      and must not block persistence (conservative per AC3).
    """
    ids = extract_work_item_ids(report_text)
    if not ids:
        return None, (
            "Warning: report does not reference any work-item ID; "
            f"cannot confirm it belongs to {issue_id}. Persisting anyway "
            "(conservative guard). Ensure report text names the audited "
            "work item."
        )
    if issue_id not in ids:
        mentioned = ", ".join(ids[:5])
        error_msg = (
            f"Error: report references work item(s) {mentioned} but not the "
            f"target {issue_id}; refusing to persist a possibly mismatched report."
        )
        return error_msg, None
    return None, None


def persist_audit(issue_id: str, report_text: str, wl_bin: str = "wl",
                  runner: Callable = None, _fail: bool = False,  # noqa: RUF013
                  worklog_dir: str | None = None) -> int:
    """Persist the given report_text to the work item using wl audit-set.

    Returns the wl subprocess return code (0 on success).

    * _fail (internal/testing only): when True, skip the wl call, print the
      report to stdout as a fallback, and return 1 to simulate a persistence
      failure.  This allows tests to verify the fallback behaviour of
      callers (e.g. audit_runner.py).
    * worklog_dir: optional explicit ``--worklog-dir`` value injected into
      every wl command so the store is targeted regardless of the caller's
      cwd (used by the audit runner; standalone CLI usage is unaffected).

    The report is checked against *issue_id* before persisting (identity
    guard): a report that clearly references a different work item (one
    that names other work-item IDs but not the target) is rejected with a
    clear error and a non-zero exit code.  A report naming no work-item ID
    is accepted with a warning (conservative).
    """
    identity_error, identity_warning = check_report_identity(issue_id, report_text)
    if identity_warning:
        print(identity_warning, file=sys.stderr)
    if identity_error:
        print(identity_error, file=sys.stderr)
        return 3

    if _fail:
        # Simulate failure: print report to stdout (fallback for operator)
        # and return 1.
        print(report_text, end="")
        return 1

    if runner is None:
        runner = subprocess.run

    ready = "yes" if _extract_ready_to_close(report_text) else "no"
    summary = "Audit result persisted via persist_audit.py"

    # Build the command as an argv list to avoid shell quoting pitfalls.
    cmd = [
        wl_bin, "audit-set", issue_id,
        "--ready-to-close", ready,
        "--summary", summary,
        "--raw-output", report_text,
        "--json"
    ]
    if worklog_dir:
        cmd[1:1] = ["--worklog-dir", worklog_dir]

    proc = runner(cmd, check=False, text=True, capture_output=True)

    # If wl returned non-zero, bubble up the failure and print diagnostics.
    if getattr(proc, "returncode", 1) != 0:
        stderr = getattr(proc, "stderr", "") or ""
        print(f"wl audit-set failed (rc={getattr(proc, 'returncode', 'unknown')}): {stderr.strip()}", file=sys.stderr)
        return int(getattr(proc, 'returncode', 1) or 1)

    # Try to parse JSON output and detect explicit failures
    stdout = getattr(proc, "stdout", "") or ""
    try:
        data = json.loads(stdout)
        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("error") or data.get("message") or "unknown"
            print(f"wl audit-set reported failure: {err}", file=sys.stderr)
            return 1
    except json.JSONDecodeError:
        # If wl didn't produce JSON, that's tolerated; just proceed.
        pass

    # ── Step 2: Update the work item's auditResult field (for Pi extension UI) ──
    # wl audit-set stores data in a separate audit store, but the work item's
    # own auditResult field (visible via wl show) must be set via wl update --audit-text
    # so that the Pi extension's audit column shows the green tick / verdict.
    #
    # SAFETY: Fetch the current work item stage and pass it explicitly so that
    # the `wl update` call never accidentally advances the stage. The
    # verdict-driven status transition (completed/in_review on 'Ready to
    # close: Yes', open/plan_complete on 'No') is applied by the audit
    # runner's finally block after persistence completes — see
    # skill/audit/SKILL.md "Status Lifecycle".
    current_stage = ""
    try:
        fetch_cmd = [wl_bin, "show", issue_id, "--json"]
        if worklog_dir:
            fetch_cmd[1:1] = ["--worklog-dir", worklog_dir]
        fetch_proc = runner(fetch_cmd, check=False, text=True, capture_output=True)
        if fetch_proc.returncode == 0:
            fetch_data = json.loads(fetch_proc.stdout)
            wi = fetch_data.get("workItem", {}) if isinstance(fetch_data, dict) else {}
            current_stage = wi.get("stage", "") or ""
    except (json.JSONDecodeError, KeyError, TypeError):
        pass  # Best-effort; audit text persistence must not fail on fetch errors

    update_cmd = [
        wl_bin, "update", issue_id,
        "--audit-text", report_text,
    ]
    if worklog_dir:
        update_cmd[1:1] = ["--worklog-dir", worklog_dir]
    # Explicitly preserve current stage to prevent accidental advancement
    if current_stage:
        update_cmd.extend(["--stage", current_stage])
    update_cmd.append("--json")
    update_proc = runner(update_cmd, check=False, text=True, capture_output=True)

    if getattr(update_proc, "returncode", 1) != 0:
        stderr = getattr(update_proc, "stderr", "") or ""
        print(
            f"wl update --audit-text failed (rc={getattr(update_proc, 'returncode', 'unknown')}): "
            f"{stderr.strip()}",
            file=sys.stderr
        )
        # Non-fatal: audit data was stored via wl audit-set; the work item
        # field is a convenience for the UI. Return 0 from persist_audit
        # if audit-set succeeded.

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
