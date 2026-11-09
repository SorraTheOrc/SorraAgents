"""Test-first contract: blocked-child classification matrix for parent/drive.

Parent: **SA-0MVTUFL0U0000NV2**. Contract item: **SA-0MVUTD3IX004E03C**.

These tests are the executable form of the parent's ACs 1–5: a child whose
``status`` is ``blocked`` must be **re-validated** before it is started or
skipped, and a stale block must never strand the child forever.

**Cases (a), (b), (c), (e) and the fail-closed case are green** after C2
(``skip-blocked`` via dep-edge re-validation) and C3 (prose / cross-project
blockers) landed. Case (d) (``drive`` propagation) stays red until C4 and
keeps ``xfail(strict=False)`` so the suite stays green while its contract is
documented.

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

#: Remaining contract test, red until C4 (drive propagation).
_RED = pytest.mark.xfail(
    strict=False,
    reason=(
        "contract test: expected red until C4 (drive propagation) lands"
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


def _classifications(report: dict) -> dict[str, dict]:
    """Index the per-child classifications (``report["children"]``) by id.

    ``children_processed`` carries the reduced pass verdict (action, status,
    blocking ids); the richer evidence (``blocker_details``,
    ``unresolved_blockers``, ``stale_block_ids``) lives on the classification.
    """
    return {
        str(entry.get("id")): entry
        for entry in report.get("children", [])
    }


def _blob(report: dict) -> str:
    """The whole report as text, for "is this id named anywhere" assertions."""
    return json.dumps(report, default=str)


# ===========================================================================
# (a) blocked child + non-terminal dep-edge blocker → not started, skip-blocked
# ===========================================================================


class TestNonTerminalDepEdgeBlocker:
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


# ===========================================================================
# Unit-level: the re-validation helper and the classification precedence
# ===========================================================================


class TestBlockedChildState:
    """Unit tests for ``_blocked_child_state`` (C2, AC2/AC4/AC5)."""

    def test_mixed_terminal_and_open_blockers_holds(self, implement_mod):
        """A non-terminal blocker wins: the block holds and only the
        non-terminal id is reported as blocking."""
        child = _child("SA-A", status="blocked")
        state = implement_mod._blocked_child_state(
            child,
            [
                _blocker("SA-T", status="completed"),
                _blocker("SA-O", status="open"),
            ],
        )
        assert state == {
            "holds": True,
            "blocking_ids": ["SA-O"],
            "stale_block_ids": [],
            "unvalidated": False,
        }

    def test_all_soft_deleted_blockers_is_stale(self, implement_mod):
        """Every soft-deleted blocker is terminal ⇒ the block is stale."""
        child = _child("SA-A", status="blocked")
        gone = _blocker("SA-GONE", status="open")
        gone["deletedBy"] = "Map"
        state = implement_mod._blocked_child_state(child, [gone])
        assert state["holds"] is False
        assert state["stale_block_ids"] == ["SA-GONE"]

    def test_no_edges_is_unvalidated_and_holds(self, implement_mod):
        """No dependency edge ⇒ unvalidated ⇒ holds (never startable)."""
        child = _child("SA-A", status="blocked")
        state = implement_mod._blocked_child_state(child, [])
        assert state["holds"] is True
        assert state["unvalidated"] is True

    def test_in_chain_edge_without_status_uses_child_record(
        self, implement_mod
    ):
        """An in-chain edge that omits ``status`` is resolved from the live
        child list, so a completed sibling makes the block stale."""
        children = [
            _child("SA-A", status="blocked", sort_index=2),
            _child("SA-B", status="completed", sort_index=1),
        ]
        edge = {"id": "SA-B", "direction": "depends-on"}  # no status
        state = implement_mod._blocked_child_state(
            children[0], [edge], children
        )
        assert state["holds"] is False
        assert state["stale_block_ids"] == ["SA-B"]

    def test_in_chain_edge_without_status_uses_child_record_when_open(
        self, implement_mod
    ):
        """The same resolution also holds a child whose sibling is open."""
        children = [
            _child("SA-A", status="blocked", sort_index=2),
            _child("SA-B", status="open", sort_index=1),
        ]
        edge = {"id": "SA-B", "direction": "depends-on"}  # no status
        state = implement_mod._blocked_child_state(
            children[0], [edge], children
        )
        assert state == {
            "holds": True,
            "blocking_ids": ["SA-B"],
            "stale_block_ids": [],
            "unvalidated": False,
        }


class TestClassifyChildPrecedence:
    """``_classify_child`` precedence: terminal → held → in-progress →
    skip-blocked → implement (AC1)."""

    def test_blocked_child_with_holding_block_is_skip_blocked(
        self, implement_mod
    ):
        child = _child("SA-A", status="blocked")
        action = implement_mod._classify_child(
            child, [_blocker("SA-O", status="open")]
        )
        assert action == implement_mod.ACTION_SKIP_BLOCKED == "skip-blocked"

    def test_blocked_child_with_stale_block_is_implementable(self, implement_mod):
        child = _child("SA-A", status="blocked")
        action = implement_mod._classify_child(
            child, [_blocker("SA-T", status="completed")]
        )
        assert action == "implement"

    def test_blocked_child_without_edges_is_skip_blocked(self, implement_mod):
        """Fail closed: an unvalidatable block never defaults to startable."""
        child = _child("SA-A", status="blocked")
        assert (
            implement_mod._classify_child(child, [])
            == implement_mod.ACTION_SKIP_BLOCKED
        )

    def test_terminal_status_outranks_a_blocked_hold(self, implement_mod):
        """A terminal child is never re-implemented, whatever its edges say."""
        child = _child("SA-A", status="completed")
        assert (
            implement_mod._classify_child(
                child, [_blocker("SA-O", status="open")]
            )
            == "skip-terminal"
        )

    def test_in_progress_status_outranks_a_blocked_hold(self, implement_mod):
        """An in-progress claim is never clobbered (precedence documented)."""
        child = _child("SA-A", status="in-progress", assignee="other")
        assert (
            implement_mod._classify_child(
                child, [_blocker("SA-O", status="open")]
            )
            == "skip-in-progress"
        )


class TestStaleBlockReporting:
    """The stale verdict and the held verdict reach the report (AC3)."""

    def test_stale_block_reported_and_child_started(self, implement_mod):
        children = [_child("SA-A", status="blocked")]
        blockers = {"SA-A": [_blocker("SA-EXT", status="in_review")]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        report = h.run()

        assert h.calls["phase_start"] == ["SA-A"]
        assert report["stale_blocks"] == [
            {"id": "SA-A", "blocking_ids": ["SA-EXT"]}
        ]
        # The lifecycle status is left to its owner (Q2 = A).
        assert [call[0] for call in h.calls["update_status"]] == []
        # The stale finding is recorded on the child, not only in the report.
        assert any(
            cid == "SA-A" and "SA-EXT" in text
            for cid, text in h.calls["comments"]
        )

    def test_held_block_reported_in_blocked_children(self, implement_mod):
        children = [_child("SA-A", status="blocked", sort_index=2)]
        blockers = {"SA-A": [_blocker("SA-EXT", status="open")]}
        h = ParentHarness(implement_mod, children, blockers_map=blockers)
        report = h.run()

        assert report["blocked_children"] == ["SA-A"]
        # The "no startable child" message names the blocking id.
        assert "SA-EXT" in report["message"]

    def test_unvalidated_block_reported_in_report(self, implement_mod):
        children = [_child("SA-A", status="blocked", sort_index=2)]
        h = ParentHarness(implement_mod, children)
        report = h.run()

        assert [b["id"] for b in report["unvalidated_blocks"]] == ["SA-A"]
        entry = _entries(report)["SA-A"]
        assert entry["action"] == "skip-blocked"
        assert entry.get("blocking_ids") == []


# ===========================================================================
# C3: prose ``Blocked by`` / cross-project blockers (AC3)
# ===========================================================================


def _prose_child(
    work_item_id: str, line: str, extra: str = "", status: str = "blocked"
) -> dict:
    """A child whose description carries an explicit blocker line."""
    child = _child(work_item_id, status=status)
    child["description"] = (
        f"# Child {work_item_id}\n\n{line}\n"
        "**Unblock condition:** the blocker is in `dev`.\n"
        f"{extra}"
    )
    return child


def _item(work_item_id: str, status: str) -> dict:
    """A resolvable work item as returned by ``wl show``."""
    return {
        "id": work_item_id,
        "title": f"Item {work_item_id}",
        "status": status,
        "deletedBy": "",
    }


class TestProseBlockerParser:
    """The parser is narrow: one explicit line, ids only."""

    def test_extracts_ids_from_the_bold_labelled_form(self, implement_mod):
        description = (
            "**Blocked by (SorraAgents):** `SA-0MVL2NH1G0094FA2` — *Finish gate*\n"
            "**Unblock condition:** `SA-0MVTET8KQ0031R6Z` is in dev\n"
            "## Related work\n- `SA-0MVL7YOMJ006UMDO` — unrelated\n"
        )
        assert implement_mod._prose_blocker_ids(description) == [
            "SA-0MVL2NH1G0094FA2"
        ]

    def test_extracts_from_a_plain_lowercase_line(self, implement_mod):
        assert implement_mod._prose_blocker_ids(
            "blocked by: SA-0MVS1QS9J009DGPT"
        ) == ["SA-0MVS1QS9J009DGPT"]

    def test_ignores_unblock_condition_and_related_work_without_a_line(
        self, implement_mod
    ):
        description = (
            "**Unblock condition:** `SA-0MVTET8KQ0031R6Z` is in dev\n"
            "## Related work\n- `SA-0MVL7YOMJ006UMDO`\n"
        )
        assert implement_mod._prose_blocker_ids(description) == []

    def test_empty_and_malformed_input_never_raises(self, implement_mod):
        assert implement_mod._prose_blocker_ids("") == []
        assert implement_mod._prose_blocker_ids("Blocked by:") == []
        assert implement_mod._prose_blocker_ids(None) == []


class TestProseBlockerRevalidation:
    """Prose blockers gate the start exactly like dependency edges."""

    def test_open_resolvable_prose_blocker_holds_with_its_status(
        self, implement_mod
    ):
        children = [
            _prose_child(
                "SA-A", "**Blocked by (SorraAgents):** SA-BLOCKER"
            )
        ]
        h = ParentHarness(
            implement_mod,
            children,
            items={"SA-BLOCKER": _item("SA-BLOCKER", "open")},
        )
        report = h.run()

        assert h.calls["phase_start"] == []
        verdict = _entries(report)["SA-A"]
        assert verdict["action"] == "skip-blocked"
        assert verdict["blocking_ids"] == ["SA-BLOCKER"]
        entry = _classifications(report)["SA-A"]
        assert entry["action"] == "skip-blocked"
        assert entry["external_blockers"] == ["SA-BLOCKER"]
        assert entry["blocker_details"] == [
            {
                "id": "SA-BLOCKER",
                "status": "open",
                "source": "prose",
                "resolved": True,
            }
        ]

    def test_terminal_resolvable_prose_blocker_is_stale_and_starts(
        self, implement_mod
    ):
        children = [
            _prose_child(
                "SA-A", "**Blocked by (SorraAgents):** SA-BLOCKER"
            )
        ]
        h = ParentHarness(
            implement_mod,
            children,
            items={"SA-BLOCKER": _item("SA-BLOCKER", "completed")},
        )
        report = h.run()

        assert h.calls["phase_start"] == ["SA-A"]
        assert report["stale_blocks"] == [
            {"id": "SA-A", "blocking_ids": ["SA-BLOCKER"]}
        ]

    def test_unresolvable_prose_blocker_holds_with_an_actionable_hint(
        self, implement_mod
    ):
        children = [
            _prose_child("SA-A", "**Blocked by (SorraAgents):** OSL-EXTERNAL")
        ]
        h = ParentHarness(implement_mod, children)  # not resolvable here
        report = h.run()

        assert h.calls["phase_start"] == []
        verdict = _entries(report)["SA-A"]
        assert verdict["action"] == "skip-blocked"
        assert verdict["blocking_ids"] == ["OSL-EXTERNAL"]
        assert report["unresolved_blockers"] == {"SA-A": ["OSL-EXTERNAL"]}
        entry = _classifications(report)["SA-A"]
        assert entry["unresolved_blockers"][0]["hint"].startswith(
            "check the owning project's worklog"
        )
        # The actionable hint reaches the operator-facing message too.
        assert "OSL-EXTERNAL" in report["message"]
        assert "worklog" in report["message"]

    def test_prose_and_edge_blockers_are_merged(self, implement_mod):
        """Declared edges stay authoritative; the prose line adds to them."""
        children = [
            _prose_child(
                "SA-A", "**Blocked by (SorraAgents):** SA-PROSE"
            )
        ]
        blockers = {"SA-A": [_blocker("SA-EDGE", status="open")]}
        h = ParentHarness(
            implement_mod,
            children,
            blockers_map=blockers,
            items={"SA-PROSE": _item("SA-PROSE", "open")},
        )
        report = h.run()
        entry = _classifications(report)["SA-A"]

        assert h.calls["phase_start"] == []
        assert _entries(report)["SA-A"]["blocking_ids"] == [
            "SA-EDGE", "SA-PROSE"
        ]
        assert {d["id"]: d["source"] for d in entry["blocker_details"]} == {
            "SA-EDGE": "dependency",
            "SA-PROSE": "prose",
        }

    def test_prose_line_on_a_non_blocked_child_does_not_gate(
        self, implement_mod
    ):
        """Only a ``blocked`` child is re-validated from prose (the open
        child is normal work, not a hold)."""
        children = [
            _prose_child(
                "SA-A",
                "**Blocked by (SorraAgents):** OSL-EXTERNAL",
                status="open",
            )
        ]
        h = ParentHarness(implement_mod, children)
        report = h.run()

        assert h.calls["phase_start"] == ["SA-A"]
        assert "unresolved_blockers" not in report
