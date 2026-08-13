"""V3 tests: safety & edge cases for implement.py parent recursion
(SA-0MSQBM2FK005NW1T, SA-0MSQIJC6U0017QIG).

Contract (per work item ACs):

- AC-6: per-child abort/failure resets that child to `open` (StatusLifecycle
  abort semantics), stops the chain with a clear report of what completed and
  what failed; already-completed siblings are not regressed or re-implemented.
  No orphaned `in_progress` states left behind.
- AC-8: tests cover (c) mixed open/blocked/terminal children → correct
  ordering and no re-implementation of terminal children.
- Constraints: worktree isolation per child must be preserved (each child
  implemented in its own worktree, never the main checkout; sequential child
  implementations reuse/rotate worktrees); the parent itself gets no
  worktree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py)."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_parent_v3", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_parent_v3"] = mod
    spec.loader.exec_module(mod)
    return mod


def _child(
    work_item_id: str, status: str = "open", assignee: str = "", sort_index: int = 1000
) -> dict:
    return {
        "id": work_item_id,
        "title": f"Child {work_item_id}",
        "status": status,
        "stage": "intake_complete",
        "assignee": assignee,
        "priority": "high",
        "sortIndex": sort_index,
    }


class ParentHarness:
    """Canned wl/phase plumbing for phase_parent scenarios."""

    def __init__(self, mod, children, *, blockers_map=None, phase_start_results=None):
        self.mod = mod
        self.children = children
        self.blockers_map = blockers_map or {}
        # phase_start_results: dict child_id → result dict (default success)
        self.phase_start_results = phase_start_results or {}
        self.calls = {"phase_start": [], "update_status": [], "comments": [], "abort": []}

    def _fake_phase_start(self, child_id, **kwargs):
        self.calls["phase_start"].append(child_id)
        result = dict(
            self.phase_start_results.get(
                child_id,
                {
                    "success": True,
                    "work_item_id": child_id,
                    "worktree_path": f"/wt/{child_id}",
                    "branch": f"wl-{child_id}",
                    "message": "Worktree created",
                },
            )
        )
        result["work_item_id"] = child_id
        return result

    def _fake_update_status(self, work_item_id, status, stage=None, assignee=None, **kwargs):
        self.calls["update_status"].append((work_item_id, status, stage, assignee))
        return {"success": True, "workItem": {"id": work_item_id, "status": status}}

    def _fake_add_comment(self, work_item_id, comment):
        self.calls["comments"].append((work_item_id, comment))
        return True

    def _fake_wl_dep_blockers(self, child_id, **_):
        return list(self.blockers_map.get(child_id, []))

    def run(self, parent_id: str = "SA-PARENT001") -> dict:
        with (
            mock.patch.object(self.mod, "wl_show", return_value={"id": parent_id, "title": "P"}),
            mock.patch.object(self.mod, "wl_show_children", return_value=self.children),
            mock.patch.object(self.mod, "wl_dep_blockers", side_effect=self._fake_wl_dep_blockers),
            mock.patch.object(self.mod, "phase_start", side_effect=self._fake_phase_start),
            mock.patch.object(
                self.mod.StatusLifecycle, "update_status", side_effect=self._fake_update_status
            ),
            mock.patch.object(self.mod, "wl_add_comment", side_effect=self._fake_add_comment),
            mock.patch.object(self.mod, "is_code_freeze_active", return_value=False),
        ):
            report = self.mod.phase_parent(parent_id, json_output=True)
        report["_calls"] = self.calls
        return report


# ===========================================================================
# AC-8c: mixed open/blocked/terminal → correct ordering, no re-implementation
# ===========================================================================


class TestMixedStatuses:
    def test_mixed_open_blocked_terminal_picks_first_startable(self, implement_mod):
        """Terminal children are never re-implemented; among the remaining
        children the first startable one (dependency-aware) is picked."""
        children = [
            _child("SA-T1", status="completed", sort_index=1),
            _child("SA-T2", status="in_review", sort_index=2),
            _child("SA-B", status="blocked", sort_index=3),   # blocked by T1 (terminal)
            _child("SA-O", status="open", sort_index=4),      # independent sibling
        ]
        blockers_map = {"SA-B": [{"id": "SA-T1", "direction": "depends-on"}]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers_map)
        report = h.run()

        assert report["success"] is True
        started = h.calls["phase_start"]
        assert "SA-T1" not in started
        assert "SA-T2" not in started
        # SA-B's blocker (SA-T1) is terminal → SA-B is startable and comes
        # before the independent sibling SA-O in dependency order.
        assert started == ["SA-B"]
        # parent not advanced (children remain)
        assert report.get("parent_advanced") is None or report.get("parent_advanced") is False

    def test_terminal_children_never_re_implemented_after_advance(self, implement_mod):
        """Once every child is terminal the parent advances and NO child is
        started — nothing is re-implemented."""
        children = [
            _child("SA-T1", status="completed"),
            _child("SA-T2", status="in_review"),
        ]
        h = ParentHarness(implement_mod, children)
        report = h.run()

        assert report["success"] is True
        assert report.get("parent_advanced") is True
        assert h.calls["phase_start"] == []
        assert h.calls["update_status"] == [("SA-PARENT001", "completed", "in_review", None)]

    def test_no_worktree_created_for_the_parent_itself(self, implement_mod):
        """The parent action orchestrates children only — phase_start is
        called per child, never for the parent id itself."""
        children = [_child("SA-C1"), _child("SA-C2")]
        h = ParentHarness(implement_mod, children)
        h.run()
        assert h.calls["phase_start"] == ["SA-C1"]
        assert "SA-PARENT001" not in h.calls["phase_start"]


# ===========================================================================
# AC-6: per-child abort/failure stops the chain with a report
# ===========================================================================


class TestAbortAndFailure:
    def test_failed_child_start_reports_completed_and_failed(self, implement_mod):
        """When starting a child fails, the chain stops with a clear report
        of what completed (terminal children) and what failed; already
        completed siblings are untouched (not reset, not re-implemented)."""
        children = [
            _child("SA-T1", status="completed", sort_index=1),
            _child("SA-F", status="open", sort_index=2),
            _child("SA-C2", status="open", sort_index=3),
        ]
        h = ParentHarness(
            implement_mod,
            children,
            phase_start_results={"SA-F": {"success": False, "message": "dirty tree"}},
        )
        report = h.run()

        assert report["success"] is False
        assert h.calls["phase_start"] == ["SA-F"]
        # The failing child is reported; the chain stops (SA-C2 not started).
        assert "SA-F" in report.get("message", "")
        assert "SA-C2" not in h.calls["phase_start"]
        # No parent status transition on failure.
        assert h.calls["update_status"] == []

    def test_in_progress_sibling_skipped_never_clobbered(self, implement_mod):
        """A child in progress by another agent is skipped and reported;
        the parent is not advanced and no orphaned transition occurs."""
        children = [
            _child("SA-P", status="in_progress", assignee="other-agent"),
            _child("SA-T", status="completed"),
        ]
        h = ParentHarness(implement_mod, children)
        report = h.run()

        assert report["success"] is True
        assert h.calls["phase_start"] == []
        assert h.calls["update_status"] == []
        assert report.get("parent_advanced") is None or report.get("parent_advanced") is False
        actions = {c["id"]: c["action"] for c in report.get("children", [])}
        assert actions["SA-P"] == "skip-in-progress"
        assert actions["SA-T"] == "skip-terminal"

    def test_abort_path_resets_child_to_open_via_status_lifecycle(self, implement_mod):
        """The abort phase (implement.py abort <child>) resets the child to
        open — the per-child abort semantics AC-6 relies on. phase_abort
        calls StatusLifecycle.update_status(open) for the child."""
        calls = []

        def fake_update_status(work_item_id, status, stage=None, assignee=None, **kwargs):
            calls.append((work_item_id, status))
            return {"success": True, "workItem": {"id": work_item_id, "status": status}}

        with (
            mock.patch.object(
                implement_mod.StatusLifecycle, "update_status", side_effect=fake_update_status
            ),
            mock.patch.object(implement_mod, "wl_add_comment", return_value=True),
            mock.patch.object(implement_mod, "cleanup_worktree_processes", return_value={}),
            mock.patch.object(implement_mod, "_discover_worktree", return_value=None),
            mock.patch.object(implement_mod, "_restore_repo_state", return_value=None),
        ):
            report = implement_mod.phase_abort("SA-CHILD-X", json_output=True)

        assert report["success"] is True
        assert ("SA-CHILD-X", "open") in calls
