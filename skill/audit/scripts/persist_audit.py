#!/usr/bin/env python3
"""Persist an audit report to a Worklog work item using the `wl audit-set` CLI.

Usage:
  persist_audit.py --issue-id <id>            # read report from stdin (if piped)
  persist_audit.py --issue-id <id> --report "<text>"
  persist_audit.py --issue-id <id> --file path/to/file

The script calls:
  wl audit-set <issue-id> --ready-to-close <yes|no> --summary <text> --raw-output "<report>" --json

Cwd-independence (SA-0MSKQERKH002IBLG): every ``wl`` command built here
resolves ``--worklog-dir`` exactly like the audit runner's READ path —
explicit ``--worklog-dir`` > prefix-to-sibling scan > cwd-chain fallback >
no flag — reusing the shared ``skill.shared.status_lifecycle`` helpers so
the PERSIST path targets the work item's own worklog store regardless of
the caller's cwd (e.g. the skill install directory).

Returns non-zero on failure.
"""  # noqa: EXE001
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# Ensure the repo root is on sys.path so the shared status_lifecycle module
# is importable when this script is executed directly from any cwd.
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

from import_guard import guard_shared_import

try:
    from shared.status_lifecycle import resolve_worklog_flags
except ModuleNotFoundError as _missing_shared:
    guard_shared_import(_missing_shared.name)


def _build_fallback_text(issue_id: str, ready: str) -> str:
    """Compact markdown fallback persisted when the full report is rejected.

    Starts with the wl-required ``Ready to close:`` line, names *issue_id*
    (so the identity and readback guards still pass) and carries a clear
    failure notice explaining that the assembled verdict content was rejected.
    """
    return (
        f"Ready to close: {ready}\n"
        "\n"
        "# Audit persistence notice\n"
        "\n"
        f"**Work item:** {issue_id}\n"
        "\n"
        "The audit pipeline completed, but the final persistence step "
        "rejected the assembled verdict content (malformed JSON / validation "
        "error). The full report could not be stored in the audit text field. "
        "The complete report is preserved in the audit raw output "
        "(`wl audit-show`). A re-audit may be required if the verdict data "
        "is needed in this field."
    )


