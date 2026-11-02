"""Regression tests: the invoked item's **own** outbound (depends-on) gate.

Contract (SA-0MVS1QS9J009DGPT):

- AC1: ``phase_parent`` resolves the parent's own outbound edges via
  ``wl_dep_blockers(<parent>)`` **before** classifying any child. An unmet
  (non-terminal) blocker means no child is started and no worktree created;
  the report carries ``parent_blocked_by`` (ids + statuses) and every child is
  reported as blocked.
- AC2: a parent whose ``status`` is ``blocked`` and which has an unmet
  outbound edge produces a dedicated ``blocked`` verdict — never an ``ok``
  report plus ``next_child`` — and the JSON names the blocker ids.
- AC3: a parent with no outbound edges, or only terminal (including
  soft-deleted) outbound blockers, behaves exactly as before (in-chain
  ordering and parent-advance rules unchanged).
- AC1/A2 for ``phase_drive``: a parent report carrying the blocked verdict is
  forwarded into the drive report and **no** child session is spawned — a soft
  stop, distinct from a hard ``failed_child`` failure.

Assumption A1 (plan default, producer confirmation pending): a parent whose
``status`` is ``blocked`` with **no** outbound edges also waits.

Blockers belonging to another project (a different id prefix, e.g.
``OSL-…`` for an ``SA-…`` item) are coordination notes, not gates — that is
the documented SA-0MSQBM2FK005NW1T assumption; same-project blockers on the
invoked item itself do gate.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
PARENT = "SA-PARENT001"


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py)."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_parent_outbound_gate", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_parent_outbound_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _neutralise_ambient_drive_guard(monkeypatch):
    """Neutralise an ambient ``IMPLEMENT_DRIVE_ACTIVE`` for this module.

    A driven child session exports the recursion guard; when the full suite
    runs inside that session, ``phase_drive`` would refuse to run at all and
    the drive tests here would fail for an unrelated reason.
    """
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)


def _child(
    work_item_id: str, status: str = "open", sort_index: int = 1000
) -> dict:
    return {
        "id": work_item_id,
        "title": f"Child {work_item_id}",
        "status": status,
        "stage": "intake_complete",
        "assignee": "",
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


class ParentHarness:
    """Run ``phase_parent`` with the external boundaries injected.

    ``blockers`` maps a work-item id to its outbound ``wl dep list`` edges —
    for the parent itself as well as for its children, which is exactly the
    gap this work item closes (the parent id was never queried before).
    """

    def __init__(
        self,
        mod,
        *,
        parent: dict | None = None,
        children: list[dict] | None = None,
        blockers: dict[str, list[dict]] | None = None,
    ):
        self.mod = mod
        self.parent = parent if parent is not None else {"id": PARENT, "title": "Epic"}
        self.children = list(children or [])
        self.blockers = {k: list(v) for k, v in (blockers or {}).items()}
        self.calls: dict[str, list] = {
            "start": [],
            "finish": [],
            "update_status": [],
            "dep_queries": [],
        }

    def _dep_blockers(self, work_item_id, **_):
        self.calls["dep_queries"].append(work_item_id)
        return list(self.blockers.get(work_item_id, []))

    def _invoke(self, action, child_id, **kwargs):
        self.calls[action].append(child_id)
        if action == "start":
            return {
                "success": True,
                "work_item_id": child_id,
                "worktree_path": f"/wt/{child_id}",
                "branch": f"wl-{child_id}",
            }
        if action == "finish":
            return {"success": True, "work_item_id": child_id}
        raise AssertionError(f"unexpected action {action}")

    def _update_status(self, work_item_id, status, stage=None, assignee=None, **_):
        self.calls["update_status"].append((work_item_id, status, stage))
        return {"success": True}

    def run(self) -> dict:
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(self.mod, "wl_show", return_value=self.parent)
            )
            stack.enter_context(
                mock.patch.object(
                    self.mod, "wl_show_children", return_value=self.children
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.mod, "wl_dep_blockers", side_effect=self._dep_blockers
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.mod, "_invoke_implement", side_effect=self._invoke
                )
            )
            stack.enter_context(
                mock.patch.object(self.mod, "_discover_worktree", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(self.mod, "_has_worktree_changes", return_value=False)
            )
            stack.enter_context(
                mock.patch.object(
                    self.mod.StatusLifecycle,
                    "update_status",
                    side_effect=self._update_status,
                )
            )
            stack.enter_context(
                mock.patch.object(self.mod, "wl_add_comment", return_value=True)
            )
            stack.enter_context(
                mock.patch.object(self.mod, "is_code_freeze_active", return_value=False)
            )
            return self.mod.phase_parent(PARENT, json_output=True)


# ===========================================================================
# AC1 / AC2: parent-level outbound blocker gates every child
# ===========================================================================


class TestParentOutboundGate:
    def test_unmet_outbound_blocker_starts_no_child(self, implement_mod):
        """A parent blocked by a non-terminal outbound edge starts nothing.

        The report is a dedicated ``blocked`` verdict naming the blocker — not
        an ``ok`` report with a ``next_child`` — and no worktree is created
        (AC1/AC2).
        """
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1")],
            blockers={PARENT: [_blocker("SA-DEP", status="in-progress")]},
        )
        report = h.run()

        assert report["success"] is True
        assert report["blocked"] is True
        assert report["parent_blocked_by"] == [
            {"id": "SA-DEP", "status": "in-progress", "title": "Blocker SA-DEP"}
        ]
        assert report["parent_blocked_reason"] == "outbound-dependency"
        assert "next_child" not in report
        assert "parent_advanced" not in report
        assert h.calls["start"] == []
        assert h.calls["update_status"] == []
        assert "SA-DEP" in report["message"]

    def test_parent_id_is_queried_before_any_child_is_classified(
        self, implement_mod
    ):
        """The parent's own edges are resolved through ``wl_dep_blockers``."""
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1")],
            blockers={PARENT: [_blocker("SA-DEP", status="open")]},
        )
        h.run()

        assert PARENT in h.calls["dep_queries"]

    def test_every_child_reported_blocked_when_parent_is_blocked(
        self, implement_mod
    ):
        """No child is classified as startable while the parent is blocked."""
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1"), _child("SA-C2", status="in_progress")],
            blockers={PARENT: [_blocker("SA-DEP", status="open")]},
        )
        report = h.run()

        assert [c["action"] for c in report["children"]] == [
            "blocked-parent",
            "blocked-parent",
        ]
        assert set(report["children"][0]) >= {"id", "title", "status", "action"}

    def test_blocked_status_and_unmet_edge_report_both_reasons(self, implement_mod):
        """A ``blocked`` parent with an unmet edge names both reasons."""
        h = ParentHarness(
            implement_mod,
            parent={"id": PARENT, "title": "Epic", "status": "blocked"},
            children=[_child("SA-C1")],
            blockers={PARENT: [_blocker("SA-DEP", status="in-progress")]},
        )
        report = h.run()

        assert report["blocked"] is True
        assert report["parent_blocked_reason"] == "outbound-dependency+status-blocked"
        assert [b["id"] for b in report["parent_blocked_by"]] == ["SA-DEP"]
        assert h.calls["start"] == []

    def test_blocked_status_without_edges_also_waits(self, implement_mod):
        """Assumption A1: a plain ``blocked`` parent gates starting too."""
        h = ParentHarness(
            implement_mod,
            parent={"id": PARENT, "title": "Epic", "status": "blocked"},
            children=[_child("SA-C1")],
        )
        report = h.run()

        assert report["blocked"] is True
        assert report["parent_blocked_by"] == []
        assert report["parent_blocked_reason"] == "status-blocked"
        assert h.calls["start"] == []
        assert h.calls["update_status"] == []

    def test_only_the_unmet_blockers_are_reported(self, implement_mod):
        """Terminal blockers are ignored; only the non-terminal one gates."""
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1")],
            blockers={
                PARENT: [
                    _blocker("SA-DONE", status="completed"),
                    _blocker("SA-DEP", status="open"),
                ]
            },
        )
        report = h.run()

        assert [b["id"] for b in report["parent_blocked_by"]] == ["SA-DEP"]


