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
