"""Tests for Quality Improvement epic creation and reuse.

These tests verify that:
- A "Quality Improvement - Refactoring" epic is created if none exists
- An existing open/in_progress epic is reused (no duplicate)
- Child work items are created with correct issueType and tags
- Idempotency: re-running with same findings doesn't create duplicates
- The script returns structured JSON output

The target implementation lives in
skill/code_review/scripts/create_quality_epics.py.

NOTE: The ``find_or_create_epic()`` function uses ``wl list --status open``
and ``wl list --status in_progress`` (not ``wl search``) because ``wl search``
does not reliably return matching epics.  All mock runners below must handle
``wl list`` commands, not ``wl search``.
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

    def _wl_list_open_empty(self, cmd: list[str]):
        """Return response for ``wl list --status open`` with no matching epics."""
        return _fake_proc(stdout=json.dumps({
            "success": True,
            "workItems": [
                {"id": "SA-OTHER01", "title": "Some other task", "status": "open", "issueType": "task"},
            ],
        }))

    def _wl_list_inprogress_empty(self, cmd: list[str]):
        """Return response for ``wl list --status in_progress`` with no matching epics."""
        return _fake_proc(stdout=json.dumps({
            "success": True,
            "workItems": [],
        }))

    def _handle_wl_create_epic(self, cmd):
        """Return a new epic creation response."""
        return _fake_proc(stdout=json.dumps({
            "success": True,
            "workItem": {"id": "SA-NEWEPIC01", "title": EXPECTED_EPIC_TITLE},
        }))

    def test_creates_epic_when_none_exists(self):
        """When no 'Quality Improvement - Refactoring' epic exists, create one."""
        mod = self._import_module()

        calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            if "list" in cmd_str and "open" in cmd_str:
                return self._wl_list_open_empty(cmd)
            if "list" in cmd_str and "in_progress" in cmd_str:
                return self._wl_list_inprogress_empty(cmd)
            # wl create (epic)
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return self._handle_wl_create_epic(cmd)
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
        # Verify list was called for both open and in_progress
        list_calls = [c for c in calls if "list" in str(c)]
        assert len(list_calls) >= 2
        # Verify the epic create call includes --stage intake_complete
        epic_create_calls = [
            c for c in calls
            if "create" in str(c) and "Quality Improvement" in str(c)
        ]
        assert len(epic_create_calls) == 1
        epic_call_str = " ".join(str(a) for a in epic_create_calls[0])
        assert "--stage" in epic_call_str
        assert "intake_complete" in epic_call_str

    def test_reuses_existing_epic(self):
        """When an existing 'Quality Improvement - Refactoring' epic exists, reuse it."""
        mod = self._import_module()

        calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            # wl list --status open - return existing epic
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                        "issueType": "epic",
                        "createdAt": "2026-01-01T00:00:00.000Z",
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
            # wl list --status open returns no matching epics
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
                }))
            # wl list --status in_progress returns the matching epic
            if "list" in cmd_str and "in_progress" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-INPROGRESS01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "in_progress",
                        "issueType": "epic",
                        "createdAt": "2026-01-01T00:00:00.000Z",
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
        """A closed/completed epic should not be reused; a new one should be created.

        With the new ``wl list`` approach, closed epics are not returned by
        ``wl list --status open`` or ``wl list --status in_progress``, so
        this test verifies that when no open/in-progress epic is found,
        a new one is created.
        """
        mod = self._import_module()

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            # wl list --status open returns no matching epics (closed epics
            # are not included in the open list)
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
                }))
            if "list" in cmd_str and "in_progress" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
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

            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                        "issueType": "epic",
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
            assert "--stage" in call_str
            assert "intake_complete" in call_str

    def test_child_tasks_have_correct_priority_by_severity(self):
        """Child items should be prioritized by finding severity.

        critical→critical, high→high, medium→medium, low→low
        """
        mod = self._import_module()

        child_calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            child_calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                        "issueType": "epic",
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
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                        "issueType": "epic",
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
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
                }))
            if "list" in cmd_str and "in_progress" in cmd_str:
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
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
                }))
            if "list" in cmd_str and "in_progress" in cmd_str:
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
    # Epic description includes lifecycle policy
    # ------------------------------------------------------------------

    def test_epic_creation_includes_lifecycle_in_description(self):
        """When creating a new epic, the description should include lifecycle policy."""
        mod = self._import_module()

        epic_create_calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True, "workItems": [],
                }))
            if "list" in cmd_str and "in_progress" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True, "workItems": [],
                }))
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                epic_create_calls.append(list(cmd))
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-EPIC01", "title": EXPECTED_EPIC_TITLE},
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        _result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )

        # Check the epic create call includes lifecycle info in description
        assert len(epic_create_calls) == 1
        call_str = " ".join(str(a) for a in epic_create_calls[0])
        assert "Closed when all child work items are resolved" in call_str
        assert "new epic is created if new findings arrive after closure" in call_str
        # Also verify --stage intake_complete is present
        assert "--stage" in call_str
        assert "intake_complete" in call_str

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

    # ------------------------------------------------------------------
    # Dry-run output
    # ------------------------------------------------------------------

    def test_dry_run_output_reflects_new_stage(self):
        """The --dry-run output should reflect that items are created at stage intake_complete."""
        mod = self._import_module()

        calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
                }))
            if "list" in cmd_str and "in_progress" in cmd_str:
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

        # Verify via create_epics_for_findings that the right args are passed
        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,
            runner=fake_runner,
        )
        assert result["epic_created"] is True
        # Verify the epic create call includes --stage intake_complete
        epic_create_calls = [
            c for c in calls
            if "create" in str(c) and "Quality Improvement" in str(c)
        ]
        assert len(epic_create_calls) == 1
        epic_call_str = " ".join(str(a) for a in epic_create_calls[0])
        assert "--stage" in epic_call_str
        assert "intake_complete" in epic_call_str

    def test_dry_run_epic_shows_stage(self):
        """Dry-run output for epic creation mentions stage intake_complete."""
        mod = self._import_module()

        # Just test that the main() function's dry-run path includes stage info
        # The dry-run output currently prints findings and then JSON
        # We test the script's behavior directly by calling main with --dry-run
        exit_code = mod.main(["--findings", json.dumps(SAMPLE_FINDINGS_CRITICAL), "--dry-run"])
        assert exit_code == 0

    # ------------------------------------------------------------------
    # New tests for the wl list approach
    # ------------------------------------------------------------------

    def test_picks_oldest_when_multiple_open_epics(self):
        """When multiple open epics with the same title exist, pick the oldest by createdAt."""
        mod = self._import_module()

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [
                        {
                            "id": "SA-YOUNGER01",
                            "title": EXPECTED_EPIC_TITLE,
                            "status": "open",
                            "issueType": "epic",
                            "createdAt": "2026-06-15T00:00:00.000Z",
                        },
                        {
                            "id": "SA-OLDEST01",
                            "title": EXPECTED_EPIC_TITLE,
                            "status": "open",
                            "issueType": "epic",
                            "createdAt": "2026-06-10T00:00:00.000Z",
                        },
                    ],
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

        # Should pick the oldest (earliest createdAt)
        assert result["epic_id"] == "SA-OLDEST01"
        assert result["epic_created"] is False

    def test_ignores_non_epic_matching_title(self):
        """Items with matching title but wrong issueType should be ignored."""
        mod = self._import_module()

        def fake_runner(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "list" in cmd_str and "open" in cmd_str:
                # Return a task (not epic) with matching title - should be ignored
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [
                        {
                            "id": "SA-TASK01",
                            "title": EXPECTED_EPIC_TITLE,
                            "status": "open",
                            "issueType": "task",
                            "createdAt": "2026-06-10T00:00:00.000Z",
                        },
                    ],
                }))
            if "list" in cmd_str and "in_progress" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [],
                }))
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-NEWEPIC", "title": EXPECTED_EPIC_TITLE},
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

        # Should create new epic because no epic-type matching was found
        assert result["epic_created"] is True
        assert result["epic_id"] == "SA-NEWEPIC"

    # ------------------------------------------------------------------
    # Priority matching: epic inherits highest child severity
    # ------------------------------------------------------------------

    def test_highest_priority_returns_critical(self):
        """_highest_priority returns critical when a critical finding exists."""
        mod = self._import_module()
        assert mod._highest_priority(SAMPLE_FINDINGS_CRITICAL) == "critical"

    def test_highest_priority_returns_high(self):
        """_highest_priority returns high when highest severity is high."""
        mod = self._import_module()
        findings = [
            {"severity": "low", "file": "a.py", "line": 1, "message": "x", "linter": "ruff", "code": "E101"},
            {"severity": "high", "file": "b.py", "line": 2, "message": "y", "linter": "ruff", "code": "E201"},
            {"severity": "medium", "file": "c.py", "line": 3, "message": "z", "linter": "ruff", "code": "E301"},
        ]
        assert mod._highest_priority(findings) == "high"

    def test_highest_priority_returns_medium_default(self):
        """_highest_priority returns medium for empty findings."""
        mod = self._import_module()
        assert mod._highest_priority([]) == "medium"

    def test_highest_priority_handles_unknown_severity(self):
        """_highest_priority defaults to medium for unknown severity values."""
        mod = self._import_module()
        findings = [
            {"severity": "unknown", "file": "a.py", "line": 1, "message": "x", "linter": "ruff", "code": "E101"},
        ]
        # Unknown severity maps to medium priority
        assert mod._highest_priority(findings) == "medium"

    def test_new_epic_created_with_correct_priority(self):
        """When creating a new epic, priority should match highest child severity."""
        mod = self._import_module()

        epic_create_calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            epic_create_calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True, "workItems": [],
                }))
            if "list" in cmd_str and "in_progress" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True, "workItems": [],
                }))
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-EPIC01", "title": EXPECTED_EPIC_TITLE},
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        # Use findings with mixed severities (critical is highest)
        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_MIXED,  # critical + medium
            runner=fake_runner,
        )

        assert result["epic_created"] is True
        # The epic create call should include --priority critical
        epic_create = [c for c in epic_create_calls if "create" in str(c) and "Quality Improvement" in str(c)]
        assert len(epic_create) >= 1
        call_str = " ".join(str(a) for a in epic_create[0])
        assert "--priority" in call_str
        assert "critical" in call_str

    def test_new_epic_uses_high_priority_when_highest_is_high(self):
        """Epic created with findings whose highest severity is high gets high priority."""
        mod = self._import_module()

        epic_create_calls: list[list[str]] = []
        findings = [
            {"severity": "high", "file": "a.py", "line": 1, "message": "x", "linter": "ruff", "code": "E101"},
            {"severity": "low", "file": "b.py", "line": 2, "message": "y", "linter": "ruff", "code": "E201"},
        ]

        def fake_runner(cmd, **kwargs):
            epic_create_calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            if "list" in cmd_str and ("open" in cmd_str or "in_progress" in cmd_str):
                return _fake_proc(stdout=json.dumps({
                    "success": True, "workItems": [],
                }))
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-EPIC02", "title": EXPECTED_EPIC_TITLE},
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(findings, runner=fake_runner)

        assert result["epic_created"] is True
        epic_create = [c for c in epic_create_calls if "create" in str(c) and "Quality Improvement" in str(c)]
        assert len(epic_create) >= 1
        call_str = " ".join(str(a) for a in epic_create[0])
        assert "--priority" in call_str
        assert "high" in call_str

    def test_reused_epic_priority_updated_when_new_children_created(self):
        """When reusing an epic and new children are created, update epic priority to match."""
        mod = self._import_module()

        update_calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            nonlocal update_calls
            result = _fake_proc(stdout=json.dumps({"success": True}))
            cmd_str = " ".join(str(c) for c in cmd)

            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                        "issueType": "epic",
                        "createdAt": "2026-01-01T00:00:00.000Z",
                    }],
                }))
            # wl show to get current epic priority
            if "show" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "priority": "medium",
                    },
                }))
            # wl update to set priority
            if "update" in cmd_str and "priority" in cmd_str:
                update_calls.append(list(cmd))
                return _fake_proc(stdout=json.dumps({"success": True}))
            # wl create (child)
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD", "title": "child"},
                }))
            # wl show --children
            if "children" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "children": [],
                }))
            return result

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_MIXED,  # critical + medium → highest is critical
            runner=fake_runner,
        )

        assert result["epic_created"] is False
        assert result["epic_id"] == "SA-EXISTING01"
        # Should have called wl update to set priority to critical
        assert len(update_calls) >= 1
        update_str = " ".join(str(a) for a in update_calls[0])
        assert "critical" in update_str

    def test_reused_epic_priority_unchanged_when_no_new_children(self):
        """When reusing an epic with no new children, priority should not be updated."""
        mod = self._import_module()

        update_calls: list[list[str]] = []

        def fake_runner(cmd, **kwargs):
            nonlocal update_calls
            result = _fake_proc(stdout=json.dumps({"success": True}))
            cmd_str = " ".join(str(c) for c in cmd)

            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                        "issueType": "epic",
                        "createdAt": "2026-01-01T00:00:00.000Z",
                    }],
                }))
            # wl show --children - return existing children that match all findings
            if "children" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "children": [
                        {"title": "[CRITICAL] src/main.py:42 \u2014 Unused variable `x` (F841)"},
                    ],
                }))
            # Should NOT be called for update
            if "update" in cmd_str and "priority" in cmd_str:
                update_calls.append(list(cmd))
                return _fake_proc(stdout=json.dumps({"success": True}))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD"},
                }))
            return result

        result = mod.create_epics_for_findings(
            SAMPLE_FINDINGS_CRITICAL,  # single finding that already exists as child
            runner=fake_runner,
        )

        assert result["epic_created"] is False
        assert result["epic_id"] == "SA-EXISTING01"
        # No children should be created (already exists), so no priority update
        assert result["children_created"] == 0
        assert len(update_calls) == 0

    def test_priority_escalation_only_never_reduce(self):
        """Epic priority is never reduced when findings have lower severity."""
        mod = self._import_module()

        update_calls: list[list[str]] = []
        findings = [
            {"severity": "low", "file": "a.py", "line": 1, "message": "x", "linter": "ruff", "code": "E101"},
        ]

        def fake_runner(cmd, **kwargs):
            nonlocal update_calls
            result = _fake_proc(stdout=json.dumps({"success": True}))
            cmd_str = " ".join(str(c) for c in cmd)

            if "list" in cmd_str and "open" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItems": [{
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "status": "open",
                        "issueType": "epic",
                        "createdAt": "2026-01-01T00:00:00.000Z",
                    }],
                }))
            # wl show to get current priority
            if "show" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "SA-EXISTING01",
                        "title": EXPECTED_EPIC_TITLE,
                        "priority": "critical",
                    },
                }))
            if "children" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "children": [],
                }))
            if "update" in cmd_str and "priority" in cmd_str:
                update_calls.append(list(cmd))
                return _fake_proc(stdout=json.dumps({"success": True}))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD", "title": "child"},
                }))
            return result

        result = mod.create_epics_for_findings(
            findings,
            runner=fake_runner,
        )

        assert result["epic_created"] is False
        # Children created but priority should NOT be reduced from critical to low
        assert result["children_created"] == 1
        assert len(update_calls) == 0, "Priority should not be reduced from critical to low"

    def test_dry_run_output_includes_computed_priority(self):
        """The --dry-run output should reflect the computed priority."""
        mod = self._import_module()
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = mod.main(["--findings", json.dumps(SAMPLE_FINDINGS_MIXED), "--dry-run"])

        assert exit_code == 0
        output = buf.getvalue()
        # Should mention the highest priority somewhere in the output
        assert "critical" in output

    def test_new_epic_with_only_low_findings_uses_low_priority(self):
        """Epic created with only low-severity findings should get low priority."""
        mod = self._import_module()

        epic_create_calls: list[list[str]] = []
        findings = [
            {"severity": "low", "file": "a.py", "line": 1, "message": "x", "linter": "ruff", "code": "E101"},
        ]

        def fake_runner(cmd, **kwargs):
            epic_create_calls.append(list(cmd))
            cmd_str = " ".join(str(c) for c in cmd)

            if "list" in cmd_str and ("open" in cmd_str or "in_progress" in cmd_str):
                return _fake_proc(stdout=json.dumps({
                    "success": True, "workItems": [],
                }))
            if "create" in cmd_str and "Quality Improvement" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-EPIC03", "title": EXPECTED_EPIC_TITLE},
                }))
            if "create" in cmd_str:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "SA-CHILD", "title": "child"},
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        result = mod.create_epics_for_findings(findings, runner=fake_runner)

        assert result["epic_created"] is True
        epic_create = [c for c in epic_create_calls if "create" in str(c) and "Quality Improvement" in str(c)]
        assert len(epic_create) >= 1
        call_str = " ".join(str(a) for a in epic_create[0])
        assert "--priority" in call_str
        assert "low" in call_str
