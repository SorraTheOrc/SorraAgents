"""Tests for the Pi review loop and report assembly (F2).

These tests pin the Pi invocation contract and the exact report structure
so that the F4 implementation has a deterministic target.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skill.audit.scripts.audit_runner import (
    build_parser,
    cmd_issue,
    cmd_project,
    _call_pi,
    _assemble_issue_report,
    _assemble_project_report,
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

        result = _call_pi("review this criterion", model="test/model", pi_bin="pi")
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

        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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
        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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

        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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


# ---------------------------------------------------------------------------
# Integration: cmd_project with Pi integration
# ---------------------------------------------------------------------------

class TestCmdProjectWithPi:
    """Test cmd_project end-to-end with stubbed Pi."""

    def test_project_report_structure(self, monkeypatch, capsys):
        """Project mode: only Summary and Recommendation sections."""

        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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

        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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

        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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

        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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


class TestCmdIssueJsonMode:
    """Verify cmd_issue emits structured JSON when json_mode=True."""

    def test_json_output_has_expected_keys(self, monkeypatch, capsys):
        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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
        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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
        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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
        def fake_call_pi(prompt, model="test/model", pi_bin="pi"):
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
        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps({"success": True, "workItems": []}),
            )

        cmd_project(runner=fake_runner, json_mode=False)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close:")