# ===========================================================================
# AC3: unchanged behaviour when the gate does not apply
# ===========================================================================


class TestParentGateUnchanged:
    def test_no_outbound_edges_behaves_as_before(self, implement_mod):
        h = ParentHarness(implement_mod, children=[_child("SA-C1")])
        report = h.run()

        assert "blocked" not in report
        assert "parent_blocked_by" not in report
        assert report.get("next_child") == "SA-C1"
        assert h.calls["start"] == ["SA-C1"]

    def test_terminal_outbound_blocker_does_not_gate(self, implement_mod):
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1")],
            blockers={PARENT: [_blocker("SA-DONE", status="completed")]},
        )
        report = h.run()

        assert "parent_blocked_by" not in report
        assert report.get("next_child") == "SA-C1"
        assert h.calls["start"] == ["SA-C1"]

    def test_soft_deleted_outbound_blocker_does_not_gate(self, implement_mod):
        """A soft-deleted blocker is terminal — it can never be implemented."""
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1")],
            blockers={PARENT: [_blocker("SA-GONE", status="deleted")]},
        )
        report = h.run()

        assert "parent_blocked_by" not in report
        assert report.get("next_child") == "SA-C1"

    def test_cross_project_outbound_blocker_is_a_coordination_note(
        self, implement_mod
    ):
        """A cross-project edge on the invoked item does not gate starting.

        This preserves the documented SA-0MSQBM2FK005NW1T assumption: edges to
        another project are coordination notes. The parent-level gate covers
        same-project (and ``status: blocked``) holds.
        """
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1")],
            blockers={PARENT: [_blocker("OSL-EXTERNAL", status="open")]},
        )
        report = h.run()

        assert "parent_blocked_by" not in report
        assert [b["id"] for b in report.get("coordination_blockers", [])] == [
            "OSL-EXTERNAL"
        ]
        assert report.get("next_child") == "SA-C1"
        assert h.calls["start"] == ["SA-C1"]

    def test_in_chain_ordering_still_applies_to_children(self, implement_mod):
        """The child-level in-chain ordering (SA-0MVO6UL70000HT0J) is intact."""
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-A", status="blocked", sort_index=2), _child("SA-B")],
            blockers={"SA-A": [_blocker("SA-B")]},
        )
        report = h.run()

        assert report.get("next_child") == "SA-B"
        assert h.calls["start"] == ["SA-B"]


