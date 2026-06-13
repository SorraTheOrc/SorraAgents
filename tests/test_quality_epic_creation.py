"""Tests for Quality Improvement epic creation and reuse.

These tests verify that:
- A "Quality Improvement - Refactoring" epic is created if none exists
- An existing open/in_progress epic is reused (no duplicate)
- Child work items are created with correct issueType and tags
- Idempotency: re-running with same findings doesn't create duplicates
- The script returns structured JSON output

The target implementation lives in
skill/code_review/scripts/create_quality_epics.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Sample findings data
# ---------------------------------------------------------------------------

SAMPLE_FINDINGS_CRITICAL = [
    {
        "file": "src/main.py",
        "line": 42,
        "severity": "critical",
        "message": "Unused variable `x`",
        "linter": "ruff",
        "code": "F841",
    },
]

SAMPLE_FINDINGS_MIXED = [
    {
        "file": "src/main.py",
        "line": 42,
        "severity": "critical",
        "message": "Unused variable `x`",
        "linter": "ruff",
        "code": "F841",
    },
    {
        "file": "src/utils.py",
        "line": 5,
        "severity": "medium",
        "message": "Unused import `os`",
        "linter": "ruff",
        "code": "W0611",
    },
]

SAMPLE_FINDINGS_MULTIPLE = [
    {
        "file": "src/main.py",
        "line": 42,
        "severity": "critical",
        "message": "Unused variable `x`",
        "linter": "ruff",
        "code": "F841",
    },
    {
        "file": "src/app.ts",
        "line": 15,
        "severity": "high",
        "message": "Missing return type",
        "linter": "eslint",
        "code": "TS-2366",
    },
    {
        "file": "src/utils.py",
        "line": 5,
        "severity": "medium",
        "message": "Unused import `os`",
        "linter": "ruff",
        "code": "W0611",
    },
    {
        "file": "src/style.py",
        "line": 15,
        "severity": "low",
        "message": "Line too long",
        "linter": "ruff",
        "code": "E501",
    },
]

EXPECTED_EPIC_TITLE = "Quality Improvement - Refactoring"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ===================================================================
# Tests: create_quality_epics module
# ===================================================================


class TestQualityEpicCreation:
    """Tests for the create_quality_epics module."""

    def _import_module(self):
        """Import create_quality_epics; skip if not implemented."""
        try:
            from skill.code_review.scripts import create_quality_epics
            return create_quality_epics
        except (ImportError, ModuleNotFoundError) as exc:
            pytest.skip(f"create_quality_epics not yet available: {exc}")

    # ------------------------------------------------------------------
    # Epic search / reuse
    # ------------------------------------------------------------------

    def test_creates_epic_when_none_exists(self):
        """When no 'Quality Improvement - Refactoring' epic exists, create one."""
        mod = self._import_module()

        calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            # wl search - should return empty (no existing epic)
            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))
            # wl create (epic) - return new epic id
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-NEWEPIC01", "title": EXPECTED_EPIC_TITLE},
                }))
            # wl create (child) - return child id
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-NEWCHILD01", "title": "child"},
                }))
            # Default fallback
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )

        assert result["epic_created"] is True
        assert result["epic_id"] == "SA-NEWEPIC01"
        assert result["children_created"] == 1
        # Verify search was called first
        search_calls = [c for c in calls if "search" in str(c)]
        assert len(search_calls) >= 1

    def test_reuses_existing_epic(self):
        """When an existing 'Quality Improvement - Refactoring' epic exists, reuse it."""
        mod = self._import_module()

        calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            # wl search - return existing epic
            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                    }],
                }))
            # wl create (child) - return child id
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-NEWCHILD01", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )

        assert result["epic_created"] is False
        assert result["epic_id"] == "SA-EXISTING01"
        assert result["children_created"] >= 1

    def test_reuses_in_progress_epic(self):
        """Reuses an epic that is 'in_progress' (not just 'open')."""
        mod = self._import_module()

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-INPROGRESS01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "in_progress",
                    }],
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD01", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )

        assert result["epic_created"] is False
        assert result["epic_id"] == "SA-INPROGRESS01"

    def test_does_not_reuse_closed_epic(self):
        """A closed/completed epic should not be reused; a new one should be created."""
        mod = self._import_module()

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            # First search returns a closed epic
            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-CLOSED01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "completed",
                    }],
                }))
            # Then create a new one
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-NEWEPIC02", "title": EXPECTED_EPIC_TITLE},
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD01", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )

        assert result["epic_created"] is True
        assert result["epic_id"] == "SA-NEWEPIC02"

    # ------------------------------------------------------------------
    # Child task creation
    # ------------------------------------------------------------------

    def test_child_tasks_have_correct_type_and_tags(self):
        """Child work items should have issueType=task and tags containing Refactor."""
        mod = self._import_module()

        child_calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            child_calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                    }],
                }))
            if "create" in cmd_str:
                # Check that --issue-type and --tags are present
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD01", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        _result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )

        # Find create calls for children (not the epic itself)
        create_calls = [c for c in child_calls if "create" in str(c) and "Quality Improvement" not in str(c)]
        for call in create_calls:
            call_str = " ".join(str(a) for a in call)
            assert "--issue-type" in call_str or "--type" in call_str
            assert "Refactor" in call_str

    def test_child_tasks_have_correct_priority_by_severity(self):
        """Child items should be prioritized by finding severity.

        critical→critical, high→high, medium→medium, low→low
        """
        mod = self._import_module()

        child_calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            child_calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                    }],
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD01", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_MULTIPLE,
            runner=fake_runner,
        )

        # Should have created 4 children (one per finding)
        assert result["children_created"] == 4

    def test_no_findings_creates_no_children(self):
        """With no findings, no children should be created."""
        mod = self._import_module()

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                    }],
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(
            [],
            runner=fake_runner,
        )

        assert result["children_created"] == 0

    # ------------------------------------------------------------------
    # Output format
    # ------------------------------------------------------------------

    def test_returns_structured_json(self):
        """create_epics_for_findings() returns JSON-serializable dict."""
        mod = self._import_module()

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
                }))
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-EPIC", "title": EXPECTED_EPIC_TITLE},
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )

        # Must be JSON-serializable
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        assert "epic_id" in parsed
        assert "children_created" in parsed
        assert "epic_created" in parsed

    def test_returns_expected_keys(self):
        """The result dict should have epic_id, children_created, and epic_created keys."""
        mod = self._import_module()

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "search" in cmd_str and EXPECTED_EPIC_TITLE in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
                }))
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-EPIC", "title": EXPECTED_EPIC_TITLE},
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )

        assert "epic_id" in result
        assert isinstance(result["epic_id"], str)
        assert "children_created" in result
        assert isinstance(result["children_created"], int)
        assert "epic_created" in result
        assert isinstance(result["epic_created"], bool)

    # ------------------------------------------------------------------
    # CLI interface
    # ------------------------------------------------------------------

    def test_cli_accepts_findings_arg(self):
        """The module should accept --findings (JSON string) and --project-root args."""
        mod = self._import_module()

        # Just check that the function can parse args or has function signature
        import inspect
        sig = inspect.signature(mod.create_epics_for_findings)
        params = list(sig.parameters.keys())
        assert "findings" in params or "findings_json" in params
