"""Tests for the audit runner core (F1).

These tests pin the CLI shape, ``wl`` invocation, AC extraction, and
persistence delegation of ``skill/audit/scripts/audit_runner.py``.

They were written *before* the implementation (F3) so that the implementation
is driven by a precise contract rather than being inferred from prose.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skill.audit.scripts.audit_runner import (
    build_parser,
    cmd_issue,
    cmd_project,
    main,
    _extract_acs,
    _run_wl,
)

# ---------------------------------------------------------------------------
# Fixtures directory
# ---------------------------------------------------------------------------
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "audit"


def _load_fixture(name: str) -> dict:
    """Load a JSON fixture from tests/fixtures/audit/."""
    with open(FIXTURE_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# CLI parsing tests
# ---------------------------------------------------------------------------

class TestCLIParsing:
    """Assert that the CLI subcommands exist and parse the expected flags."""

    def test_issue_subcommand_exists(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.command == "issue"
        assert args.issue_id == "SA-123"

    def test_issue_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.persist is False
        assert args.pi_bin == "pi"
        assert args.model == "opencode-go/glm-5.1"

    def test_issue_persist_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--persist"])
        assert args.persist is True

    def test_issue_pi_bin_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--pi-bin", "/usr/local/bin/pi"])
        assert args.pi_bin == "/usr/local/bin/pi"

    def test_issue_model_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--model", "custom/model"])
        assert args.model == "custom/model"

    def test_project_subcommand_exists(self):
        parser = build_parser()
        args = parser.parse_args(["project"])
        assert args.command == "project"

    def test_project_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["project"])
        assert args.pi_bin == "pi"
        assert args.model == "opencode-go/glm-5.1"

    def test_project_pi_bin_flag(self):
        parser = build_parser()
        args = parser.parse_args(["project", "--pi-bin", "/opt/pi"])
        assert args.pi_bin == "/opt/pi"

    def test_project_model_flag(self):
        parser = build_parser()
        args = parser.parse_args(["project", "--model", "other/model"])
        assert args.model == "other/model"

    def test_no_subcommand_returns_2(self):
        rc = main([])
        assert rc == 2

    def test_no_subcommand_via_main(self):
        rc = main([])
        assert rc == 2


# ---------------------------------------------------------------------------
# _run_wl tests
# ---------------------------------------------------------------------------

class TestRunWl:
    """Fake ``subprocess.run`` for ``wl show --children --json`` and
    ``wl dep list --json`` and assert exact argv + JSON-decoding behaviour."""

    def test_run_wl_success(self):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            return _fake_proc(stdout='{"success": true}')

        result = _run_wl(fake_runner, ["wl", "show", "SA-123", "--children", "--json"])
        assert result == {"success": True}
        assert calls == [["wl", "show", "SA-123", "--children", "--json"]]

    def test_run_wl_nonzero_exit_raises(self):
        def fake_runner(cmd, **kwargs):
            return _fake_proc(returncode=1, stderr="not found")

        with pytest.raises(RuntimeError, match="wl command failed"):
            _run_wl(fake_runner, ["wl", "show", "SA-NOEXIST", "--json"])

    def test_run_wl_invalid_json_raises(self):
        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout="not json")

        with pytest.raises(RuntimeError, match="Invalid JSON"):
            _run_wl(fake_runner, ["wl", "dep", "list", "SA-123", "--json"])

    def test_run_wl_dep_list(self):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            return _fake_proc(stdout="[]")

        result = _run_wl(fake_runner, ["wl", "dep", "list", "SA-123", "--json"])
        assert result == []
        assert calls == [["wl", "dep", "list", "SA-123", "--json"]]


# ---------------------------------------------------------------------------
# Acceptance-criteria extraction tests
# ---------------------------------------------------------------------------

class TestExtractACs:
    """AC extraction from both ``## Acceptance Criteria`` and
    ``### Acceptance Criteria`` headings, with numbered and bulleted variants."""

    def test_numbered_ac_under_h2(self):
        desc = _load_fixture("wi_with_numbered_ac.json")["workItem"]["description"]
        acs = _extract_acs(desc)
        assert len(acs) == 3
        assert "The system must handle user authentication." in acs[0]
        assert "The system must log all access attempts." in acs[1]
        assert "The system must support role-based access control." in acs[2]

    def test_bulleted_ac_under_h2(self):
        desc = _load_fixture("wi_with_bulleted_ac.json")["workItem"]["description"]
        acs = _extract_acs(desc)
        assert len(acs) == 3
        assert acs[0] == "The API must return 200 for valid requests."
        assert acs[1] == "The API must return 400 for malformed input."
        assert acs[2] == "The API must return 500 for internal errors."

    def test_numbered_ac_under_h3(self):
        desc = _load_fixture("wi_with_h3_ac.json")["workItem"]["description"]
        acs = _extract_acs(desc)
        assert len(acs) == 2
        assert "The cache must invalidate after TTL expiry." in acs[0]
        assert "The cache must support distributed locking." in acs[1]

    def test_no_ac_section(self):
        desc = _load_fixture("wi_without_ac.json")["workItem"]["description"]
        acs = _extract_acs(desc)
        assert acs == ["No acceptance criteria defined."]

    def test_no_ac_section_empty_description(self):
        acs = _extract_acs("")
        assert acs == ["No acceptance criteria defined."]

    def test_bulleted_with_asterisk(self):
        desc = (
            "## Summary\n\n## Acceptance Criteria\n"
            "* First criterion\n* Second criterion\n\n## Other\n"
        )
        acs = _extract_acs(desc)
        assert acs == ["First criterion", "Second criterion"]

    def test_stops_at_next_heading(self):
        desc = (
            "## Acceptance Criteria\n"
            "1. Must do X\n2. Must do Y\n\n## Implementation\n"
            "Some implementation details.\n"
        )
        acs = _extract_acs(desc)
        assert acs == ["Must do X", "Must do Y"]

    def test_success_criteria_synonym(self):
        desc = (
            "## Summary\n\n## Success Criteria\n"
            "1. Must be fast\n\n## Notes\n"
        )
        acs = _extract_acs(desc)
        assert acs == ["Must be fast"]


