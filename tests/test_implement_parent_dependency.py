"""V2 tests: dependency-graph resolution, topological ordering & cycle
detection for implement.py parent recursion (SA-0MSQBM2FK005NW1T,
SA-0MSQIJBTB008RHS1).

Contract (per work item ACs):

- AC2: if a child is `blocked` by another item (blocked-by/dependency
  edge), the blocking item is implemented before the child; the chain is
  resolved depth-first, dependency-order correct.
- AC7: dependency edges are respected (no out-of-order implementation);
  cycle detection fails fast with a clear error (no infinite recursion).
- AC8: tests cover (b) blocked child → blocker implemented first; (d)
  cycle → graceful error.
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
        "implement_under_test_parent_v2", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_parent_v2"] = mod
    spec.loader.exec_module(mod)
    return mod


def _child(
    work_item_id: str, status: str = "open", sort_index: int = 1000, assignee: str = ""
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


def _blocker(work_item_id: str, status: str = "open") -> dict:
    """A dependency-edge entry as returned by ``wl dep list`` outbound."""
    return {
        "id": work_item_id,
        "title": f"Blocker {work_item_id}",
        "status": status,
        "priority": "high",
        "direction": "depends-on",
    }


# ===========================================================================
# _resolve_implementation_order — pure ordering logic
# ===========================================================================


class TestResolveOrder:
    def test_plain_children_keep_stable_order(self, implement_mod):
        children = [_child("SA-C1", sort_index=1), _child("SA-C2", sort_index=2)]
        ordered, err = implement_mod._resolve_implementation_order(children, {})
        assert err is None
        assert [c["id"] for c in ordered] == ["SA-C1", "SA-C2"]

    def test_blocker_implemented_before_blocked_child(self, implement_mod):
        """A child that depends on a sibling blocker must be ordered AFTER
        the blocker (AC2: blocked child → blocker implemented first)."""
        children = [
            _child("SA-A", status="blocked", sort_index=2),
            _child("SA-B", sort_index=1),
        ]
        blockers = {"SA-A": [_blocker("SA-B")]}
        ordered, err = implement_mod._resolve_implementation_order(children, blockers)
        assert err is None
        assert [c["id"] for c in ordered] == ["SA-B", "SA-A"]

    def test_transitive_chain_resolved_depth_first(self, implement_mod):
        """A→(needs B)→(needs C): C, B, A — dependency-order correct."""
        children = [
            _child("SA-A", status="blocked", sort_index=3),
            _child("SA-B", status="blocked", sort_index=2),
            _child("SA-C", sort_index=1),
        ]
        blockers = {
            "SA-A": [_blocker("SA-B")],
            "SA-B": [_blocker("SA-C")],
        }
        ordered, err = implement_mod._resolve_implementation_order(children, blockers)
        assert err is None
        assert [c["id"] for c in ordered] == ["SA-C", "SA-B", "SA-A"]

    def test_self_dependency_cycle_fails_fast(self, implement_mod):
        """A child that depends on itself is a cycle → graceful error, no
        ordering, no infinite recursion."""
        children = [_child("SA-A")]
        blockers = {"SA-A": [_blocker("SA-A")]}
        ordered, err = implement_mod._resolve_implementation_order(children, blockers)
        assert ordered == []
        assert err is not None
        assert "cycle" in err.lower() or "loop" in err.lower()

    def test_mutual_dependency_cycle_fails_fast(self, implement_mod):
        """A↔B mutual dependency → graceful cycle error (AC8d)."""
        children = [_child("SA-A"), _child("SA-B")]
        blockers = {
            "SA-A": [_blocker("SA-B")],
            "SA-B": [_blocker("SA-A")],
        }
        ordered, err = implement_mod._resolve_implementation_order(children, blockers)
        assert ordered == []
        assert err is not None
        assert "SA-A" in err and "SA-B" in err

    def test_external_blocker_not_in_children_is_ignored_for_ordering(self, implement_mod):
        """A blocker outside the parent's children (e.g. cross-project) does
        not create a graph edge; the child remains orderable."""
        children = [_child("SA-A"), _child("SA-B")]
        blockers = {"SA-A": [_blocker("OSL-EXTERNAL")]}
        ordered, err = implement_mod._resolve_implementation_order(children, blockers)
        assert err is None
        assert len(ordered) == 2


# ===========================================================================
# phase_parent — dependency-aware next-child selection
# ===========================================================================


class TestParentDependencyOrder:
    def test_blocked_child_started_only_after_blocker(self, implement_mod):
        """phase_parent must start the in-chain blocker BEFORE the blocked
        child (AC2), regardless of wl children order."""
        children = [
            _child("SA-A", status="blocked", sort_index=2),
            _child("SA-B", sort_index=1),
        ]
        blockers_map = {"SA-A": [_blocker("SA-B")]}
        report = self._run_parent(implement_mod, children, blockers_map)
        assert report["success"] is True
        assert report["_calls"]["phase_start"] == ["SA-B"]
        assert report.get("next_child") == "SA-B"

    def test_terminal_blocker_does_not_block_the_child(self, implement_mod):
        """A child whose blocker is ALREADY terminal is immediately
        implementable — no re-implementation of the terminal blocker."""
        children = [
            _child("SA-B", status="completed"),
            _child("SA-A", status="blocked"),
        ]
        blockers_map = {"SA-A": [_blocker("SA-B", status="completed")]}
        report = self._run_parent(implement_mod, children, blockers_map)
        assert report["success"] is True
        assert report["_calls"]["phase_start"] == ["SA-A"]
        assert report["_calls"]["update_status"] == []

    def test_in_progress_blocker_prevents_child_start(self, implement_mod):
        """A child whose in-chain blocker is NOT terminal (e.g. in progress
        by another agent) must NOT be started — no out-of-order
        implementation (AC-7)."""
        children = [
            _child("SA-B", status="in_progress", assignee="other-agent"),
            _child("SA-A", status="blocked"),
        ]
        blockers_map = {"SA-A": [_blocker("SA-B", status="in-progress")]}
        report = self._run_parent(implement_mod, children, blockers_map)
        assert report["success"] is True
        assert report["_calls"]["phase_start"] == []
        assert report.get("next_child") is None
        assert report.get("blocked_children") == ["SA-A"]

    def test_cycle_does_not_start_any_child(self, implement_mod):
        """A dependency cycle → graceful error: no child is started and no
        parent status transition happens (AC8d)."""
        children = [_child("SA-A"), _child("SA-B")]
        blockers_map = {
            "SA-A": [_blocker("SA-B")],
            "SA-B": [_blocker("SA-A")],
        }
        report = self._run_parent(implement_mod, children, blockers_map)
        assert report["success"] is False
        assert report["_calls"]["phase_start"] == []
        assert report["_calls"]["update_status"] == []
        assert "cycle" in report.get("message", "").lower() or "loop" in report.get("message", "").lower()

    def test_external_blocker_reported_not_started_early(self, implement_mod):
        """Cross-project blockers are reported as coordination notes; the
        child is still orderable among its siblings."""
        children = [_child("SA-A", status="blocked"), _child("SA-B")]
        blockers_map = {"SA-A": [_blocker("OSL-EXTERNAL")]}
        report = self._run_parent(implement_mod, children, blockers_map)
        assert report["success"] is True
        # No in-chain edge — the first implementable child is SA-A or SA-B
        # depending on order; both are non-terminal so SA-A comes first
        # (stable order) and is started.
        assert report["_calls"]["phase_start"] == ["SA-A"]

    def _run_parent(self, mod, children, blockers_map):
        """Run phase_parent with mocked wl/phase_start and a per-child
        blocker map (keyed by child id → wl dep list outbound entries)."""

        def fake_wl_dep_blockers(child_id, **_):
            return list(blockers_map.get(child_id, []))

        calls = {"phase_start": [], "update_status": []}

        def fake_phase_start(child_id, **kwargs):
            calls["phase_start"].append(child_id)
            return {
                "success": True,
                "work_item_id": child_id,
                "worktree_path": f"/wt/{child_id}",
                "branch": f"wl-{child_id}",
                "message": "Worktree created",
            }

        def fake_update_status(work_item_id, status, stage=None, assignee=None, **kwargs):
            calls["update_status"].append((work_item_id, status, stage, assignee))
            return {"success": True, "workItem": {"id": work_item_id, "status": status}}

        with (
            mock.patch.object(mod, "wl_show", return_value={"id": "SA-PARENT001", "title": "P"}),
            mock.patch.object(mod, "wl_show_children", return_value=children),
            mock.patch.object(mod, "wl_dep_blockers", side_effect=fake_wl_dep_blockers),
            mock.patch.object(mod, "phase_start", side_effect=fake_phase_start),
            mock.patch.object(mod.StatusLifecycle, "update_status", side_effect=fake_update_status),
            mock.patch.object(mod, "wl_add_comment", return_value=True),
            mock.patch.object(mod, "is_code_freeze_active", return_value=False),
        ):
            report = mod.phase_parent("SA-PARENT001", json_output=True)
        report["_calls"] = calls
        return report