# ===========================================================================
# Same-project out-of-chain child edge → child reported blocked
# ===========================================================================


class TestChildOutOfChainBlocker:
    def test_same_project_out_of_chain_blocker_reports_child_blocked(
        self, implement_mod
    ):
        """A child blocked by a same-project item outside the epic is not
        started (consistent with the ``phase_start`` leaf gate)."""
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1", sort_index=1), _child("SA-C2", sort_index=2)],
            blockers={"SA-C1": [_blocker("SA-OTHER", status="in-progress")]},
        )
        report = h.run()

        assert h.calls["start"] == ["SA-C2"]
        assert report.get("blocked_children") == ["SA-C1"]
        blocked = [
            c for c in report["children_processed"] if c["id"] == "SA-C1"
        ]
        assert blocked and blocked[0]["action"] == "blocked"

    def test_cross_project_child_blocker_remains_a_coordination_note(
        self, implement_mod
    ):
        """Cross-project edges on a child stay coordination notes (unchanged)."""
        h = ParentHarness(
            implement_mod,
            children=[_child("SA-C1")],
            blockers={"SA-C1": [_blocker("OSL-EXTERNAL", status="open")]},
        )
        report = h.run()

        assert h.calls["start"] == ["SA-C1"]
        assert report["children"][0]["external_blockers"] == ["OSL-EXTERNAL"]


# ===========================================================================
# phase_drive forwards the blocked verdict and spawns nothing
# ===========================================================================


class _ScriptedParentRunner:
    def __init__(self, reports: list[dict]):
        self.reports = list(reports)
        self.calls: list[tuple] = []

    def __call__(self, action, work_item_id, no_refactor=False, verbose=False):
        self.calls.append((action, work_item_id))
        if not self.reports:
            raise AssertionError("phase_parent called more times than scripted")
        return self.reports.pop(0)


class _RecordingSpawner:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, child_id, worktree_path, timeout, env, verbose=False):
        self.calls.append({"id": child_id, "worktree_path": worktree_path})
        return {"success": True, "returncode": 0, "reason": ""}


class TestDriveForwardsParentBlocked:
    def test_drive_stops_without_spawning_when_parent_is_blocked(
        self, implement_mod
    ):
        """The drive report carries ``parent_blocked_by`` and spawns nothing.

        Distinct from a hard failure: no ``failed_child`` is recorded and the
        soft outcome is reported as a blocked wait (AC1/A2).
        """
        m = implement_mod
        blocked_by = [
            {"id": "SA-DEP", "status": "in-progress", "title": "Blocker SA-DEP"}
        ]
        runner = _ScriptedParentRunner([
            {
                "success": True,
                "blocked": True,
                "parent_blocked_by": blocked_by,
                "parent_blocked_reason": "outbound-dependency",
                "message": (
                    "Parent SA-PARENT001 is blocked by unmet outbound "
                    "dependency: SA-DEP (in-progress)."
                ),
            },
        ])
        spawner = _RecordingSpawner()

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(m, "wl_show", return_value={"id": PARENT})
            )
            stack.enter_context(
                mock.patch.object(
                    m, "wl_show_children", return_value=[{"id": "SA-C1"}]
                )
            )
            stack.enter_context(
                mock.patch.object(m, "_discover_worktree", return_value="")
            )
            stack.enter_context(
                mock.patch.object(m, "wl_add_comment", return_value=True)
            )
            report = m.phase_drive(
                PARENT, parent_runner=runner, spawn_session=spawner
            )

        assert report["blocked"] is True
        assert report["parent_blocked_by"] == blocked_by
        assert report["parent_blocked_reason"] == "outbound-dependency"
        assert report["sessions_spawned"] == 0
        assert spawner.calls == []
        assert report["parent_advanced"] is False
        assert "failed_child" not in report
        assert report["success"] is True
        assert "SA-DEP" in report["message"]