# ---------------------------------------------------------------------------
# Persistence delegation tests
# ---------------------------------------------------------------------------

class TestPersistenceDelegation:
    """Assert that ``--persist`` delegates to ``persist_audit`` rather than
    duplicating the ``wl update --audit-text`` call."""

    def test_persist_delegates_to_persist_audit(self, monkeypatch):
        persisted = {}

        def fake_persist(issue_id, report_text, **kwargs):
            persisted["issue_id"] = issue_id
            persisted["report_text"] = report_text
            return 0

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x": {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        rc = cmd_issue("SA-TEST-001", persist=True, runner=fake_runner)
        assert rc == 0
        assert persisted["issue_id"] == "SA-TEST-001"
        assert "Ready to close:" in persisted["report_text"]
        assert "## Acceptance Criteria Status" in persisted["report_text"]

    def test_no_persist_returns_zero(self, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x": {"verdict": "met", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        rc = cmd_issue("SA-TEST-002", persist=False, runner=fake_runner)
        assert rc == 0

    def test_persist_propagates_nonzero_from_persist_audit(self, monkeypatch):
        def fake_persist(issue_id, report_text, **kwargs):
            return 1

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_without_ac.json")),
            )

        rc = cmd_issue("SA-FAIL", persist=True, runner=fake_runner)
        assert rc == 1


# ---------------------------------------------------------------------------
# Report structure tests (issue mode)
# ---------------------------------------------------------------------------

class TestReportStructure:
    """Validate the assembled report format for issue mode."""

    def test_report_starts_with_ready_to_close(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x": {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-STRUCT", runner=fake_runner)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close:")

    def test_report_contains_section_headings(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x": {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-STRUCT", runner=fake_runner)
        captured = capsys.readouterr()
        assert "## Summary" in captured.out
        assert "## Acceptance Criteria Status" in captured.out
        assert "## Children Status" in captured.out

    def test_report_contains_ac_table(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x": {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_bulleted_ac.json")),
            )

        cmd_issue("SA-STRUCT", runner=fake_runner)
        captured = capsys.readouterr()
        assert "| # | Criterion | Verdict | Evidence |" in captured.out
        assert "The API must return 200 for valid requests." in captured.out
        assert "unmet" in captured.out

    def test_report_no_ac_fallback(self, capsys):
        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_without_ac.json")),
            )

        cmd_issue("SA-STRUCT", runner=fake_runner)
        captured = capsys.readouterr()
        assert "No acceptance criteria defined." in captured.out


# ---------------------------------------------------------------------------
# Project-mode report tests
# ---------------------------------------------------------------------------

class TestProjectMode:
    """Validate project-mode report structure."""

    def test_project_report_starts_with_ready_to_close(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x": {"verdict": "met", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        cmd_project(runner=fake_runner)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close:")

    def test_project_report_has_summary_and_recommendation(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x": {"verdict": "met", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        cmd_project(runner=fake_runner)
        captured = capsys.readouterr()
        assert "## Summary" in captured.out
        assert "## Recommendation" in captured.out
        # Project mode should NOT have AC or children sections
        assert "## Acceptance Criteria Status" not in captured.out
        assert "## Children Status" not in captured.out


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------

class TestExitCodes:
    """Assert correct exit codes for various failure modes."""

    def test_issue_wl_failure_returns_1(self, capsys):
        def fake_runner(cmd, **kwargs):
            return _fake_proc(returncode=1, stderr="work item not found")

        rc = cmd_issue("SA-MISSING", runner=fake_runner)
        assert rc == 1

    def test_project_wl_failure_returns_1(self):
        def fake_runner(cmd, **kwargs):
            return _fake_proc(returncode=1, stderr="wl error")

        rc = cmd_project(runner=fake_runner)
        assert rc == 1

    def test_no_subcommand_returns_2(self):
        assert main([]) == 2
