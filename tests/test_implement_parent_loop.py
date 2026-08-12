"""V4 tests: integration & release gate for implement.py parent recursion
(SA-0MSQBM2FK005NW1T, SA-0MSQIJCKU001U21T).

Contract (per work item ACs):

- AC-1: invoking the implement skill on a parent item with non-terminal
  children automatically implements the children (+ blockers) in dependency
  order, without manual per-child invocations.
- AC-8: tests cover (a) parent with open children → all children implemented
  + parent advanced.

This file drives the FULL recursion loop end-to-end: the parent phase is
invoked repeatedly while the harness simulates each child being implemented
(claim → start → terminal) exactly as the standard workflow would, and asserts
that (1) children are started in dependency order, (2) terminal children are
never re-implemented, and (3) the parent is advanced exactly once, only after
ALL children are terminal.
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
        "implement_under_test_parent_v4", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_parent_v4"] = mod
    spec.loader.exec_module(mod)
    return mod


class ChainSimulator:
    """In-memory simulation of the parent-recursion loop.

    The simulator holds a mutable view of child statuses (like a worklog).
    ``phase_parent`` is invoked repeatedly; between invocations the caller
    simulates "implementing" the started child by marking it terminal — the
    same effect ``implement.py finish`` has on a real child.
    """

    def __init__(self, mod, child_specs: list[tuple[str, list[str]]]):
        """child_specs: list of (child_id, [blocker_ids]) in plan order."""
        self.mod = mod
        self.status: dict[str, str] = {}
        self.blockers: dict[str, list[dict]] = {}
        for cid, blocker_ids in child_specs:
            self.status[cid] = "open"
            self.blockers[cid] = [
                {"id": bid, "direction": "depends-on"} for bid in blocker_ids
            ]
        self.started: list[str] = []
        self.advancements: list[tuple[str, str, str]] = []  # (parent, status, stage)
        self.parent_status = "in-progress"

    # -- canned wl plumbing -------------------------------------------------

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

    def _phase_start(self, child_id, **kwargs):
        self.started.append(child_id)
        self.status[child_id] = "in_progress"
        return {
            "success": True,
            "work_item_id": child_id,
            "worktree_path": f"/wt/{child_id}",
            "branch": f"wl-{child_id}",
            "message": "Worktree created",
        }

    def _update_status(self, work_item_id, status, stage=None, assignee=None, **kwargs):
        if work_item_id == "SA-PARENT001":
            self.parent_status = status
            self.advancements.append((work_item_id, status, stage or ""))
        else:
            self.status[work_item_id] = status
        return {"success": True, "workItem": {"id": work_item_id, "status": status}}

    # -- simulation driver --------------------------------------------------

    def implement_child(self, child_id: str) -> None:
        """Simulate the standard per-child workflow completing (finish)."""
        self.status[child_id] = "in_review"

    def run_parent_phase(self) -> dict:
        def _wl_show(*args, **kwargs):
            return {"id": "SA-PARENT001", "title": "P", "status": self.parent_status}

        with (
            mock.patch.object(self.mod, "wl_show", side_effect=_wl_show),
            mock.patch.object(self.mod, "wl_show_children", side_effect=lambda *a, **k: self._children()),
            mock.patch.object(self.mod, "wl_dep_blockers", side_effect=self._dep_blockers),
            mock.patch.object(self.mod, "phase_start", side_effect=self._phase_start),
            mock.patch.object(
                self.mod.StatusLifecycle, "update_status", side_effect=self._update_status
            ),
            mock.patch.object(self.mod, "wl_add_comment", return_value=True),
            mock.patch.object(self.mod, "is_code_freeze_active", return_value=False),
        ):
            return self.mod.phase_parent("SA-PARENT001", json_output=True)


# ===========================================================================
# AC-8a: parent with open children → all implemented + parent advanced
# ===========================================================================


class TestFullChainLoop:
    def test_open_chain_implements_all_children_then_advances_parent(self, implement_mod):
        """Three open children in a dependency chain: the loop starts them in
        dependency order, each is 'finished' before the next, and the parent
        is advanced exactly once — only after ALL children are terminal."""
        sim = ChainSimulator(
            implement_mod,
            [
                ("SA-C1", []),            # root
                ("SA-C2", ["SA-C1"]),     # blocked by C1
                ("SA-C3", ["SA-C2"]),     # blocked by C2
            ],
        )

        # Phase invocation 1 → starts C1 (root, no blockers).
        r1 = sim.run_parent_phase()
        assert r1["success"] is True and r1.get("next_child") == "SA-C1"
        assert sim.started == ["SA-C1"]

        # C1 finished → phase 2 → starts C2 (its blocker C1 is terminal).
        sim.implement_child("SA-C1")
        r2 = sim.run_parent_phase()
        assert r2.get("next_child") == "SA-C2"
        assert sim.started == ["SA-C1", "SA-C2"]

        # C2 finished → phase 3 → starts C3.
        sim.implement_child("SA-C2")
        r3 = sim.run_parent_phase()
        assert r3.get("next_child") == "SA-C3"
        assert sim.started == ["SA-C1", "SA-C2", "SA-C3"]

        # C3 finished → phase 4 → parent advanced (all terminal).
        sim.implement_child("SA-C3")
        r4 = sim.run_parent_phase()
        assert r4.get("parent_advanced") is True
        assert sim.advancements == [("SA-PARENT001", "completed", "in_review")]
        # No child was started in the final phase (nothing left).
        assert sim.started == ["SA-C1", "SA-C2", "SA-C3"]

    def test_terminal_children_never_reimplemented_across_loop(self, implement_mod):
        """Re-running the loop with an already-terminal child must not
        re-implement it — the chain continues with the remaining child."""
        sim = ChainSimulator(implement_mod, [("SA-C1", []), ("SA-C2", [])])
        sim.status["SA-C1"] = "completed"  # pre-existing terminal child

        r = sim.run_parent_phase()
        assert r.get("next_child") == "SA-C2"
        assert sim.started == ["SA-C2"]
        assert "SA-C1" not in sim.started

    def test_parent_advanced_only_once(self, implement_mod):
        """After all children are terminal, repeated parent invocations do
        NOT re-advance or re-implement anything (idempotent terminal state)."""
        sim = ChainSimulator(implement_mod, [("SA-C1", [])])
        sim.status["SA-C1"] = "done"

        r1 = sim.run_parent_phase()
        r2 = sim.run_parent_phase()
        assert r1.get("parent_advanced") is True
        assert r2.get("parent_advanced") is True
        assert sim.advancements == [("SA-PARENT001", "completed", "in_review")]
        assert sim.started == []
