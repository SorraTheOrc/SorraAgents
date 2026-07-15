"""Tests for the Pi review loop and report assembly (F2).

These tests pin the Pi invocation contract and the exact report structure
so that the F4 implementation has a deterministic target.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from skill.audit.scripts.audit_runner import (
    build_parser,
    cmd_issue,
    cmd_project,
    _call_pi,
    _assemble_issue_report,
    _assemble_project_report,
    _build_issue_json,
    _has_phase1_blocking_issues,
    _CHILDREN_CAP,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = __import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "audit"


def _load_fixture(name: str) -> dict:
    with open(FIXTURE_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _make_pi_response(verdict: str, evidence: str) -> str:
    """Build a minimal pi --mode json response for a single AC verdict.

    Mimics the JSON-stream format that _call_pi parses.
    """
    # Use the agent_end format which is the most authoritative
    lines = [
        json.dumps({"type": "session", "id": "test-session"}),
        json.dumps({
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": f'{{"verdict": "{verdict}", "evidence": "{evidence}"}}',
            },
        }),
        json.dumps({
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f'{{"verdict": "{verdict}", "evidence": "{evidence}"}}'}],
                },
            ],
        }),
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# _call_pi tests
# ---------------------------------------------------------------------------

class TestCallPi:
    """Stub the Pi subprocess invocation and verify the contract."""

    def test_call_pi_spawns_correct_command(self, monkeypatch):
        """Assert the Pi command shape: pi -p --mode json --model <model> <prompt>."""
        captured_cmds = []

        def fake_popen(cmd, **kwargs):
            captured_cmds.append(cmd)
            response = _make_pi_response("met", "test.py:10 — test evidence")
            return SimpleNamespace(
                communicate=lambda timeout=None: (response, ""),
                stdout=SimpleNamespace(read=lambda: response),
                stderr="",
                wait=lambda timeout=None: None,
            )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        _result = _call_pi("review this criterion", model="test/model", pi_bin="pi")
        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert cmd[0] == "pi"
        assert "-p" in cmd
        assert "--mode" in cmd
        assert "json" in cmd
        assert "--model" in cmd
        assert "test/model" in cmd
        assert "review this criterion" in " ".join(cmd)

    def test_call_pi_parses_verdict_and_evidence(self, monkeypatch):
        """Assert that verdict and evidence from FakePi flow through correctly."""

        def fake_popen(cmd, **kwargs):
            response = _make_pi_response("partial", "auth.py:42 — missing edge case")
            return SimpleNamespace(
                communicate=lambda timeout=None: (response, ""),
                stdout=SimpleNamespace(read=lambda: response),
                stderr="",
                wait=lambda timeout=None: None,
            )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = _call_pi("some criterion", model="test/model")
        assert result["verdict"] == "partial"
        assert "auth.py:42" in result["evidence"]

    def test_call_pi_returns_unmet_on_parse_failure(self, monkeypatch):
        """If Pi returns unparseable output, default verdict is unmet."""

        def fake_popen(cmd, **kwargs):
            return SimpleNamespace(
                communicate=lambda timeout=None: ("not valid json at all\n", ""),
                stdout=SimpleNamespace(read=lambda: "not valid json at all\n"),
                stderr="",
                wait=lambda timeout=None: None,
            )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = _call_pi("some criterion", model="test/model")
        assert result["verdict"] == "unmet"

    def test_call_pi_raises_on_missing_binary(self, monkeypatch):
        """If the pi binary is not found, raise a clear error."""

        def fake_popen(cmd, **kwargs):
            raise FileNotFoundError("pi not found")

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        with pytest.raises(RuntimeError, match="pi binary not found"):
            _call_pi("some criterion", model="test/model", pi_bin="/nonexistent/pi")


    def test_call_pi_timeout_expired_returns_structured_error(self, monkeypatch):
        """When communicate times out, _call_pi should return a structured error
        with a clear diagnostic message indicating timeout and manual audit needed."""
        call_count = [0]

        def fake_popen(cmd, **kwargs):
            def timed_out_communicate(timeout=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise subprocess.TimeoutExpired(cmd="pi", timeout=timeout or 100)
                return ("", "")  # second call after kill returns empty

            return SimpleNamespace(
                communicate=timed_out_communicate,
                kill=lambda: None,
                stdout=None,
                stderr=None,
            )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        result = _call_pi("test prompt", model="test/model")

        assert result["verdict"] == "unmet"
        evidence = result.get("evidence", "")
        assert evidence, "Evidence should not be empty when timeout occurs"
        assert "timed out" in evidence.lower() or "timeout" in evidence.lower(), (
            f"Evidence should mention timeout: {evidence}"
        )
        assert "manual audit" in evidence.lower(), (
            f"Evidence should mention manual audit: {evidence}"
        )

    def test_call_pi_timeout_is_generous(self, monkeypatch):
        """The communicate timeout should be generous (>= 300s) for large prompts."""
        captured_timeout = [None]
        call_count = [0]

        def fake_popen(cmd, **kwargs):
            def capture_communicate(timeout=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    captured_timeout[0] = timeout
                    raise subprocess.TimeoutExpired(cmd="pi", timeout=timeout or 0)
                return ("", "")  # second call after kill succeeds

            return SimpleNamespace(
                communicate=capture_communicate,
                kill=lambda: None,
                stdout=None,
                stderr=None,
            )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        _call_pi("test prompt", model="test/model")

        assert captured_timeout[0] is not None, "communicate should receive a timeout value"
        assert captured_timeout[0] >= 300, (
            f"communicate timeout {captured_timeout[0]}s should be >= 300s "
            "to allow large audit prompts to complete"
        )
        assert captured_timeout[0] <= 900, (
            f"communicate timeout {captured_timeout[0]}s should be <= 900s "
            "(not exceed the original value)"
        )


# ---------------------------------------------------------------------------
# Report assembly tests
# ---------------------------------------------------------------------------

class TestAssembleIssueReport:
    """Verify the canonical issue report structure."""

    def test_report_starts_with_ready_to_close(self):
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "met", "evidence": "file.py:1 — note"},
            {"text": "Criterion 2", "verdict": "unmet", "evidence": ""},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close:")

    def test_yes_when_all_met(self):
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "met", "evidence": "file.py:1 — note"},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: Yes")

    def test_no_when_any_unmet(self):
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "met", "evidence": "file.py:1 — note"},
            {"text": "Criterion 2", "verdict": "unmet", "evidence": ""},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: No")

    def test_no_when_any_partial(self):
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "partial", "evidence": "file.py:1 — partial"},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: No")

    def test_yes_when_all_adjusted(self):
        """Adjusted verdict should not block ready-to-close."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "adjusted", "evidence": "file.py:1 — adjusted with justification"},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: Yes")

    def test_yes_when_mixed_met_and_adjusted(self):
        """Mixed met and adjusted should still be ready to close."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "met", "evidence": "file.py:1 — ok"},
            {"text": "Criterion 2", "verdict": "adjusted", "evidence": "file.py:2 — adjusted for performance"},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: Yes")

    def test_no_when_mixed_adjusted_and_unmet(self):
        """Unmet criteria still block closure even when adjusted ones are present."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "adjusted", "evidence": "file.py:1 — adjusted"},
            {"text": "Criterion 2", "verdict": "unmet", "evidence": ""},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: No")

    def test_summary_mentions_adjusted_count(self):
        """Summary should mention how many criteria were adjusted."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "adjusted", "evidence": "file.py:1 — adjusted"},
            {"text": "Criterion 2", "verdict": "met", "evidence": "file.py:2 — ok"},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert "adjusted" in report.lower()

    def test_variance_decisions_section_present_when_adjusted(self):
        """When adjusted verdicts exist, a variance decisions section should appear."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "adjusted", "evidence": "file.py:1 — adjusted for reason X"},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert "## Variance Decisions" in report
        assert "adjusted for reason X" in report

    def test_variance_decisions_section_absent_when_no_adjusted(self):
        """When no adjusted verdicts, variance section should be absent."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "met", "evidence": "file.py:1 — ok"},
            {"text": "Criterion 2", "verdict": "partial", "evidence": "file.py:2 — partial"},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert "## Variance Decisions" not in report

    def test_adjusted_in_child_does_not_block_closure(self):
        """Adjusted verdicts in children should not block closure."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [{"text": "Parent AC", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child Task",
                "id": "SA-CHILD",
                "status": "in_progress",
                "stage": "in_review",
                "ac_results": [{"text": "Child AC", "verdict": "adjusted", "evidence": "y:1 — adjusted"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: Yes")

    def test_adjusted_verdict_table_shows_adjusted(self):
        """The table should show 'adjusted' as the verdict."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Criterion 1", "verdict": "adjusted", "evidence": "file.py:1 — reason"},
        ]
        child_results = []
        _report = _assemble_issue_report(issue, ac_results, child_results)
        # The verdict should appear as 

    def test_yes_when_children_in_review_stage(self):
        """Children in in_review stage (even with in_progress status) allow closure."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [{"text": "Criterion 1", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child Task",
                "id": "SA-CHILD",
                "status": "in_progress",
                "stage": "in_review",
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: Yes")

    def test_yes_when_children_done_stage(self):
        """Children in done stage allow closure."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [{"text": "Criterion 1", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child Task",
                "id": "SA-CHILD",
                "status": "completed",
                "stage": "done",
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: Yes")

    def test_no_when_children_in_progress_stage(self):
        """Children in in_progress stage (not in_review) block closure."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [{"text": "Criterion 1", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child Task",
                "id": "SA-CHILD",
                "status": "in_progress",
                "stage": "in_progress",
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: No")

    def test_no_when_children_plan_complete_stage(self):
        """Children in plan_complete stage block closure."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [{"text": "Criterion 1", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child Task",
                "id": "SA-CHILD",
                "status": "open",
                "stage": "plan_complete",
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: No")

    def test_mixed_children_stages(self):
        """Mixed children stages: all must be in_review/done to close."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [{"text": "Criterion 1", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child 1",
                "id": "SA-C1",
                "status": "in_progress",
                "stage": "in_review",
                "ac_results": [],
            },
            {
                "title": "Child 2",
                "id": "SA-C2",
                "status": "in_progress",
                "stage": "in_progress",
                "ac_results": [],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: No")

    def test_empty_stage_excluded_from_check(self):
        """Children with empty stage are excluded from stage check."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [{"text": "Criterion 1", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child Task",
                "id": "SA-CHILD",
                "status": "in_progress",
                "stage": "",
                "ac_results": [],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        # Empty stage children are excluded from check, so all_met is the only factor
        assert report.startswith("Ready to close: Yes")

    def test_contains_section_headings_in_order(self):
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [{"text": "C1", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)

        summary_idx = report.index("## Summary")
        ac_idx = report.index("## Acceptance Criteria Status")
        children_idx = report.index("## Children Status")
        assert summary_idx < ac_idx < children_idx

    def test_verdict_and_evidence_in_table(self):
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = [
            {"text": "Must handle auth", "verdict": "met", "evidence": "auth.py:42 — middleware correct"},
        ]
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert "Must handle auth" in report
        assert "met" in report
        assert "auth.py:42" in report
        assert "middleware correct" in report

    def test_children_section_with_child_results(self):
        issue = {"id": "SA-123", "title": "Parent", "description": ""}
        ac_results = [{"text": "Parent AC", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child Task",
                "id": "SA-CHILD",
                "status": "in_progress",
                "stage": "in_progress",
                "ac_results": [
                    {"text": "Child AC", "verdict": "partial", "evidence": "child.py:5 — incomplete"},
                ],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert "Child Task" in report
        assert "SA-CHILD" in report
        assert "Child AC" in report
        assert "partial" in report

    def test_no_children_message(self):
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = []
        child_results = []
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert "No children." in report

    def test_children_cap_note(self):
        """When children exceed the cap, emit an explicit note."""
        issue = {"id": "SA-123", "title": "Test", "description": ""}
        ac_results = []
        # Create more children than the cap
        child_results = [
            {
                "title": f"Child {i}",
                "id": f"SA-CHILD-{i}",
                "status": "open",
                "stage": "intake_complete",
                "ac_results": [],
            }
            for i in range(_CHILDREN_CAP + 3)
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        # Should mention the cap
        assert str(_CHILDREN_CAP) in report
        assert "omitted" in report.lower() or "remaining" in report.lower() or "cap" in report.lower()


class TestAssembleProjectReport:
    """Verify the project-mode report structure."""

    def test_project_report_starts_with_ready_to_close(self):
        summary = "Project is on track."
        recommendation = "Continue current work."
        report = _assemble_project_report(summary, recommendation)
        assert report.startswith("Ready to close:")

    def test_project_report_has_summary_and_recommendation(self):
        summary = "5 items in progress."
        recommendation = "Focus on blocked items."
        report = _assemble_project_report(summary, recommendation)
        assert "## Summary" in report
        assert "## Recommendation" in report
        assert "5 items in progress." in report
        assert "Focus on blocked items." in report

    def test_project_report_no_ac_section(self):
        report = _assemble_project_report("Summary", "Recommendation")
        assert "## Acceptance Criteria Status" not in report
        assert "## Children Status" not in report


# ---------------------------------------------------------------------------
# Integration: cmd_issue with Pi integration
# ---------------------------------------------------------------------------

class TestCmdIssueWithPi:
    """Test cmd_issue end-to-end with a stubbed Pi."""

    def test_issue_calls_pi_per_ac(self, monkeypatch):
        """Assert at least one Pi call is made for the batched AC review."""
        pi_calls = []

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            pi_calls.append(prompt)
            return {"verdict": "met", "evidence": "test.py:1 — covered"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-INTEGRATION", runner=fake_runner, model="test/model", persist=False)
        # ACs are batched into a single Pi call, so expect at least 1 call
        assert len(pi_calls) >= 1

    def test_issue_report_reflects_pi_verdicts(self, monkeypatch, capsys):
        """Assert batched verdicts from Pi flow into the report table."""
        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            # Return a batched JSON array matching the bulleted fixture's 3 ACs
            return {
                "verdict": "met",
                "evidence": json.dumps([
                    {"index": 0, "verdict": "met", "evidence": "api.py:10 — ok"},
                    {"index": 1, "verdict": "partial", "evidence": "handler.py:22 — missing timeout"},
                    {"index": 2, "verdict": "met", "evidence": "errors.py:5 — ok"},
                ]),
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_bulleted_ac.json")),
            )

        cmd_issue("SA-VERDICTS", runner=fake_runner, persist=False)
        captured = capsys.readouterr()
        assert "partial" in captured.out
        assert "handler.py:22" in captured.out
        assert "missing timeout" in captured.out

    def test_issue_ready_to_close_yes_when_all_met(self, monkeypatch, capsys):
        """Ready to close: Yes when all AC verdicts are met."""

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            # Return a batched JSON array with all met verdicts for the 3 numbered ACs
            return {
                "verdict": "met",
                "evidence": json.dumps([
                    {"index": 0, "verdict": "met", "evidence": "auth.py:1 — done"},
                    {"index": 1, "verdict": "met", "evidence": "logging.py:2 — done"},
                    {"index": 2, "verdict": "met", "evidence": "rbac.py:3 — done"},
                ]),
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-YES", runner=fake_runner, persist=False)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close: Yes")

    def test_timeout_diagnostic_in_report(self, monkeypatch, capsys):
        """When _call_pi returns a timeout diagnostic, the report should contain it."""
        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            # Simulate a timeout result with clear diagnostic
            return {
                "verdict": "unmet",
                "evidence": "Pi model call timed out after 100s. Manual audit required.",
                "extracted_text": "",
                "raw_stdout": "",
                "raw_stderr": "",
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-TIMEOUT", runner=fake_runner, persist=False)
        captured = capsys.readouterr()
        # The report should contain the timeout diagnostic
        assert "timed out" in captured.out.lower() or "timeout" in captured.out.lower()
        assert "manual audit" in captured.out.lower()


# ---------------------------------------------------------------------------
# Integration: cmd_project with Pi integration
# ---------------------------------------------------------------------------

class TestCmdProjectWithPi:
    """Test cmd_project end-to-end with stubbed Pi."""

    def test_project_report_structure(self, monkeypatch, capsys):
        """Project mode: only Summary and Recommendation sections."""

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {"verdict": "met", "evidence": "summary:0 — ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        cmd_project(runner=fake_runner)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close:")
        assert "## Summary" in captured.out
        assert "## Recommendation" in captured.out
        assert "## Acceptance Criteria Status" not in captured.out
        assert "## Children Status" not in captured.out


# ---------------------------------------------------------------------------
# Children review tests
# ---------------------------------------------------------------------------

class TestChildrenReview:
    """Assert children review behaviour: depth 1, skip completed, ignore deleted, cap at 10."""

    def _make_child_runner(self, children_data):
        """Create a fake runner that returns children from wl show --children."""
        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = children_data

        def fake_runner(cmd, **kwargs):
            if "show" in cmd and "--children" in cmd:
                return _fake_proc(stdout=json.dumps(parent_wi))
            return _fake_proc(stdout="[]")

        return fake_runner

    def test_skips_completed_children(self, monkeypatch, capsys):
        """Children with completed/done status are skipped from review."""

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {"verdict": "met", "evidence": "x:1 — ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        children = [
            {
                "id": "SA-DONE-001",
                "title": "Completed child",
                "status": "completed",
                "stage": "done",
                "description": "## Acceptance Criteria\n1. Already done\n",
            },
            {
                "id": "SA-ACTIVE-001",
                "title": "Active child",
                "status": "in_progress",
                "stage": "in_progress",
                "description": "## Acceptance Criteria\n1. In progress\n",
            },
        ]

        runner = self._make_child_runner(children)
        cmd_issue("SA-CHILDREN", runner=runner, persist=False)
        captured = capsys.readouterr()
        # Completed child should NOT appear in the review (skipped)
        assert "Completed child" not in captured.out
        # Active child should appear with Pi verdicts
        assert "Active child" in captured.out

    def test_ignores_deleted_children(self, monkeypatch, capsys):
        """Deleted children are completely ignored."""

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {"verdict": "met", "evidence": "x:1 — ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        children = [
            {
                "id": "SA-DELETED",
                "title": "Deleted child",
                "status": "completed",
                "stage": "done",
                "deletedBy": "someone",
                "description": "",
            },
        ]

        runner = self._make_child_runner(children)
        cmd_issue("SA-CHILDREN", runner=runner, persist=False)
        captured = capsys.readouterr()
        assert "Deleted child" not in captured.out

    def test_caps_children_at_10(self, monkeypatch, capsys):
        """Only the first 10 children are reviewed; an explicit note is emitted."""

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {"verdict": "met", "evidence": "x:1 — ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        children = [
            {
                "id": f"SA-CHILD-{i:02d}",
                "title": f"Child {i}",
                "status": "open",
                "stage": "intake_complete",
                "description": "## Acceptance Criteria\n1. Something\n",
            }
            for i in range(15)
        ]

        runner = self._make_child_runner(children)
        cmd_issue("SA-CHILDREN", runner=runner, persist=False)
        captured = capsys.readouterr()
        # Should mention the cap in the explicit note
        assert "omitted for brevity" in captured.out


# ---------------------------------------------------------------------------
# --json flag tests
# ---------------------------------------------------------------------------

class TestBuildParserJsonFlag:
    """Verify --json is accepted by both subcommands."""

    def test_issue_parses_json_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--json"])
        assert args.json is True

    def test_project_parses_json_flag(self):
        parser = build_parser()
        args = parser.parse_args(["project", "--json"])
        assert args.json is True

    def test_issue_defaults_json_false(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.json is False


class TestBuildParserForceFlag:
    """Verify --force is accepted only by the issue subcommand."""

    def test_issue_parses_force_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--force"])
        assert args.force is True

    def test_issue_defaults_force_false(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.force is False

    def test_force_can_combine_with_other_flags(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--force", "--json", "--do-not-persist"])
        assert args.force is True
        assert args.json is True
        assert args.do_not_persist is True

    def test_project_rejects_force_flag(self):
        """--force is only for issue subcommand; project should reject it."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["project", "--force"])


class TestCmdIssueJsonMode:
    """Verify cmd_issue emits structured JSON when json_mode=True."""

    def test_json_output_has_expected_keys(self, monkeypatch, capsys):
        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {
                "verdict": "met",
                "evidence": json.dumps([
                    {"index": 0, "verdict": "met", "evidence": "x:1 — ok"},
                ]),
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-JSON", runner=fake_runner, json_mode=True, persist=False)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert "ready_to_close" in payload
        assert "summary" in payload
        assert "acceptance_criteria" in payload
        assert "children" in payload
        assert isinstance(payload["ready_to_close"], bool)

    def test_json_output_ready_to_close_true_when_all_met(self, monkeypatch, capsys):
        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {
                "verdict": "met",
                "evidence": json.dumps([
                    {"index": 0, "verdict": "met", "evidence": "x:1 — ok"},
                    {"index": 1, "verdict": "met", "evidence": "y:2 — ok"},
                    {"index": 2, "verdict": "met", "evidence": "z:3 — ok"},
                ]),
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-JSON", runner=fake_runner, json_mode=True, persist=False)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["ready_to_close"] is True

    def test_json_output_ac_results_present(self, monkeypatch, capsys):
        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {
                "verdict": "met",
                "evidence": json.dumps([
                    {"index": 0, "verdict": "partial", "evidence": "a.py:1 — partial"},
                ]),
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_bulleted_ac.json")),
            )

        cmd_issue("SA-JSON", runner=fake_runner, json_mode=True, persist=False)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert len(payload["acceptance_criteria"]) == 3
        assert payload["acceptance_criteria"][0]["verdict"] == "partial"
        assert payload["acceptance_criteria"][0]["evidence"] == "a.py:1 — partial"

    def test_default_mode_still_emits_markdown(self, monkeypatch, capsys):
        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {"verdict": "met", "evidence": "x:1 — ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-MD", runner=fake_runner, json_mode=False, persist=False)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close:")


class TestCmdProjectJsonMode:
    """Verify cmd_project emits structured JSON when json_mode=True."""

    def test_json_output_has_expected_keys(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps({"success": True, "workItems": []}),
            )

        cmd_project(runner=fake_runner, json_mode=True)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert "ready_to_close" in payload
        assert "summary" in payload
        assert "recommendation" in payload
        assert payload["ready_to_close"] is False

    def test_default_mode_still_emits_markdown(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps({"success": True, "workItems": []}),
            )

        cmd_project(runner=fake_runner, json_mode=False)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close:")


# ---------------------------------------------------------------------------
# Child audit verdict tests
# ---------------------------------------------------------------------------

class TestChildAuditVerdict:
    """Verify child audit verdict checking in report assembly and phase 1."""

    def test_ready_no_when_child_audit_verdict_no(self):
        """When a child's child_audit_ready is False, parent report says No."""
        issue = {"id": "SA-123", "title": "Parent", "description": ""}
        ac_results = [{"text": "Parent AC", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child 1",
                "id": "SA-C1",
                "status": "in_progress",
                "stage": "in_review",
                "child_audit_ready": False,
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: No")

    def test_ready_yes_when_child_audit_verdict_yes(self):
        """When all children have child_audit_ready True, parent can close."""
        issue = {"id": "SA-123", "title": "Parent", "description": ""}
        ac_results = [{"text": "Parent AC", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child 1",
                "id": "SA-C1",
                "status": "in_progress",
                "stage": "in_review",
                "child_audit_ready": True,
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: Yes")

    def test_ready_no_when_child_no_audit_verdict_field_still_works(self):
        """Backward compat: children without child_audit_ready field use stage check only."""
        issue = {"id": "SA-123", "title": "Parent", "description": ""}
        ac_results = [{"text": "Parent AC", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child 1",
                "id": "SA-C1",
                "status": "in_progress",
                "stage": "in_review",
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        assert report.startswith("Ready to close: Yes")

    def test_completed_child_exempt_from_audit_verdict(self):
        """Completed/done children are exempt from child_audit_ready check."""
        issue = {"id": "SA-123", "title": "Parent", "description": ""}
        ac_results = [{"text": "Parent AC", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Done Child",
                "id": "SA-DONE",
                "status": "completed",
                "stage": "done",
                "child_audit_ready": False,  # Would block if exempted, but should be exempt
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        report = _assemble_issue_report(issue, ac_results, child_results)
        # Completed child with child_audit_ready=False is exempt, should not block
        assert report.startswith("Ready to close: Yes")

    def test_child_audit_blocks_phase1(self):
        """_has_phase1_blocking_issues returns True when child_audit_ready is False."""
        cq_findings = []
        child_results = [
            {
                "title": "Bad Child",
                "id": "SA-BAD",
                "status": "in_progress",
                "stage": "in_review",
                "child_audit_ready": False,
                "ac_results": [],
            },
        ]
        blocked, reason = _has_phase1_blocking_issues(cq_findings, child_results)
        assert blocked is True
        assert "audit says not ready" in reason.lower() or "unready" in reason.lower() or "not ready" in reason.lower()

    def test_child_audit_ready_does_not_block_phase1(self):
        """_has_phase1_blocking_issues returns False when all child_audit_ready are True."""
        cq_findings = []
        child_results = [
            {
                "title": "Good Child",
                "id": "SA-GOOD",
                "status": "in_progress",
                "stage": "in_review",
                "child_audit_ready": True,
                "ac_results": [],
            },
        ]
        blocked, reason = _has_phase1_blocking_issues(cq_findings, child_results)
        assert blocked is False

    def test_phase1_still_blocks_for_stage_when_no_audit_field(self):
        """Backward compat: without child_audit_ready field, stage check still works."""
        cq_findings = []
        child_results = [
            {
                "title": "Child in progress",
                "id": "SA-IP",
                "status": "in_progress",
                "stage": "in_progress",
                "ac_results": [],
            },
        ]
        blocked, reason = _has_phase1_blocking_issues(cq_findings, child_results)
        assert blocked is True


class TestBuildIssueJsonChildAuditVerdict:
    """Verify _build_issue_json incorporates child audit verdict."""

    def test_ready_false_when_child_audit_no(self):
        """_build_issue_json ready_to_close is False when child_audit_ready is False."""
        issue = {"id": "SA-123", "title": "Parent", "description": ""}
        ac_results = [{"text": "Parent AC", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child",
                "id": "SA-C1",
                "status": "in_progress",
                "stage": "in_review",
                "child_audit_ready": False,
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        payload = _build_issue_json(issue, ac_results, child_results)
        assert payload["ready_to_close"] is False

    def test_ready_true_when_child_audit_yes(self):
        """_build_issue_json ready_to_close is True when all child_audit_ready are True."""
        issue = {"id": "SA-123", "title": "Parent", "description": ""}
        ac_results = [{"text": "Parent AC", "verdict": "met", "evidence": "x:1 — ok"}]
        child_results = [
            {
                "title": "Child",
                "id": "SA-C1",
                "status": "in_progress",
                "stage": "in_review",
                "child_audit_ready": True,
                "ac_results": [{"text": "Child AC", "verdict": "met", "evidence": "y:1 — ok"}],
            },
        ]
        payload = _build_issue_json(issue, ac_results, child_results)
        assert payload["ready_to_close"] is True
