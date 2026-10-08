"""Tests for the single-pass parent orchestration loop
(SA-0MUWBHEEF002A6BO).

Contract (per work item ACs):

- AC-1: a single ``phase_parent`` invocation iterates ALL implementable
  children in dependency order — finishing those whose worktrees already
  contain changes and starting the next unimplemented one.
- AC-2: each child's start/finish runs through a separate subprocess
  (``_invoke_implement``) called serially, not a direct in-process call.
- AC-3: the parent process reports a per-child summary
  (``children_processed``) and periodic progress updates.
- AC-4: the parent is advanced to ``completed``/``in_review`` in the same
  pass once every child is terminal.
- AC-5: failures reset the failing child to ``open`` and stop the chain;
  already-completed siblings are never regressed.
- AC-6: terminal/in-progress-elsewhere children are skipped and reported.

These tests mock the worklog plumbing and the subprocess bridge so the
orchestration loop itself is exercised deterministically.
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


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py)."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_single_pass", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_single_pass"] = mod
    spec.loader.exec_module(mod)
    return mod


class Simulation:
    """Deterministic in-memory harness for the single-pass loop.

    Holds child statuses, worktree paths, whether each worktree has
    changes, and records every subprocess bridge invocation.
    """

    def __init__(
        self,
        mod,
        child_specs: list[tuple[str, list[str]]],
        *,
        worktrees: dict[str, str] | None = None,
        changes: dict[str, bool] | None = None,
    ):
        self.mod = mod
        self.status: dict[str, str] = {}
        self.blockers: dict[str, list[dict]] = {}
        for cid, blocker_ids in child_specs:
            self.status[cid] = "open"
            self.blockers[cid] = [
                {"id": bid, "direction": "depends-on"} for bid in blocker_ids
            ]
        self.worktrees: dict[str, str] = dict(worktrees or {})
        self.changes: dict[str, bool] = dict(changes or {})
        self.calls: list[tuple[str, str]] = []  # (action, child_id)
        self.advancements: list[tuple[str, str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.parent_status = "in-progress"
        self.finish_failures: set[str] = set()
        self.start_failures: set[str] = set()

    # -- worklog plumbing (mocked) -----------------------------------------

    def _children(self):
        return [
            {
                "id": cid,
                "title": f"Child {cid}",
                "status": self.status[cid],
                "stage": "intake_complete",
                "assignee": "",
                "priority": "high",
                "sortIndex": idx * 1000,
            }
            for idx, cid in enumerate(self.status)
        ]

    def _dep_blockers(self, cid, **_):
        return self.blockers.get(cid, [])

    def _invoke(self, action, child_id, **kwargs):
        """Mock the subprocess bridge (``_invoke_implement``)."""
        self.calls.append((action, child_id))
        if action == "start":
            if child_id in self.start_failures:
                return None
            self.status[child_id] = "in_progress"
            wt = f"/wt/{child_id}"
            self.worktrees[child_id] = wt
            return {
                "success": True,
                "work_item_id": child_id,
                "worktree_path": wt,
                "branch": f"wl-{child_id}",
                "message": "Worktree created",
            }
        if action == "finish":
            if child_id in self.finish_failures:
                # Emulate phase_finish failure: status reset to open.
                self.status[child_id] = "open"
                return None
            self.status[child_id] = "completed"
            self.worktrees.pop(child_id, None)
            self.changes.pop(child_id, None)
            return {"success": True, "work_item_id": child_id}
        raise AssertionError(f"unexpected action {action}")

    def _discover_worktree(self, child_id):
        return self.worktrees.get(child_id)

    def _has_worktree_changes(self, worktree_path):
        # worktree_path == /wt/<cid>
        cid = worktree_path.rsplit("/", 1)[-1]
        return bool(self.changes.get(cid, False))

    def _update_status(self, work_item_id, status, stage=None, assignee=None, **kwargs):
        if work_item_id == "SA-PARENT001":
            self.parent_status = status
            self.advancements.append((work_item_id, status, stage or ""))
        else:
            self.status[work_item_id] = status
        return {"success": True, "workItem": {"id": work_item_id, "status": status}}

    def _wl_show(self, *args, **kwargs):
        return {"id": "SA-PARENT001", "title": "P", "status": self.parent_status}

    def _add_comment(self, work_item_id, comment):
        self.comments.append((work_item_id, comment))
        return True

    # -- driver ------------------------------------------------------------

    def run(self) -> dict:
        with (
            mock.patch.object(self.mod, "wl_show", side_effect=self._wl_show),
            mock.patch.object(
                self.mod, "wl_show_children",
                side_effect=lambda *a, **k: self._children(),
            ),
            mock.patch.object(
                self.mod, "wl_dep_blockers", side_effect=self._dep_blockers
            ),
            mock.patch.object(
                self.mod, "_invoke_implement", side_effect=self._invoke
            ),
            mock.patch.object(
                self.mod, "_discover_worktree",
                side_effect=self._discover_worktree,
            ),
            mock.patch.object(
                self.mod, "_has_worktree_changes",
                side_effect=self._has_worktree_changes,
            ),
            mock.patch.object(
                self.mod.StatusLifecycle, "update_status",
                side_effect=self._update_status,
            ),
            mock.patch.object(
                self.mod, "wl_add_comment", side_effect=self._add_comment
            ),
            mock.patch.object(
                self.mod, "is_code_freeze_active", return_value=False
            ),
        ):
            return self.mod.phase_parent("SA-PARENT001", json_output=True)


# ===========================================================================
# AC-1/AC-4: single invocation finishes all implemented children
# ===========================================================================


class TestSinglePassFinish:
    def test_all_implemented_children_finished_in_one_pass(self, implement_mod):
        """Three children all with worktree changes: one invocation finishes
        all three and advances the parent (no per-child re-invocation)."""
        sim = Simulation(
            implement_mod,
            [("SA-C1", []), ("SA-C2", []), ("SA-C3", [])],
            worktrees={"SA-C1": "/wt/SA-C1", "SA-C2": "/wt/SA-C2",
                       "SA-C3": "/wt/SA-C3"},
            changes={"SA-C1": True, "SA-C2": True, "SA-C3": True},
        )
        sim.status = {cid: "in_progress" for cid in sim.status}

        result = sim.run()

        assert result["success"] is True
        assert result["parent_advanced"] is True
        # Each child finished exactly once, in dependency order.
        assert sim.calls == [
            ("finish", "SA-C1"),
            ("finish", "SA-C2"),
            ("finish", "SA-C3"),
        ]
        assert result["children_processed"] == [
            {"id": "SA-C1", "title": "Child SA-C1",
             "action": "finished", "status": "completed"},
            {"id": "SA-C2", "title": "Child SA-C2",
             "action": "finished", "status": "completed"},
            {"id": "SA-C3", "title": "Child SA-C3",
             "action": "finished", "status": "completed"},
        ]
        assert sim.advancements == [("SA-PARENT001", "completed", "in_review")]

    def test_finish_then_start_next_in_same_pass(self, implement_mod):
        """A single invocation finishes an implemented child and starts the
        next unimplemented child (single-pass chaining)."""
        sim = Simulation(
            implement_mod,
            [("SA-C1", []), ("SA-C2", []), ("SA-C3", [])],
            worktrees={"SA-C1": "/wt/SA-C1"},
            changes={"SA-C1": True},
        )
        sim.status["SA-C1"] = "in_progress"

        result = sim.run()

        assert result.get("next_child") == "SA-C2"
        assert sim.calls == [("finish", "SA-C1"), ("start", "SA-C2")]
        assert result["worktree_path"] == "/wt/SA-C2"
        # Parent not advanced: C2/C3 remain.
        assert not result.get("parent_advanced")
        assert sim.advancements == []

    def test_multiple_finishes_before_starting_next(self, implement_mod):
        """All implemented siblings finish before the next unimplemented
        child starts — the loop drains ready work first."""
        sim = Simulation(
            implement_mod,
            [("SA-C1", []), ("SA-C2", []), ("SA-C3", [])],
            worktrees={"SA-C1": "/wt/SA-C1", "SA-C2": "/wt/SA-C2"},
            changes={"SA-C1": True, "SA-C2": True},
        )
        sim.status["SA-C1"] = "in_progress"
        sim.status["SA-C2"] = "in_progress"

        result = sim.run()

        assert result.get("next_child") == "SA-C3"
        assert sim.calls == [
            ("finish", "SA-C1"),
            ("finish", "SA-C2"),
            ("start", "SA-C3"),
        ]


# ===========================================================================
# AC-2: subprocess isolation & serial ordering
# ===========================================================================


class TestSubprocessIsolation:
    def test_start_called_via_subprocess_bridge(self, implement_mod):
        """Starting a child goes through ``_invoke_implement`` (subprocess),
        never a direct ``phase_start`` call."""
        sim = Simulation(implement_mod, [("SA-C1", [])])

        with mock.patch.object(implement_mod, "phase_start") as direct_start:
            result = sim.run()

        assert result.get("next_child") == "SA-C1"
        assert sim.calls == [("start", "SA-C1")]
        direct_start.assert_not_called()

    def test_serial_order_matches_dependency_chain(self, implement_mod):
        """Children run serially in dependency order (blockers first)."""
        sim = Simulation(
            implement_mod,
            [("SA-C3", ["SA-C2"]), ("SA-C1", []), ("SA-C2", ["SA-C1"])],
        )

        # First invocation starts the root C1 only.
        r1 = sim.run()
        assert r1.get("next_child") == "SA-C1"
        assert sim.calls == [("start", "SA-C1")]

        # Simulate C1 implemented; next invocation finishes C1 and starts C2.
        sim.worktrees["SA-C1"] = "/wt/SA-C1"
        sim.changes["SA-C1"] = True
        r2 = sim.run()
        assert r2.get("next_child") == "SA-C2"
        assert sim.calls == [
            ("start", "SA-C1"),
            ("finish", "SA-C1"),
            ("start", "SA-C2"),
        ]


# ===========================================================================
# AC-3: periodic progress + summary
# ===========================================================================


class TestSummaryOutput:
    def test_children_processed_reports_every_outcome(self, implement_mod):
        """The summary lists terminal, in-progress-skipped, and finished
        children with their action and status."""
        sim = Simulation(
            implement_mod,
            [("SA-C1", []), ("SA-C2", []), ("SA-C3", [])],
            worktrees={"SA-C2": "/wt/SA-C2"},
            changes={"SA-C2": True},
        )
        sim.status["SA-C1"] = "completed"   # terminal
        sim.status["SA-C2"] = "in_progress"  # ours, implemented
        sim.status["SA-C3"] = "open"         # started this pass

        result = sim.run()

        actions = {r["id"]: r["action"] for r in result["children_processed"]}
        assert actions["SA-C1"] == "skipped-terminal"
        assert actions["SA-C2"] == "finished"
        assert result.get("next_child") == "SA-C3"

    def test_parent_advance_comment_includes_summary(self, implement_mod):
        """Advancing the parent posts a comment containing the per-child
        summary (producer visibility)."""
        sim = Simulation(
            implement_mod,
            [("SA-C1", [])],
            worktrees={"SA-C1": "/wt/SA-C1"},
            changes={"SA-C1": True},
        )
        sim.status["SA-C1"] = "in_progress"

        result = sim.run()

        assert result["parent_advanced"] is True
        assert sim.comments, "expected a parent-advance comment"
        body = sim.comments[-1][1]
        assert "SA-C1" in body
        assert "finished" in body


# ===========================================================================
# AC-5: failure isolation
# ===========================================================================


class TestFailureIsolation:
    def test_finish_failure_stops_chain_and_reports(self, implement_mod):
        """A finish subprocess failure stops the chain, reports the failing
        child, and leaves the work item resettable (not advanced)."""
        sim = Simulation(
            implement_mod,
            [("SA-C1", []), ("SA-C2", [])],
            worktrees={"SA-C1": "/wt/SA-C1", "SA-C2": "/wt/SA-C2"},
            changes={"SA-C1": True, "SA-C2": True},
        )
        sim.status["SA-C1"] = "in_progress"
        sim.status["SA-C2"] = "in_progress"
        sim.finish_failures = {"SA-C1"}

        result = sim.run()

        assert result["success"] is False
        assert result["finishing_failed"] is True
        assert result["next_child"] == "SA-C1"
        # C2 must not be finished after C1 failed.
        assert ("finish", "SA-C2") not in sim.calls
        # Parent never advanced.
        assert sim.advancements == []

    def test_start_failure_stops_chain_and_reports(self, implement_mod):
        """A start subprocess failure stops the chain and reports the child."""
        sim = Simulation(implement_mod, [("SA-C1", [])])
        sim.start_failures = {"SA-C1"}

        result = sim.run()

        assert result["success"] is False
        assert result["start_failed"] is True
        assert result["next_child"] == "SA-C1"
        assert sim.advancements == []


# ===========================================================================
# AC-6: skip terminal / in-progress elsewhere
# ===========================================================================


class TestSkipGuards:
    def test_terminal_children_never_reimplemented(self, implement_mod):
        sim = Simulation(implement_mod, [("SA-C1", []), ("SA-C2", [])])
        sim.status["SA-C1"] = "done"

        result = sim.run()

        assert result.get("next_child") == "SA-C2"
        assert sim.calls == [("start", "SA-C2")]

    def test_in_progress_elsewhere_without_worktree_is_skipped(self, implement_mod):
        """An in-progress child with no worktree is another agent's claim:
        skip it and continue to the next startable child."""
        sim = Simulation(implement_mod, [("SA-C1", []), ("SA-C2", [])])
        sim.status["SA-C1"] = "in_progress"  # no worktree → other agent

        result = sim.run()

        assert result.get("next_child") == "SA-C2"
        assert sim.calls == [("start", "SA-C2")]

    def test_all_terminal_advances_parent(self, implement_mod):
        sim = Simulation(implement_mod, [("SA-C1", []), ("SA-C2", [])])
        sim.status["SA-C1"] = "completed"
        sim.status["SA-C2"] = "in_review"

        result = sim.run()

        assert result["parent_advanced"] is True
        assert sim.calls == []
        assert sim.advancements == [("SA-PARENT001", "completed", "in_review")]
