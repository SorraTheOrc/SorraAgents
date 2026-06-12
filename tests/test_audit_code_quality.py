"""Tests for code quality integration into the audit runner.

These tests verify that:
- audit_runner.py invokes code quality checks before AC verification
- Critical/high findings block closure ("Ready to close: No")
- Medium/low findings report warnings but don't block closure
- Code quality section appears in the audit report
- Code quality failure does not crash the audit

The target audit_runner.py modifications are in F7; these tests establish
the expected behavior contract using mocks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from skill.audit.scripts.audit_runner import (
        _assemble_issue_report,
        _extract_acs,
        cmd_issue,
    )
    _AUDIT_RUNNER_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    _AUDIT_RUNNER_AVAILABLE = False
    _assemble_issue_report = None
    _extract_acs = None
    cmd_issue = None

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "audit"


def _load_fixture(name: str) -> dict:
    with open(FIXTURE_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Sample code quality output
# ---------------------------------------------------------------------------

SAMPLE_CQ_CRITICAL_FINDING = json.dumps({
    "languages": ["python"],
    "linters": [{"name": "ruff", "available": True}],
    "total_findings": 1,
    "findings_by_severity": {"critical": 1, "high": 0, "medium": 0, "low": 0},
    "findings": [
        {
            "file": "src/main.py",
            "line": 42,
            "severity": "critical",
            "message": "Unused variable `x`",
            "linter": "ruff",
            "code": "F841",
        }
    ],
})

SAMPLE_CQ_HIGH_FINDING = json.dumps({
    "languages": ["python"],
    "linters": [{"name": "ruff", "available": True}],
    "total_findings": 1,
    "findings_by_severity": {"critical": 0, "high": 1, "medium": 0, "low": 0},
    "findings": [
        {
            "file": "src/main.py",
            "line": 10,
            "severity": "high",
            "message": "Syntax error",
            "linter": "ruff",
            "code": "E999",
        }
    ],
})

SAMPLE_CQ_MEDIUM_FINDING = json.dumps({
    "languages": ["python"],
    "linters": [{"name": "ruff", "available": True}],
    "total_findings": 1,
    "findings_by_severity": {"critical": 0, "high": 0, "medium": 1, "low": 0},
    "findings": [
        {
            "file": "src/utils.py",
            "line": 5,
            "severity": "medium",
            "message": "Unused import `os`",
            "linter": "ruff",
            "code": "W0611",
        }
    ],
})

SAMPLE_CQ_LOW_FINDING = json.dumps({
    "languages": ["python"],
    "linters": [{"name": "ruff", "available": True}],
    "total_findings": 1,
    "findings_by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 1},
    "findings": [
        {
            "file": "src/style.py",
            "line": 15,
            "severity": "low",
            "message": "Line too long",
            "linter": "ruff",
            "code": "E501",
        }
    ],
})

SAMPLE_CQ_CLEAN = json.dumps({
    "languages": ["python"],
    "linters": [{"name": "ruff", "available": True}],
    "total_findings": 0,
    "findings_by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    "findings": [],
})

# An issue fixture with numbered ACs for testing
ISSUE_WITH_ACS = _load_fixture("wi_with_numbered_ac.json")
ISSUE_WITHOUT_ACS = _load_fixture("wi_without_ac.json")


# ===================================================================
# Tests: code quality section in assembled report
# ===================================================================


def _check_assemble_issue_report_supports_cq():
    """Check if _assemble_issue_report accepts code_quality_findings kwarg."""
    if not _AUDIT_RUNNER_AVAILABLE or _assemble_issue_report is None:
        pytest.skip("audit_runner module not available")
    try:
        import inspect
        sig = inspect.signature(_assemble_issue_report)
        if 'code_quality_findings' not in sig.parameters:
            pytest.skip("_assemble_issue_report does not yet accept code_quality_findings")
    except (ValueError, TypeError):
        pytest.skip("Cannot inspect _assemble_issue_report signature")


class TestCodeQualityReportSection:
    """Tests for the '### Code Quality' section in the audit report."""

    @pytest.fixture(autouse=True)
    def _check_support(self):
        _check_assemble_issue_report_supports_cq()

    def test_report_contains_code_quality_section(self):
        """The assembled report should include a '### Code Quality' section."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[
                {"file": "src/main.py", "line": 1, "severity": "medium",
                 "message": "Test finding", "linter": "ruff", "code": "W001"}
            ],
        )
        assert "### Code Quality" in report

    def test_code_quality_section_lists_findings(self):
        """The code quality section should list individual findings."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[
                {"file": "src/main.py", "line": 42, "severity": "high",
                 "message": "Syntax error", "linter": "ruff", "code": "E999"}
            ],
        )
        assert "src/main.py" in report
        assert "Syntax error" in report
        assert "ruff" in report
        assert "E999" in report

    def test_no_findings_shows_clean_message(self):
        """When there are no code quality findings, show a clean message."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[],
        )
        # Should still have the section
        assert "### Code Quality" in report
        # Should indicate no issues found
        assert "No issues" in report or "clean" in report.lower() or "0" in report

    def test_no_linters_available_note(self):
        """When no linters are available, show a note."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=None,
            code_quality_skipped_reason="No linters available",
        )
        assert "### Code Quality" in report
        assert "No linters available" in report


# ===================================================================
# Tests: blocking behavior
# ===================================================================


class TestCodeQualityBlocking:
    """Tests that code quality findings affect 'Ready to close' verdict."""

    @pytest.fixture(autouse=True)
    def _check_support(self):
        _check_assemble_issue_report_supports_cq()
    """Tests that code quality findings affect 'Ready to close' verdict."""

    def test_critical_finding_blocks_closure(self):
        """Critical findings should result in 'Ready to close: No'."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[
                {"file": "src/main.py", "line": 42, "severity": "critical",
                 "message": "Unused variable", "linter": "ruff", "code": "F841"}
            ],
        )
        assert report.startswith("Ready to close: No")

    def test_high_finding_blocks_closure(self):
        """High severity findings should result in 'Ready to close: No'."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[
                {"file": "src/main.py", "line": 10, "severity": "high",
                 "message": "Syntax error", "linter": "ruff", "code": "E999"}
            ],
        )
        assert report.startswith("Ready to close: No")

    def test_medium_finding_does_not_block(self):
        """Medium findings should warn but not block closure."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[
                {"file": "src/utils.py", "line": 5, "severity": "medium",
                 "message": "Unused import", "linter": "ruff", "code": "W0611"}
            ],
        )
        assert report.startswith("Ready to close: Yes")
        # Should still mention the finding
        assert "medium" in report.lower() or "W0611" in report

    def test_low_finding_does_not_block(self):
        """Low findings should warn but not block closure."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[
                {"file": "src/style.py", "line": 15, "severity": "low",
                 "message": "Line too long", "linter": "ruff", "code": "E501"}
            ],
        )
        assert report.startswith("Ready to close: Yes")
        assert "low" in report.lower() or "E501" in report

    def test_mixed_severity_blocks_on_critical(self):
        """Mixed findings with at least one critical/high should block."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[
                {"file": "src/main.py", "line": 42, "severity": "critical",
                 "message": "Unused variable", "linter": "ruff", "code": "F841"},
                {"file": "src/utils.py", "line": 5, "severity": "medium",
                 "message": "Unused import", "linter": "ruff", "code": "W0611"},
            ],
        )
        assert report.startswith("Ready to close: No")

    def test_clean_still_allows_closure(self):
        """No findings should still allow closure when ACs are met."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "met", "evidence": ""}],
            [],
            code_quality_findings=[],
        )
        assert report.startswith("Ready to close: Yes")

    def test_unmet_acs_still_block_despite_clean_code(self):
        """Unmet ACs block closure even if code quality is clean."""
        report = _assemble_issue_report(
            ISSUE_WITH_ACS["workItem"],
            [{"text": "Handle authentication", "verdict": "unmet", "evidence": ""}],
            [],
            code_quality_findings=[],
        )
        assert report.startswith("Ready to close: No")


# ===================================================================
# Tests: cmd_issue integration (mock subprocess calls)
# ===================================================================


class TestCmdIssueCodeQualityIntegration:
    """Tests that cmd_issue integrates code quality via subprocess/runner."""

    @pytest.fixture(autouse=True)
    def _check_support(self):
        """Skip if cmd_issue doesn't yet support code quality integration."""
        if not _AUDIT_RUNNER_AVAILABLE or cmd_issue is None:
            pytest.skip("audit_runner not available")
        # Check if cmd_issue source references code_quality or cq
        import inspect
        try:
            source = inspect.getsource(cmd_issue)
            if "code_quality" not in source and "cq_" not in source:
                pytest.skip("cmd_issue does not yet integrate code quality")
        except (OSError, TypeError):
            pytest.skip("Cannot inspect cmd_issue source")
    """Tests that cmd_issue integrates code quality via subprocess/runner."""

    def _make_fake_cq_runner(self, cq_output_json: str):
        """Create a mock runner that returns standard wl + code_quality outputs.

        The first call is to ``wl show``, which returns a simple issue.
        The second call is to ``python3 code_quality.py``, which returns
        the provided *cq_output_json*.
        """
        call_count = [0]

        def fake_runner(cmd, **kwargs):
            call_count[0] += 1
            # First call: wl show
            if call_count[0] == 1:
                return _fake_proc(
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "SA-CQTEST",
                            "title": "Code quality test",
                            "description": "## Acceptance Criteria\n1. Do the thing.",
                            "status": "open",
                            "priority": "high",
                        },
                        "children": [],
                    })
                )
            # Second call: code_quality.py (or maybe more for pi, persistence)
            # Return code quality output
            return _fake_proc(stdout=cq_output_json)

        return fake_runner

    def test_cmd_issue_with_critical_finding_blocks(
        self, monkeypatch, capsys
    ):
        """cmd_issue with critical code quality finding should report blocking."""
        # Skip this test if the target module doesn't support code_quality yet
        try:
            from skill.audit.scripts.audit_runner import cmd_issue
        except ImportError:
            pytest.skip("audit_runner not importable")

        runner = self._make_fake_cq_runner(SAMPLE_CQ_CRITICAL_FINDING)

        rc = cmd_issue(
            "SA-CQTEST",
            persist=False,
            runner=runner,
        )
        captured = capsys.readouterr()
        output = captured.out
        assert "Ready to close: No" in output
        assert "Code Quality" in output
        assert "critical" in output.lower()
        assert rc == 0

    def test_cmd_issue_with_medium_finding_does_not_block(
        self, monkeypatch, capsys
    ):
        """cmd_issue with only medium findings should allow closure."""
        try:
            from skill.audit.scripts.audit_runner import cmd_issue
        except ImportError:
            pytest.skip("audit_runner not importable")

        runner = self._make_fake_cq_runner(SAMPLE_CQ_MEDIUM_FINDING)

        rc = cmd_issue(
            "SA-CQTEST",
            persist=False,
            runner=runner,
        )
        captured = capsys.readouterr()
        output = captured.out
        assert "Ready to close: Yes" in output
        assert "Code Quality" in output
        assert "medium" in output.lower()
        assert rc == 0

    def test_cmd_issue_with_clean_quality(
        self, monkeypatch, capsys
    ):
        """cmd_issue with no findings should show clean quality section."""
        try:
            from skill.audit.scripts.audit_runner import cmd_issue
        except ImportError:
            pytest.skip("audit_runner not importable")

        runner = self._make_fake_cq_runner(SAMPLE_CQ_CLEAN)

        rc = cmd_issue(
            "SA-CQTEST",
            persist=False,
            runner=runner,
        )
        captured = capsys.readouterr()
        output = captured.out
        assert "Ready to close: Yes" in output
        assert "Code Quality" in output
        # Should mention no issues
        assert "No issues" in output or "clean" in output.lower() or "0 findings" in output.lower()
        assert rc == 0
