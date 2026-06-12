"""Tests for the audit persistence fallback and cycle detection.

Verifies that Ralph reads audit state via `wl audit-show` instead of the
deprecated `workItem.audit` field from `wl show`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import (
    RalphLoop,
    RalphError,
    parse_audit_report,
    _build_remediation_prompt,
)


# ──────────────────────────────────────────────────────────
# Helper: a mock subprocess runner that returns canned JSON
# for wl / pi calls, with tracking for calls made.
# ──────────────────────────────────────────────────────────


def _wl_show_response(stage: str = "plan_complete") -> str:
    """Build a wl show JSON response (no longer carries audit)."""
    work_item = {
        "id": "SA-TEST",
        "stage": stage,
        "status": "open",
    }
    return json.dumps({"success": True, "workItem": work_item, "children": []})


def _wl_audit_show_response(audit_text: str | None = None) -> str:
    """Build a wl audit-show JSON response.

    When audit_text is None, returns audit: null (no audit record).
    Otherwise returns the audit_results row with rawOutput set.
    """
    audit = None
    if audit_text is not None:
        audit = {
            "workItemId": "SA-TEST",
            "readyToClose": "Yes" in audit_text,
            "auditedAt": "2026-06-05T12:00:00Z",
            "summary": "Audit result",
            "rawOutput": audit_text,
            "author": "audit-agent",
        }
    return json.dumps({"success": True, "workItemId": "SA-TEST", "audit": audit})


def _wl_update_audit_response() -> str:
    return json.dumps({"success": True})


def _pi_audit_output(report_text: str) -> str:
    """Build a pi JSON streaming output with the given report text."""
    lines = [
        json.dumps({"type": "session", "id": "sess-1"}),
        json.dumps({
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": report_text}],
                }
            ],
            "willRetry": False,
        }),
    ]
    return "\n".join(lines)


# Valid audit report that says "Yes"
_VALID_AUDIT_REPORT = (
    "Ready to close: Yes\n\n"
    "## Summary\n"
    "All criteria met.\n\n"
    "## Acceptance Criteria Status\n"
    "| # | Criterion | Verdict | Evidence |\n"
    "|---|-----------|---------|----------|\n"
    "| 1 | AC-1 | met | src/main.py:10 |\n"
)

# Valid audit report that says "No" (genuine gaps)
_GAP_AUDIT_REPORT = (
    "Ready to close: No\n\n"
    "## Summary\n"
    "Criterion 1 is unmet.\n\n"
    "## Acceptance Criteria Status\n"
    "| # | Criterion | Verdict | Evidence |\n"
    "|---|-----------|---------|----------|\n"
    "| 1 | AC-1 | unmet | src/main.py:10 |\n"
)

# Output without any "Ready to close:" marker
_INVALID_AUDIT_OUTPUT = (
    "I checked the code and everything is fine.\n"
    "No issues found."
)


# ──────────────────────────────────────────────────────────
# Test: parse_audit_report utility
# ──────────────────────────────────────────────────────────


def test_parse_audit_report_yes():
    result = parse_audit_report(_VALID_AUDIT_REPORT)
    assert result.ready_to_close is True
    assert len(result.criteria) == 1
    assert result.criteria[0].verdict == "met"


def test_parse_audit_report_no():
    result = parse_audit_report(_GAP_AUDIT_REPORT)
    assert result.ready_to_close is False
    assert len(result.criteria) == 1
    assert result.criteria[0].verdict == "unmet"


def test_parse_audit_report_adjusted():
    """parse_audit_report should recognize 'adjusted' verdict."""
    adjusted_report = (
        "Ready to close: Yes\n\n"
        "## Summary\n"
        "All criteria acceptable with adjustments.\n\n"
        "## Acceptance Criteria Status\n"
        "| # | Criterion | Verdict | Evidence |\n"
        "|---|-----------|---------|----------|\n"
        "| 1 | AC-1 | adjusted | src/main.py:10 — adjusted for performance |\n"
    )
    result = parse_audit_report(adjusted_report)
    assert result.ready_to_close is True
    assert len(result.criteria) == 1
    assert result.criteria[0].verdict == "adjusted"


def test_parse_audit_report_adjusted_does_not_trigger_unmet():
    """Criteria with 'adjusted' verdict should not appear in unmet_or_partial."""
    adjusted_report = (
        "Ready to close: Yes\n\n"
        "## Summary\n"
        "All good.\n\n"
        "## Acceptance Criteria Status\n"
        "| # | Criterion | Verdict | Evidence |\n"
        "|---|-----------|---------|----------|\n"
        "| 1 | AC-1 | adjusted | src/main.py:10 — reason |\n"
    )
    result = parse_audit_report(adjusted_report)
    assert len(result.unmet_or_partial) == 0


# ──────────────────────────────────────────────────────────
# AC1: _read_persisted_audit_text uses wl audit-show
# ──────────────────────────────────────────────────────────


def test_read_persisted_audit_text_uses_audit_show():
    """Verify that _read_persisted_audit_text calls wl audit-show, not wl show."""
    audit_show_called = []
    wl_show_called = []

    def runner(cmd, **kwargs):
        argv = list(cmd)
        if argv[:3] == ["wl", "audit-show", "SA-TEST"]:
            audit_show_called.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(_VALID_AUDIT_REPORT))
        if argv[:2] == ["wl", "show"]:
            wl_show_called.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_show_response())
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    loop = RalphLoop(runner=runner, pi_bin="pi", wl_bin="wl", stream=False)
    text = loop._read_persisted_audit_text("SA-TEST")

    assert len(audit_show_called) == 1, "wl audit-show should have been called"
    assert len(wl_show_called) == 0, "wl show should NOT have been called for audit"
    assert text == _VALID_AUDIT_REPORT


def test_read_persisted_audit_text_missing_audit_returns_empty():
    """When no audit_results row exists, _read_persisted_audit_text returns ''."""

    def runner(cmd, **kwargs):
        argv = list(cmd)
        if argv[:3] == ["wl", "audit-show", "SA-TEST"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(audit_text=None))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    loop = RalphLoop(runner=runner, pi_bin="pi", wl_bin="wl", stream=False)
    text = loop._read_persisted_audit_text("SA-TEST")

    assert text == ""


def test_read_persisted_audit_text_handles_exception():
    """When wl audit-show fails, _read_persisted_audit_text returns '' without crashing."""

    def runner(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    loop = RalphLoop(runner=runner, pi_bin="pi", wl_bin="wl", stream=False)
    text = loop._read_persisted_audit_text("SA-TEST")

    assert text == ""


# ──────────────────────────────────────────────────────────
# AC2: Fallback persist when audit model does not persist
# but produces a valid report in output stream
# ──────────────────────────────────────────────────────────


def test_fallback_persist_valid_report_ready_to_close():
    """When audit output has 'Ready to close: Yes' but audit is not persisted,
    Ralph should persist from captured output and succeed."""
    persist_called = []
    audit_show_count = []

    def runner(cmd, **kwargs):
        argv = list(cmd)
        if argv[0] == "wl" and argv[1] == "audit-show":
            audit_show_count.append(argv)
            count = len(audit_show_count)
            if count == 1:
                return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(audit_text=None))
            else:
                return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(audit_text=_VALID_AUDIT_REPORT))
        if argv[0] == "wl" and argv[1] == "show":
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_show_response())
        if argv[0] == "wl" and argv[1] == "update" and "--audit-text" in argv:
            persist_called.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_update_audit_response(), stderr="")
        if argv[0] == "pi":
            return subprocess.CompletedProcess(argv, 0, stdout=_pi_audit_output(_VALID_AUDIT_REPORT), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    loop = RalphLoop(
        runner=runner,
        pi_bin="pi",
        wl_bin="wl",
        max_attempts=3,
        stream=False,
    )
    loop._wl_comment_list = lambda *a, **kw: []
    loop._wl_comment_add = lambda *a, **kw: None
    loop._run_checks = lambda: []
    loop._capture_changed_files = lambda: []
    loop._run_merge = lambda: None
    loop._cleanup_pi_process = lambda: None
    loop._scope_in_review = lambda s: True

    result = loop.run_single_item("SA-TEST", implement_command="implement-single", skip_implement=True)

    assert len(persist_called) >= 1, "Fallback persist should have been called"
    assert result["status"] == "success", f"Expected success, got {result}"


def test_fallback_persist_not_used_when_output_lacks_marker():
    """When audit output has NO valid 'Ready to close:' marker, fallback
    persist should not be called, and the attempt should retry."""
    persist_called = []
    pi_call_count = []

    def runner(cmd, **kwargs):
        argv = list(cmd)
        if argv[0] == "wl" and argv[1] == "audit-show":
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(audit_text=None))
        if argv[0] == "wl" and argv[1] == "show":
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_show_response())
        if argv[0] == "wl" and argv[1] == "update" and "--audit-text" in argv:
            persist_called.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_update_audit_response(), stderr="")
        if argv[0] == "pi":
            pi_call_count.append(argv)
            if len(pi_call_count) == 1:
                return subprocess.CompletedProcess(argv, 0, stdout="implement done", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout=_pi_audit_output(_INVALID_AUDIT_OUTPUT), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    loop = RalphLoop(
        runner=runner,
        pi_bin="pi",
        wl_bin="wl",
        max_attempts=2,
        stream=False,
    )
    loop._wl_comment_list = lambda *a, **kw: []
    loop._wl_comment_add = lambda *a, **kw: None
    loop._run_checks = lambda: []
    loop._capture_changed_files = lambda: []
    loop._run_merge = lambda: None
    loop._cleanup_pi_process = lambda: None
    loop._scope_in_review = lambda s: True

    result = loop.run_single_item("SA-TEST", implement_command="implement-single", skip_implement=True)

    assert len(persist_called) == 0, f"Fallback persist should not be called: {persist_called}"
    assert result["status"] == "max_attempts", f"Expected max_attempts, got {result}"


# ──────────────────────────────────────────────────────────
# AC3: Distinguish "not persisted" from "genuine gaps"
# ──────────────────────────────────────────────────────────


def test_genuine_gaps_trigger_remediation_not_persistence():
    """When audit IS persisted and says 'Ready to close: No', Ralph should
    trigger remediation, NOT fallback persistence."""
    persist_called = []

    def runner(cmd, **kwargs):
        argv = list(cmd)
        if argv[0] == "wl" and argv[1] == "audit-show":
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(audit_text=_GAP_AUDIT_REPORT))
        if argv[0] == "wl" and argv[1] == "show":
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_show_response())
        if argv[0] == "wl" and argv[1] == "update" and "--audit-text" in argv:
            persist_called.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_update_audit_response(), stderr="")
        if argv[0] == "pi":
            return subprocess.CompletedProcess(argv, 0, stdout=_pi_audit_output(_GAP_AUDIT_REPORT), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    loop = RalphLoop(
        runner=runner,
        pi_bin="pi",
        wl_bin="wl",
        max_attempts=2,
        stream=False,
    )
    loop._wl_comment_list = lambda *a, **kw: []
    loop._wl_comment_add = lambda *a, **kw: None
    loop._run_checks = lambda: []
    loop._capture_changed_files = lambda: []
    loop._run_merge = lambda: None
    loop._cleanup_pi_process = lambda: None
    loop._scope_in_review = lambda s: True

    result = loop.run_single_item("SA-TEST", implement_command="implement-single", skip_implement=True)

    assert len(persist_called) == 0, f"Fallback persist called on persisted audit: {persist_called}"
    assert result["status"] == "max_attempts", f"Expected max_attempts, got {result}"


# ──────────────────────────────────────────────────────────
# AC4: Cycle detection — retry limit without code changes
# ──────────────────────────────────────────────────────────


def test_cycle_detection_stops_after_max_attempts_no_code_change():
    """When audit does not persist and git HEAD doesn't change, Ralph should
    stop after max_attempts and report the stall."""
    pi_call_count = []
    persist_called = []

    def runner(cmd, **kwargs):
        argv = list(cmd)
        if argv[0] == "wl" and argv[1] == "audit-show":
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(audit_text=None))
        if argv[0] == "wl" and argv[1] == "show":
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_show_response())
        if argv[0] == "wl" and argv[1] == "update" and "--audit-text" in argv:
            persist_called.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"success": False, "error": "failed"}), stderr="")
        if argv[0] == "pi":
            pi_call_count.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_pi_audit_output(_VALID_AUDIT_REPORT), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    loop = RalphLoop(
        runner=runner,
        pi_bin="pi",
        wl_bin="wl",
        max_attempts=3,
        stream=False,
    )
    loop._wl_comment_list = lambda *a, **kw: []
    loop._wl_comment_add = lambda *a, **kw: None
    loop._run_checks = lambda: []
    loop._capture_changed_files = lambda: []
    loop._run_merge = lambda: None
    loop._cleanup_pi_process = lambda: None
    loop._scope_in_review = lambda s: True

    result = loop.run_single_item("SA-TEST", implement_command="implement-single", skip_implement=True)

    assert result["status"] == "max_attempts"
    assert result.get("reason") == "no_persisted_audit"


# ──────────────────────────────────────────────────────────
# AC5: Integration test — fallback works with model that does not persist
# ──────────────────────────────────────────────────────────


def test_fallback_persist_in_main_loop_no_children():
    """When the main loop (run, not run_single_item) encounters a missing
    audit with valid report in output, it should fallback persist."""
    persist_called = []
    audit_show_count = []

    def runner(cmd, **kwargs):
        argv = list(cmd)
        if argv[0] == "wl" and argv[1] == "show":
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-TEST", "stage": "in_review", "status": "open"},
                    "children": [],
                }),
                stderr="",
            )
        if argv[0] == "wl" and argv[1] == "audit-show":
            audit_show_count.append(argv)
            count = len(audit_show_count)
            if count == 1:
                return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(audit_text=None), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_audit_show_response(audit_text=_VALID_AUDIT_REPORT), stderr="")
        if argv[0] == "wl" and argv[1] == "update" and "--audit-text" in argv:
            persist_called.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=_wl_update_audit_response(), stderr="")
        if argv[0] == "pi":
            return subprocess.CompletedProcess(argv, 0, stdout=_pi_audit_output(_VALID_AUDIT_REPORT), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    loop = RalphLoop(
        runner=runner,
        pi_bin="pi",
        wl_bin="wl",
        max_attempts=3,
        stream=False,
    )
    loop._wl_comment_list = lambda *a, **kw: []
    loop._wl_comment_add = lambda *a, **kw: None
    loop._run_checks = lambda: []
    loop._capture_changed_files = lambda: []
    loop._run_merge = lambda: None
    loop._cleanup_pi_process = lambda: None
    loop._scope_ids_recursive = lambda tid: [tid]
    loop._get_children = lambda tid: []
    loop._assert_precondition = lambda tid: None
    loop._latest_audit_comment_ts_for_scope = lambda s: None
    loop._max_updated_at_for_scope = lambda s: None
    loop._child_stage_map = lambda tid: {}
    loop._compact_after_child_transition = lambda *a, **kw: (0, 0)
    loop._scope_in_review = lambda s: True
    loop._latest_audit_comment = lambda wid: None

    result = loop.run("SA-TEST")

    assert len(persist_called) >= 1, f"Fallback persist should have been called: {persist_called}"
    assert result["status"] == "success", f"Expected success, got {result}"


# ──────────────────────────────────────────────────────────
# Helper: _build_remediation_prompt
# ──────────────────────────────────────────────────────────


def test_build_remediation_prompt():
    result = _build_remediation_prompt()
    assert "Address all the gaps" in result