def _find_json_fragment_end(line: str, start: int, content_end: int) -> tuple[int, bool]:
    """Return ``(end_exclusive, is_valid)`` for a JSON fragment at *start*.

    Scans *line* from *start* (which must be ``{`` or ``[``) to
    *content_end* (the line content, excluding the trailing newline).
    ``is_valid`` is True when the fragment parses as JSON; when the fragment
    is unbalanced or malformed, *end_exclusive* is the balanced-close
    position (or *content_end* when unterminated).
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, content_end):
        ch = line[i]
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
            if depth == 0:
                candidate = line[start:i + 1]
                try:
                    json.loads(candidate)
                    return i + 1, True
                except json.JSONDecodeError:
                    return i + 1, False
    candidate = line[start:content_end]
    try:
        json.loads(candidate)
        return content_end, True
    except json.JSONDecodeError:
        return content_end, False


def _salvage_json_prefix(fragment: str) -> str | None:
    """Extract the longest valid JSON prefix of a broken JSON fragment.

    Truncates *fragment* at each position outside string literals, longest
    first, and returns the first prefix (optionally completed with the
    fragment's own missing closing bracket) that parses as JSON. Returns
    None when nothing parses — conservative: never fabricates verdicts.
    """
    if not fragment or fragment[0] not in "{[":
        return None
    closer = "}" if fragment[0] == "{" else "]"

    # Forward pass: record string spans so cuts only happen outside strings.
    string_ranges: list[tuple[int, int]] = []
    in_string = False
    escape = False
    start = -1
    for i, ch in enumerate(fragment):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                string_ranges.append((start, i))
            continue
        if ch == '"':
            in_string = True
            start = i
    if in_string:
        string_ranges.append((start, len(fragment)))

    def _inside_string(pos: int) -> bool:
        return any(a <= pos <= b for a, b in string_ranges)

    for end in range(len(fragment) - 1, 0, -1):
        if _inside_string(end):
            continue
        prefix = fragment[:end]
        for candidate in (prefix + closer, prefix):
            try:
                parsed = json.loads(candidate)
                return json.dumps(parsed)
            except json.JSONDecodeError:
                continue
    return None


def _repair_json_fragment_in_line(line: str) -> tuple[str, bool]:
    """Salvage broken JSON fragments in a single report line.

    Conservative: only fragments that fail to parse as JSON are touched;
    valid JSON and prose without JSON-like fragments pass through unchanged.
    Returns ``(line, changed)``.
    """
    content_end = len(line)
    if line.endswith("\n"):
        content_end -= 1
    if content_end > 0 and line[content_end - 1] == "\r":
        content_end -= 1

    pieces: list[str] = []
    cursor = 0
    changed = False
    i = 0
    while i < content_end:
        ch = line[i]
        if ch == '"':
            # Skip string contents (brackets inside strings are not JSON
            # structure).
            j = i + 1
            while j < content_end:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == '"':
                    break
                j += 1
            i = j + 1
            continue
        if ch in "{[":
            rest = line[i + 1:content_end].lstrip()
            if rest and rest[0] in ('"', "{", "[", "]", "}",
                                     "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "-"):
                end, valid = _find_json_fragment_end(line, i, content_end)
                if not valid:
                    salvaged = _salvage_json_prefix(line[i:end])
                    pieces.append(line[cursor:i])
                    if salvaged is not None:
                        pieces.append(salvaged + " \u2026(truncated malformed JSON)")
                    else:
                        pieces.append("`(unparseable verdict data)`")
                    cursor = end
                    changed = True
                    i = end
                    continue
                i = end
                continue
        i += 1
    if not changed:
        return line, False
    pieces.append(line[cursor:])
    return "".join(pieces), True


def _salvage_report_text(report_text: str, issue_id: str) -> tuple[str, bool]:
    """Repair pass: salvage broken JSON fragments from the report.

    On the final persistence path, when ``wl update --audit-text`` rejects
    the assembled verdict content (malformed JSON), this pass extracts valid
    JSON prefixes from broken fragments and appends a clear failure notice,
    so a repaired (usable) report can be retried once. Per-AC rows already
    parsed are preserved verbatim; verdicts are never fabricated.

    Returns ``(repaired_text, changed)``; ``changed`` is False when the
    report contained no broken JSON.
    """
    repaired_lines: list[str] = []
    changed = False
    for line in report_text.splitlines(keepends=True):
        repaired, line_changed = _repair_json_fragment_in_line(line)
        repaired_lines.append(repaired)
        changed = changed or line_changed
    if not changed:
        return report_text, False
    notice = (
        "\n\n> **Audit persistence notice:** the assembled audit report "
        "contained malformed JSON (verdict data) that failed validation "
        f"during persistence for {issue_id}. The affected fragment(s) were "
        "salvaged \u2014 valid JSON prefixes were extracted and preserved. "
        "Per-AC verdict rows above were not modified. The complete model "
        "output remains available in the audit raw output."
    )
    return "".join(repaired_lines).rstrip("\n") + notice + "\n", True


def _extract_ready_to_close(report_text: str) -> bool:
    """Extract the Ready to close: Yes/No value from the report text."""
    for line in report_text.splitlines():
        if line.strip().lower().startswith("ready to close:"):
            return "yes" in line.lower()
    return False


# Work-item id pattern: 2-5 uppercase letters, dash, then base62-ish suffix
# (e.g. ``SA-0MSAS108O009DYKT``, ``OSL-0MSABC7SB001NVUN``).
_WORK_ITEM_ID_RE = r"\b[A-Z]{2,5}-[A-Z0-9]{12,20}\b"

# Sentinel return code: the final ``wl update --audit-text`` step rejected the
# assembled verdict content (malformed JSON), and only the compact fallback
# markdown notice could be persisted. The audit is usable (identity/readback
# guards pass) but degraded; the audit runner uses this signal to trigger a
# bounded model re-ask (SA-0MSF3RXUB000NLOI).
PERSIST_CONTENT_INVALID = 4


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


def _worklog_flags(cmd: Sequence[str], worklog_dir: str | None) -> list[str]:
    """Resolve ``--worklog-dir`` flags for a wl command built by this module.

    Mirrors the audit runner's READ path (SA-0MSG48MEI0083K82): resolution
    order is explicit ``worklog_dir`` > prefix-to-sibling scan (the work-item
    id prefix is matched against ``SIBLING_SCAN_ROOT/*/.worklog/config.yaml``)
    > cwd-chain fallback > no flag. The shared resolution is reused — no scan
    logic is duplicated here (SA-0MSKQERKH002IBLG). Returns an empty list
    when no worklog directory is resolvable (wl resolves from cwd).
    """
    return resolve_worklog_flags(list(cmd), explicit_dir=worklog_dir)


def _maybe_lower_priority(issue_id: str, wl_bin: str, runner: Callable,
                        worklog_dir: str | None, ready_to_close: bool) -> None:
    """Best-effort: lower a critical work item's priority to high.

    When *ready_to_close* is True and the work item currently carries
    ``critical`` priority, issue ``wl update <id> --priority high --json``
    so the item leaves the critical queue once the audit says it is ready
    to close (SA-0MSBRMXS800625RR).

    The adjustment is strictly best-effort: any failure (unparseable wl
    output, non-zero exit, unexpected JSON shape) logs a warning to stderr
    and returns without blocking audit persistence.
    """
    if not ready_to_close:
        return

    try:
        fetch_cmd = [wl_bin, "show", issue_id, "--json"]
        fetch_cmd[1:1] = _worklog_flags(fetch_cmd, worklog_dir)
        fetch_proc = runner(fetch_cmd, check=False, text=True, capture_output=True)
        if getattr(fetch_proc, "returncode", 1) != 0:
            stderr = getattr(fetch_proc, "stderr", "") or ""
            print(
                f"wl show failed while checking priority (rc={getattr(fetch_proc, 'returncode', 'unknown')}): "
                f"{stderr.strip()}",
                file=sys.stderr,
            )
            return
        data = json.loads(getattr(fetch_proc, "stdout", "") or "{}")
        wi = data.get("workItem", {}) if isinstance(data, dict) else {}
        priority = (wi.get("priority") or "").strip().lower()
    except (json.JSONDecodeError, KeyError, TypeError):
        print("Failed to read priority from wl show output; skipping priority adjustment", file=sys.stderr)
        return

    if priority != "critical":
        return

    update_cmd = [wl_bin, "update", issue_id, "--priority", "high", "--json"]
    update_cmd[1:1] = _worklog_flags(update_cmd, worklog_dir)
    try:
        update_proc = runner(update_cmd, check=False, text=True, capture_output=True)
    except Exception as exc:  # noqa: BLE001 - best-effort; never block persistence
        print(f"wl update --priority high failed: {exc}", file=sys.stderr)
        return
    if getattr(update_proc, "returncode", 1) != 0:
        stderr = getattr(update_proc, "stderr", "") or ""
        print(
            f"wl update --priority high failed (rc={getattr(update_proc, 'returncode', 'unknown')}): "
            f"{stderr.strip()}",
            file=sys.stderr,
        )


def persist_audit(issue_id: str, report_text: str, wl_bin: str = "wl",
                  runner: Callable = None, _fail: bool = False,  # noqa: RUF013
                  worklog_dir: str | None = None) -> int:
    """Persist the given report_text to the work item using wl audit-set.

    Return codes:

    - ``0`` — a usable audit was persisted (the original report, or a
      repaired report after the resilience pass salvaged malformed JSON).
    - ``PERSIST_CONTENT_INVALID`` (4) — the final ``wl update --audit-text``
      step rejected the assembled verdict content (malformed JSON) and only
      the compact fallback markdown notice (with a clear failure notice and
      the target work-item ID) could be persisted. The audit is usable and
      the identity/readback guards pass; the caller may trigger a bounded
      model re-ask (SA-0MSF3RXUB000NLOI).
    - non-zero — nothing could be persisted (e.g. ``wl audit-set`` failed,
      or every ``--audit-text`` attempt including the fallback failed).

    * _fail (internal/testing only): when True, skip the wl call, print the
      report to stdout as a fallback, and return 1 to simulate a persistence
      failure.  This allows tests to verify the fallback behaviour of
      callers (e.g. audit_runner.py).
    * worklog_dir: optional explicit ``--worklog-dir`` value injected into
      every wl command (highest precedence). When None, every wl command
      resolves the worklog store itself via the shared prefix-to-sibling
      scan / cwd-chain fallback (:func:`_worklog_flags`) — so the persist
      path is cwd-independent exactly like the runner's READ path
      (SA-0MSKQERKH002IBLG), for the runner, child audits, the re-ask
      path, and the standalone CLI alike.

    The report is checked against *issue_id* before persisting (identity
    guard): a report that clearly references a different work item (one
    that names other work-item IDs but not the target) is rejected with a
    clear error and a non-zero exit code.  A report naming no work-item ID
    is accepted with a warning (conservative).

    When the report says 'Ready to close: Yes', a work item carrying
    ``critical`` priority is first lowered to ``high`` (best-effort) so it
    leaves the critical queue before the audit is persisted (SA-0MSBRMXS800625RR).
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

    # Lower critical → high priority before persisting when the audit
    # verdict is 'Ready to close: Yes' (best-effort; AC1-AC5).
    _maybe_lower_priority(issue_id, wl_bin, runner, worklog_dir, ready == "yes")

    # Build the command as an argv list to avoid shell quoting pitfalls.
    cmd = [
        wl_bin, "audit-set", issue_id,
        "--ready-to-close", ready,
        "--summary", summary,
        "--raw-output", report_text,
        "--json"
    ]
    cmd[1:1] = _worklog_flags(cmd, worklog_dir)

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
    # CRITICAL: Do NOT pass --stage on this update call.  The verdict-driven
    # status transition (completed/in_review on 'Ready to close: Yes',
    # open/plan_complete on 'No') is applied by the audit runner's
    # _apply_terminal_lifecycle after persistence completes — see
    # skill/audit/SKILL.md "Status Lifecycle".  Passing --stage causes wl
    # update() to bump updatedAt on the work-item row.  If that bump pushes
    # updatedAt past auditedAt + AUDIT_FRESHNESS_BUFFER (60 s), freshness
    # checks (_audit_time_is_fresh / isAuditFresh) return false, causing
    # passed audits to show a stale icon (SA-0MTHC710X003ORZM).

    def _run_audit_text_update(text: str):
        cmd = [wl_bin, "update", issue_id, "--audit-text", text]
        cmd[1:1] = _worklog_flags(cmd, worklog_dir)
        cmd.append("--json")
        return runner(cmd, check=False, text=True, capture_output=True)

    update_proc = _run_audit_text_update(report_text)
    if getattr(update_proc, "returncode", 1) != 0:
        stderr = getattr(update_proc, "stderr", "") or ""
        print(
            f"wl update --audit-text failed (rc={getattr(update_proc, 'returncode', 'unknown')}): "
            f"{stderr.strip()}",
            file=sys.stderr
        )
        # ── Resilience repair pass (SA-0MSF3RXUB000NLOI, P8) ──
        # The final `wl update --audit-text` step is the last write of the
        # run. A failure here (e.g. wl rejecting the assembled verdict JSON)
        # must not lose the completed audit: repair the report (salvage
        # broken JSON fragments, zero model calls) and retry once, then fall
        # back to a compact markdown notice so the audit text field never
        # stays the 43-char stub.
        repaired, changed = _salvage_report_text(report_text, issue_id)
        if changed:
            repair_proc = _run_audit_text_update(repaired)
            if getattr(repair_proc, "returncode", 1) == 0:
                print(
                    "wl update --audit-text succeeded after salvaging "
                    "malformed JSON from the report.",
                    file=sys.stderr,
                )
                return 0
            rerr = getattr(repair_proc, "stderr", "") or ""
            print(
                f"wl update --audit-text (repaired) failed (rc={getattr(repair_proc, 'returncode', 'unknown')}): "
                f"{rerr.strip()}",
                file=sys.stderr,
            )

        # Fallback: persist a compact markdown notice so the audit text field
        # carries usable content (with a clear failure notice and the target
        # work-item ID so the identity/readback guards still pass) instead of
        # the 43-char stub. The audit runner maps PERSIST_CONTENT_INVALID to
        # a bounded model re-ask (≤1 additional call).
        fallback = _build_fallback_text(issue_id, "Yes" if ready == "yes" else "No")
        fb_proc = _run_audit_text_update(fallback)
        if getattr(fb_proc, "returncode", 1) != 0:
            ferr = getattr(fb_proc, "stderr", "") or ""
            print(
                f"wl update --audit-text (fallback) failed (rc={getattr(fb_proc, 'returncode', 'unknown')}): "
                f"{ferr.strip()}",
                file=sys.stderr,
            )
            return int(getattr(update_proc, "returncode", 1) or 1)
        print(
            f"Persisted fallback audit notice for {issue_id} after the "
            "assembled verdict content was rejected.",
            file=sys.stderr,
        )
        return PERSIST_CONTENT_INVALID

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Persist an audit report to a Worklog work item using wl")
    p.add_argument("--issue-id", "-i", required=True, help="Worklog issue id to persist the audit to")
    p.add_argument("--report", "-r", help="Direct audit report text (if not provided, read from stdin or --file)")
    p.add_argument("--file", "-f", type=Path, help="Path to a file containing the audit report")
    p.add_argument("--wl-bin", default="wl", help="Path to the wl CLI (default: wl)")
    p.add_argument(
        "--worklog-dir", default=None,
        help="Explicit worklog directory (highest precedence). When omitted, "
             "the worklog store is auto-resolved from the work-item id prefix "
             "(prefix-to-sibling scan) or the cwd-chain fallback.",
    )
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

    rc = persist_audit(
        args.issue_id, report_text,
        wl_bin=args.wl_bin, worklog_dir=args.worklog_dir,
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
