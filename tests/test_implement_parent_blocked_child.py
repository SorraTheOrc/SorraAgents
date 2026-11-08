"""Test-first contract: blocked-child classification matrix for parent/drive.

Parent: **SA-0MVTUFL0U0000NV2**. Contract item: **SA-0MVUTD3IX004E03C**.

These tests are the executable form of the parent's ACs 1–5: a child whose
``status`` is ``blocked`` must be **re-validated** before it is started or
skipped, and a stale block must never strand the child forever.

**Expected red until the implementation items land** — C2 (``skip-blocked``
via dep-edge re-validation), C3 (prose / cross-project blockers) and C4
(``drive`` propagation). They are marked ``xfail(strict=False)`` so the suite
stays green while the contract is documented; when the production change
lands they xpass and the markers can be removed.

Every test drives the production ``phase_parent`` / ``phase_drive`` through the
shared harnesses (``tests.test_implement_parent_safety.ParentHarness`` and
``tests.test_implement_drive.ScriptedParent`` / ``RecordingSpawner``) — no
re-implementation of the orchestration logic.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.test_implement_drive import (
    RecordingSpawner,
    ScriptedParent,
)
from tests.test_implement_drive import (
    _run as _run_drive,
)
from tests.test_implement_parent_safety import ParentHarness, _child

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

#: The contract tests below are expected red until C2/C3/C4 land.
_RED = pytest.mark.xfail(
    strict=False,
    reason=(
        "contract test: expected red until the blocked-child re-validation "
        "lands (C2 skip-blocked / C3 prose+cross-project blockers / "
        "C4 drive propagation)"
    ),
)


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py)."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_blocked_child", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_blocked_child"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _neutralise_ambient_drive_guard(monkeypatch):
    """Keep the module hermetic when the suite runs inside a driven session."""
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _blocker(work_item_id: str, status: str = "open") -> dict:
    """A dependency-edge entry as returned by ``wl dep list`` outbound."""
    return {
        "id": work_item_id,
        "title": f"Blocker {work_item_id}",
        "status": status,
        "priority": "high",
        "direction": "depends-on",
    }


def _blocked_child_with_prose(work_item_id: str, blocker_id: str) -> dict:
    """A ``blocked`` child whose blocker is declared only in prose."""
    child = _child(work_item_id, status="blocked")
    child["description"] = f"**Blocked by (SorraAgents):** {blocker_id}"
    return child


def _entries(report: dict) -> dict[str, dict]:
    """Index ``children_processed`` by child id."""
    return {
        str(entry.get("id")): entry
        for entry in report.get("children_processed", [])
    }


def _blob(report: dict) -> str:
    """The whole report as text, for "is this id named anywhere" assertions."""
    return json.dumps(report, default=str)


# ===========================================================================
# (a) blocked child + non-terminal dep-edge blocker → not started, skip-blocked
# ===========================================================================


class TestNonTerminalDepEdgeBlocker:
    @_RED
    def test_blocked_child_with_open_dep_edge_blocker_is_skip_blocked(
        self, implement_mod
    ):
        """A blocked child whose dep-edge blocker is still open is NOT
        started; its entry is ``skip-blocked`` with ``blocking_ids``."""
        children = [_child("SA-A", status="blocked")]
        blockers = {"SA-A": [_blocker("SA-EXT", status="open")]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        report = h.run()

        assert h.calls["phase_start"] == [], (
            "a blocked child with a non-terminal blocker must not be started"
        )
        entry = _entries(report)["SA-A"]
        assert entry["action"] == "skip-blocked"
        assert entry.get("blocking_ids") == ["SA-EXT"]
        # The new verdict must not be confused with the other skip actions.
        assert entry["action"] not in ("skip-in-progress", "skipped-terminal")

    @_RED
    def test_blocked_child_with_in_chain_non_terminal_blocker_is_skip_blocked(
        self, implement_mod
    ):
        """The same holds when the blocker is an in-chain sibling."""
        children = [
            _child("SA-A", status="blocked", sort_index=2),
            _child("SA-B", status="open", sort_index=1),
        ]
        blockers = {"SA-A": [_blocker("SA-B", status="open")]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        report = h.run()

        # SA-B is startable; SA-A waits behind it.
        assert h.calls["phase_start"] == ["SA-B"]
        entry = _entries(report)["SA-A"]
        assert entry["action"] == "skip-blocked"
        assert entry.get("blocking_ids") == ["SA-B"]


# ===========================================================================
# (b) blocked child + every dep-edge blocker terminal → stale block, started
# ===========================================================================


class TestStaleBlockRevalidation:
    @_RED
    def test_blocked_child_with_terminal_dep_edge_blocker_starts_and_reports_stale(
        self, implement_mod
    ):
        """When every dep-edge blocker is terminal the block is stale: the
        child starts and the stale block is named in the report."""
        children = [_child("SA-A", status="blocked")]
        blockers = {"SA-A": [_blocker("SA-EXT", status="completed")]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        report = h.run()

        assert h.calls["phase_start"] == ["SA-A"], (
            "a stale block must not strand the child forever"
        )
        blob = _blob(report).lower()
        assert "sa-ext" in blob, "the stale blocker must be named in the report"
        assert "stale" in blob, "the report must say the block was stale"

    @_RED
    def test_blocked_child_with_soft_deleted_blocker_starts(self, implement_mod):
        """A soft-deleted blocker is terminal, so the block is stale."""
        children = [_child("SA-A", status="blocked")]
        gone = _blocker("SA-GONE", status="open")
        gone["deletedBy"] = "Map"  # soft-deleted ⇒ terminal
        blockers = {"SA-A": [gone]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        report = h.run()
        assert h.calls["phase_start"] == ["SA-A"]
        assert "SA-GONE" in _blob(report)


# ===========================================================================
# (c) blocked child + prose-only blocker (unresolvable id) → held, id named
# ===========================================================================


class TestProseBlocker:
    @_RED
    def test_prose_blocker_with_unresolvable_id_holds_the_child_and_names_it(
        self, implement_mod
    ):
        """A blocker declared only in prose, with an id that cannot be
        resolved, must hold the child and be reported actionably."""
        children = [_blocked_child_with_prose("SA-A", "OSL-UNRESOLVED")]
        h = ParentHarness(implement_mod, children)
        report = h.run()

        assert h.calls["phase_start"] == [], (
            "an unresolvable prose blocker must never default to startable"
        )
        blob = _blob(report)
        assert "OSL-UNRESOLVED" in blob, (
            "the report must name the unresolved blocker id"
        )
        assert "worklog" in blob.lower() or "check" in blob.lower(), (
            "the report must say where to check the unresolved blocker"
        )


# ===========================================================================
# (d) drive propagates the classification and blocker fields
# ===========================================================================


class TestDrivePropagation:
    @_RED
    def test_drive_propagates_skip_blocked_and_blocking_ids(self, implement_mod):
        """No session is spawned for a skip-blocked child and the drive
        report carries the child id plus its blocking ids."""
        m = implement_mod
        runner = ScriptedParent([
            {
                "success": True,
                "next_child": None,
                "blocked_children": ["SA-A"],
                "children_processed": [
                    {
                        "id": "SA-A",
                        "action": "skip-blocked",
                        "blocking_ids": ["SA-EXT"],
                    },
                ],
                "message": "No startable child: SA-A is blocked by SA-EXT.",
            },
        ])
        spawner = RecordingSpawner()
        report = _run_drive(
            m, runner, spawner, children=[_child("SA-A", status="blocked")]
        )

        assert spawner.calls == [], (
            "a skip-blocked child must never spawn a session"
        )
        assert "SA-A" in report.get("blocked_children", [])
        structured = [report]
        structured.extend(report.get("children_driven", []))
        structured.extend(report.get("children_processed", []))
        assert any(
            entry.get("blocking_ids") == ["SA-EXT"] for entry in structured
        ), (
            "the drive report must carry the child's blocking ids in a "
            "structured field (blocking_ids), not only in free text"
        )


# ===========================================================================
# Fail-closed: blocked child with no dep edges and no parseable prose blocker
# ===========================================================================


class TestFailClosed:
    @_RED
    def test_blocked_child_without_resolvable_blocker_is_not_started(
        self, implement_mod
    ):
        """A blocked child with no dep edges and no ``Blocked by`` line is
        reported unvalidated — it must never default to startable."""
        children = [_child("SA-A", status="blocked")]
        h = ParentHarness(implement_mod, children)
        report = h.run()

        assert h.calls["phase_start"] == [], (
            "an unvalidated block must fail closed, never start the child"
        )
        entry = _entries(report)["SA-A"]
        assert entry["action"] in ("skip-blocked", "skip-unvalidated")
        assert entry["action"] not in ("skip-in-progress", "skipped-terminal")


# ===========================================================================
# (e) Regression cases unchanged
# ===========================================================================


class TestRegressionUnchanged:
    def test_in_chain_blocker_implemented_first(self, implement_mod):
        """In-chain sibling ordering is unchanged: the blocker starts first."""
        children = [
            _child("SA-A", status="open", sort_index=1),
            _child("SA-B", status="open", sort_index=2),
        ]
        blockers = {"SA-B": [_blocker("SA-A")]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        h.run()
        assert h.calls["phase_start"] == ["SA-A"]

    def test_dependency_cycle_fails_fast(self, implement_mod):
        """A cycle fails fast: no child starts and no status transition."""
        children = [_child("SA-A"), _child("SA-B")]
        blockers = {"SA-A": [_blocker("SA-B")], "SA-B": [_blocker("SA-A")]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        report = h.run()

        assert report["success"] is False
        assert h.calls["phase_start"] == []
        assert h.calls["update_status"] == []
        assert "cycle" in report["message"].lower()

    def test_terminal_children_never_reimplemented(self, implement_mod):
        """Terminal children are never re-implemented; the parent advances."""
        children = [_child("SA-T", status="completed")]
        h = ParentHarness(implement_mod, children)
        report = h.run()

        assert h.calls["phase_start"] == []
        assert report.get("parent_advanced") is True

    def test_in_progress_child_elsewhere_is_skipped(self, implement_mod):
        """An in-progress child is reported, never clobbered."""
        children = [_child("SA-P", status="in-progress", assignee="other-agent")]
        h = ParentHarness(implement_mod, children)
        h.run()
        assert h.calls["phase_start"] == []

    def test_soft_deleted_sibling_counts_as_terminal(self, implement_mod):
        """A soft-deleted sibling is terminal for in-chain gating."""
        children = [
            _child("SA-A", status="open", sort_index=1),
            _child("SA-B", status="open", sort_index=2, deleted_by="Map"),
        ]
        blockers = {"SA-A": [_blocker("SA-B")]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        h.run()
        assert h.calls["phase_start"] == ["SA-A"]
