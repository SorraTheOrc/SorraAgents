"""Tests for implement.py parent-recursion (SA-0MSQBM2FK005NW1T, V1+).

Contract (per work item ACs):

- AC1: invoking the implement skill on a parent item with non-terminal
  children automatically implements the children (+ blockers) in dependency
  order, without manual per-child invocations.
- AC4: once all children reach a terminal state
  (`in_review`/`completed`/`done`), the parent is advanced to
  `completed`/`in_review` (existing Step 5.1 advancement retained).
- AC5: the dead-end path ("set open + comment + return control") no longer
  fires for a normal parent invocation; a parent with no children or
  all-terminal children behaves as today.
- AC7: a child already claimed/in_progress by another agent is skipped or
  reported, never clobbered.
- AC8: tests cover (a) parent with open children → all children implemented
  + parent advanced; (e) leaf item → unchanged current behavior.

This file covers the V1 happy path plus the advancement/skip guarantees;
dependency-graph ordering and cycle detection are covered by V2 tests in the
same file (added by SA-0MSQIJBTB008RHS1).
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
        "implement_under_test_parent_recursion", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_parent_recursion"] = mod
    spec.loader.exec_module(mod)
    return mod


def _child(work_item_id: str, status: str = "open", assignee: str = "") -> dict:
    """Build a canned child dict as returned by ``wl show --children``."""
    return {
        "id": work_item_id,
        "title": f"Child {work_item_id}",
        "status": status,
        "stage": "intake_complete",
        "assignee": assignee,
        "priority": "high",
        "sortIndex": 1000,
    }


def _parent(work_item_id: str = "SA-PARENT001", status: str = "in-progress") -> dict:
    return {
        "id": work_item_id,
        "title": f"Parent {work_item_id}",
        "status": status,
        "stage": "in_progress",
    }


def _run_parent(
    mod,
    parent_id: str,
    children: list[dict],
    *,
    phase_start_result: dict | None = None,
    wl_show_result: dict | None = None,
    freeze_active: bool = False,
) -> dict:
    """Run phase_parent with mocked wl/phase_start plumbing.

    Returns the phase_parent report dict.
    """
    wl_show_result = wl_show_result or _parent(parent_id)
    if phase_start_result is None:
        phase_start_result = {
            "success": True,
            "worktree_path": f"/wt/{parent_id}",
            "branch": f"wl-{parent_id}",
            "message": "Worktree created",
        }

    calls: dict = {"update_status": [], "phase_start": [], "comments": []}

    def fake_phase_start(child_id, **kwargs):
        calls["phase_start"].append(child_id)
        result = dict(phase_start_result)
        result["work_item_id"] = child_id
        return result

    def fake_update_status(work_item_id, status, stage=None, assignee=None, **kwargs):
        calls["update_status"].append((work_item_id, status, stage, assignee))
        return {"success": True, "workItem": {"id": work_item_id, "status": status}}

    def fake_add_comment(work_item_id, comment):
        calls["comments"].append((work_item_id, comment))
        return True

    with (
        mock.patch.object(mod, "wl_show", return_value=wl_show_result),
        mock.patch.object(mod, "wl_show_children", return_value=children),
        mock.patch.object(mod, "phase_start", side_effect=fake_phase_start),
        mock.patch.object(mod.StatusLifecycle, "update_status", side_effect=fake_update_status),
        mock.patch.object(mod, "wl_add_comment", side_effect=fake_add_comment),
        mock.patch.object(mod, "is_code_freeze_active", return_value=freeze_active),
    ):
        report = mod.phase_parent(parent_id, json_output=True)
    report["_calls"] = calls  # type: ignore[attr-defined]
    return report


# ===========================================================================
# AC-9: SKILL.md / AGENTS_GLOBAL.md docs updated
# ===========================================================================


class TestDocUpdates:
    def test_skill_md_step_51_describes_recursion_not_dead_end(self):
        """Step 5.1 must describe parent recursion; the old dead-end clause
        ("set open + comment 'Not all children are in a terminal stage'") must
        be gone."""
        content = (_REPO_ROOT / "skill" / "implement" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "implement.py parent" in content
        assert "Parent recursion" in content
        assert "Not all children are in a terminal" not in content

    def test_skill_md_keeps_required_hygiene_markers(self):
        """The existing doc-hygiene markers must survive the rewrite."""
        content = (_REPO_ROOT / "skill" / "implement" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "Intake/interview helpers: `intake`, `plan`." in content
        assert ".command/" not in content

    def test_agents_global_step_4_mentions_parent_recursion(self):
        """AGENTS_GLOBAL.md workflow step 4 must align with the recursion
        behavior (/skill:implement <parent> recurses automatically)."""
        content = (_REPO_ROOT / "AGENTS_GLOBAL.md").read_text(encoding="utf-8")
        assert "/skill:implement <parent>" in content


# ===========================================================================
# AC-8e: leaf item → unchanged current behavior
# ===========================================================================


class TestLeafParent:
    def test_parent_with_no_children_reports_leaf_and_changes_nothing(self, implement_mod):
        """A parent with no children must behave as today: no child is
        started, the parent status is untouched, and the report says to use
        the standard start/finish workflow."""
        report = _run_parent(implement_mod, "SA-PARENT001", [])

        assert report["success"] is True
        assert report.get("leaf") is True
        assert report.get("parent_advanced") is None or report.get("parent_advanced") is False
        # No child started, no status transition on the parent.
        assert report["_calls"]["phase_start"] == []
        assert report["_calls"]["update_status"] == []
        assert "start" in report.get("message", "") or "finish" in report.get("message", "")

    def test_parent_with_no_children_keeps_in_progress_status(self, implement_mod):
        """A leaf parent invoked by the skill stays in_progress (the claim
        from Step 1 is preserved) — the parent action must not reset it."""
        report = _run_parent(implement_mod, "SA-PARENT001", [])
        assert report["success"] is True
        assert report["_calls"]["update_status"] == []


# ===========================================================================
# AC-4: all children terminal → advance the parent
# ===========================================================================


class TestParentAdvancement:
    def test_all_terminal_children_advance_parent(self, implement_mod):
        """All children in_review/completed → the parent is advanced to
        completed/in_review and a summary comment is recorded."""
        children = [
            _child("SA-C1", status="in_review"),
            _child("SA-C2", status="completed"),
            _child("SA-C3", status="done"),
        ]
        report = _run_parent(implement_mod, "SA-PARENT001", children)

        assert report["success"] is True
        assert report.get("parent_advanced") is True
        # Exactly one status transition: the parent → completed/in_review.
        assert report["_calls"]["update_status"] == [
            ("SA-PARENT001", "completed", "in_review", None)
        ]
        # No child worktree was started.
        assert report["_calls"]["phase_start"] == []
        # A summary comment was written.
        assert report["_calls"]["comments"], "expected a summary comment on advancement"
        summary = report["_calls"]["comments"][0][1]
        for cid in ("SA-C1", "SA-C2", "SA-C3"):
            assert cid in summary

    def test_single_terminal_child_advances_parent(self, implement_mod):
        children = [_child("SA-ONLY", status="in_review")]
        report = _run_parent(implement_mod, "SA-PARENT001", children)
        assert report["success"] is True
        assert report.get("parent_advanced") is True
        assert report["_calls"]["update_status"] == [
            ("SA-PARENT001", "completed", "in_review", None)
        ]


# ===========================================================================
# AC-1 happy path: parent with open children → start the next child
# ===========================================================================


class TestStartNextChild:
    def test_parent_with_open_children_starts_first_child(self, implement_mod):
        """Open children → the parent action claims/starts the first child
        (phase_start) and reports its worktree; the parent is NOT advanced."""
        children = [_child("SA-C1"), _child("SA-C2")]
        report = _run_parent(implement_mod, "SA-PARENT001", children)

        assert report["success"] is True
        assert report["_calls"]["phase_start"] == ["SA-C1"]
        assert report["_calls"]["update_status"] == []
        assert report.get("next_child") == "SA-C1"
        assert "/wt/SA-PARENT001" in report.get("message", "")
        assert "finish" in report.get("message", "")  # tells the agent next steps

    def test_parent_with_open_children_skips_terminal_siblings(self, implement_mod):
        """Terminal children are never re-implemented: only the first
        non-terminal child is started."""
        children = [
            _child("SA-C1", status="in_review"),
            _child("SA-C2", status="open"),
            _child("SA-C3", status="completed"),
        ]
        report = _run_parent(implement_mod, "SA-PARENT001", children)

        assert report["success"] is True
        assert report["_calls"]["phase_start"] == ["SA-C2"]
        assert report.get("next_child") == "SA-C2"

    def test_parent_starts_blocked_child_only_after_open_siblings(self, implement_mod):
        """Blocked children are implementable once their in-chain blockers
        are handled; the parent action still advances one child at a time."""
        children = [
            _child("SA-C1", status="open"),
            _child("SA-C2", status="blocked"),
        ]
        report = _run_parent(implement_mod, "SA-PARENT001", children)
        assert report["_calls"]["phase_start"] == ["SA-C1"]


# ===========================================================================
# AC-7: in-progress children are skipped, never clobbered
# ===========================================================================


class TestInProgressSkip:
    def test_in_progress_child_is_reported_not_started(self, implement_mod):
        """A child already in_progress (by this or another agent) is skipped
        and reported, not re-claimed."""
        children = [
            _child("SA-C1", status="in_progress", assignee="other-agent"),
            _child("SA-C2", status="open"),
        ]
        report = _run_parent(implement_mod, "SA-PARENT001", children)

        assert report["success"] is True
        assert report["_calls"]["phase_start"] == ["SA-C2"]
        actions = {c["id"]: c["action"] for c in report.get("children", [])}
        assert actions["SA-C1"] == "skip-in-progress"
        assert actions["SA-C2"] == "implement"

    def test_all_children_in_progress_reports_waiting(self, implement_mod):
        """If every non-terminal child is in progress elsewhere, the parent
        action reports that no child was started (no clobbering)."""
        children = [_child("SA-C1", status="in_progress", assignee="other")]
        report = _run_parent(implement_mod, "SA-PARENT001", children)
        assert report["success"] is True
        assert report["_calls"]["phase_start"] == []
        assert report["_calls"]["update_status"] == []
        assert report.get("next_child") is None


# ===========================================================================
# Failure handling: a failed child start stops the chain with a report
# ===========================================================================


class TestChildStartFailure:
    def test_failed_child_start_reports_and_does_not_advance_parent(self, implement_mod):
        children = [_child("SA-C1"), _child("SA-C2")]
        report = _run_parent(
            implement_mod,
            "SA-PARENT001",
            children,
            phase_start_result={"success": False, "message": "dirty tree"},
        )
        assert report["success"] is False
        assert report["_calls"]["phase_start"] == ["SA-C1"]
        assert report["_calls"]["update_status"] == []
        assert "SA-C1" in report.get("message", "")
