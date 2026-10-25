"""Regression tests for parent-persisted child audits that demote exempt
children (SA-0MVI35BIU002MTZQ, recurrence of SA-0MUJAPC680078396).

``wl audit-set --ready-to-close no`` unconditionally reverts a
``completed``/``in_review`` item to ``open``/``plan_complete``. For a child
screened as a side effect of its parent's audit that is a destructive
side-effect: the child's lifecycle is owned by its own review, not by the
parent's read-only pass.

Acceptance criteria pinned here:

* **AC1 (lifecycle-neutral persistence)** — persisting a child audit via
  ``_persist_child_audit`` never changes a child's status/stage when the
  child was ``completed``/``in_review`` at audit start.
* **AC2 (tainted verdicts never demote)** — an infra-fallback-tainted child
  verdict (persisted as ``No``) also restores the captured pre-audit state.
* **AC3 (no side-effect lifecycle deltas)** — the full child set's
  ``(status, stage)`` is byte-identical before and after a parent audit.
* **F3a/F3b (corrected RCA §5, SA-0MVI35BIU002MTZQ)** — a snapshot-exempt
  child's tainted child ACs do not force the parent to ``No``; a non-exempt
  child's tainted ACs are named as the blocker with fallback provenance
  (covered in ``test_audit_runner_review.py``).
* **AC5 (observed scenario, end to end)** — parent + N children all
  ``in_review`` with tainted ``No`` child verdicts: children end
  ``completed``/``in_review``, the parent verdict is ``Yes`` and no
  ``blocked`` appears.
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


def _strip_worklog_flags(cmd: list[str]) -> list[str]:
    """Drop ``--worklog-dir <path>`` pairs from a mocked wl argv."""
    out: list[str] = []
    skip = False
    for token in cmd:
        if skip:
            skip = False
            continue
        if token == "--worklog-dir":
            skip = True
            continue
        out.append(token)
    return out


class FakeWorklog:
    """Minimal stateful worklog modelling ``wl show`` / ``wl update`` / revert.

    ``audit-set --ready-to-close no`` is modelled by
    :meth:`apply_audit_set`, which mirrors the real ``revertToPlanComplete``
    primitive: only an item in exactly ``completed``/``in_review`` is reverted
    to ``open``/``plan_complete``. ``wl update`` mutates the modelled state so
    the runner's readback verification observes the applied transition.
    """

    def __init__(self, states: dict[str, dict[str, str]]):
        self.states = {key: dict(value) for key, value in states.items()}
        self.updates: list[list[str]] = []

    def snapshot(self) -> dict[str, tuple[str, str]]:
        return {
            key: (value.get("status", ""), value.get("stage", ""))
            for key, value in self.states.items()
        }

    def apply_audit_set(self, item_id: str, ready: bool) -> None:
        """Model ``wl audit-set --ready-to-close``'s reversion side effect."""
        if ready:
            return
        state = self.states.get(item_id)
        if state is None:
            return
        if state.get("status") == "completed" and state.get("stage") == "in_review":
            state["status"] = "open"
            state["stage"] = "plan_complete"

    def runner(self, cmd):
        args = _strip_worklog_flags(list(cmd))
        if args[:2] == ["wl", "show"]:
            item_id = args[2]
            state = self.states.get(item_id)
            if state is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            item = {"id": item_id, **state}
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "workItem": item}),
                stderr="",
            )
        if args[:2] == ["wl", "update"]:
            item_id = args[2]
            state = self.states.setdefault(item_id, {})
            if "--status" in args:
                state["status"] = args[args.index("--status") + 1]
            if "--stage" in args:
                state["stage"] = args[args.index("--stage") + 1]
            self.updates.append(args)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )


def _met_ac(text: str = "AC") -> dict:
    return {"text": text, "verdict": audit_runner.VERDICT_MET, "evidence": "ok"}


def _partial_ac(text: str = "AC") -> dict:
    """A child AC degraded to partial by an infrastructure fallback."""
    return {
        "text": text,
        "verdict": "partial",
        "evidence": "partial (timed out \u2014 manual review required)",
    }


def _in_review_child(child_id: str, *, ac_results: list[dict] | None = None) -> dict:
    child = {
        "id": child_id,
        "title": f"Child {child_id}",
        "status": "completed",
        "stage": "in_review",
        "ac_results": ac_results if ac_results is not None else [_met_ac()],
    }
    return child


def _persist_with_revert(worklog: FakeWorklog):
    """A persist_audit stub that runs ``audit-set`` against *worklog*."""

    def _fake_persist(issue_id, report, worklog_dir=None):
        ready = audit_runner._parse_ready_to_close(report) == "yes"
        worklog.apply_audit_set(issue_id, ready)
        return 0

    return _fake_persist


class TestChildPersistLifecycleNeutral:
    """AC1/AC2: a parent-persisted child audit cannot change a child's
    lifecycle when the child was completed/in_review at audit start."""

    def test_in_review_child_restored_after_no_verdict(self):
        """AC1: an in_review child demoted by a 'No' persist is restored."""
        worklog = FakeWorklog({"CHILD-1": {"status": "completed", "stage": "in_review"}})
        before = worklog.snapshot()
        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_persist_with_revert(worklog)
        ):
            rc, _report = audit_runner._persist_child_audit(
                "CHILD-1", "Child one", "completed", "in_review",
                [_partial_ac()],
                runner=worklog.runner,
            )
        assert rc == 0
        assert worklog.snapshot() == before
        assert worklog.states["CHILD-1"] == {
            "status": "completed", "stage": "in_review",
        }

    def test_tainted_no_verdict_does_not_demote(self):
        """AC2: an infra-fallback-tainted 'No' also restores the child."""
        worklog = FakeWorklog({"CHILD-2": {"status": "completed", "stage": "in_review"}})
        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_persist_with_revert(worklog)
        ):
            audit_runner._persist_child_audit(
                "CHILD-2", "Child two", "completed", "in_review",
                [_partial_ac()],
                runner=worklog.runner,
            )
        assert worklog.states["CHILD-2"] == {
            "status": "completed", "stage": "in_review",
        }

    def test_pre_review_child_not_promoted(self):
        """A persist never promotes a pre-review child: no lifecycle delta."""
        worklog = FakeWorklog({"CHILD-3": {"status": "open", "stage": "plan_complete"}})
        before = worklog.snapshot()
        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_persist_with_revert(worklog)
        ):
            audit_runner._persist_child_audit(
                "CHILD-3", "Child three", "open", "plan_complete",
                [_partial_ac()],
                runner=worklog.runner,
            )
        assert worklog.snapshot() == before

    def test_ready_verdict_does_not_change_lifecycle(self):
        """A 'Yes' persist is already lifecycle-neutral (audit-set never
        promotes); the guard leaves the state untouched."""
        worklog = FakeWorklog({"CHILD-4": {"status": "completed", "stage": "in_review"}})
        before = worklog.snapshot()
        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_persist_with_revert(worklog)
        ):
            audit_runner._persist_child_audit(
                "CHILD-4", "Child four", "completed", "in_review",
                [_met_ac()],
                runner=worklog.runner,
            )
        assert worklog.snapshot() == before


class TestFullChildSetRegression:
    """AC3/AC5: the full child set is byte-identical after a parent audit and
    the parent verdict is Yes for snapshot-exempt children."""

    def test_full_child_set_byte_identical_after_persisting_all(self):
        """AC3: persist every child with a tainted 'No'; the whole
        (status, stage) set is unchanged and nothing becomes blocked."""
        child_ids = [f"CHILD-{i}" for i in range(1, 6)]
        worklog = FakeWorklog({
            cid: {"status": "completed", "stage": "in_review"} for cid in child_ids
        })
        before = worklog.snapshot()
        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_persist_with_revert(worklog)
        ):
            for cid in child_ids:
                audit_runner._persist_child_audit(
                    cid, f"Child {cid}", "completed", "in_review",
                    [_partial_ac()],
                    runner=worklog.runner,
                )
        after = worklog.snapshot()
        assert after == before
        assert all(status != "blocked" for status, _stage in after.values())

    def test_observed_scenario_parent_yes_children_in_review(self):
        """AC5: parent + N in_review children with tainted 'No' verdicts —
        children end completed/in_review, the parent verdict is Yes and no
        child is blocked."""
        child_ids = ["CHILD-F1", "CHILD-F2", "CHILD-F3"]
        children = [
            _in_review_child(cid, ac_results=[_partial_ac()]) for cid in child_ids
        ]
        # Pre-audit snapshot exactly as the runner captures it.
        ctx = audit_runner._AuditContext(
            issue_id="PARENT-1", persist=True, timeout=None,
            parent_timeout=None, pi_bin="pi", model=None,
            model_source="default", runner=lambda cmd: None,
            json_mode=False, debug_log=None, force=False,
            worklog_dir=None, batch_phase2=False, green_run=None,
            audit_children=False, max_child_audits=None, run_tests=False,
        )
        ctx.children = [dict(c) for c in children]
        audit_runner._capture_exempt_children_snapshot(ctx)

        worklog = FakeWorklog({
            cid: {"status": "completed", "stage": "in_review"} for cid in child_ids
        })
        before = worklog.snapshot()
        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_persist_with_revert(worklog)
        ):
            for child in children:
                audit_runner._persist_child_audit(
                    child["id"], child["title"], child["status"],
                    child["stage"], child["ac_results"],
                    runner=worklog.runner,
                )
        # Children are byte-identically in_review after the run.
        assert worklog.snapshot() == before
        assert all(
            (state["status"], state["stage"]) == ("completed", "in_review")
            for state in worklog.states.values()
        )
        # The parent's own assessment treats the children as exempt, so the
        # report verdict is Yes (the tainted child screens do not block).
        child_results = [
            {
                **child,
                "child_audit_ready": False,
                "child_audit_not_ready": True,
            }
            for child in children
        ]
        report = audit_runner._assemble_issue_report(
            {"id": "PARENT-1"}, [_met_ac("Parent AC")], child_results,
            snapshot_exempt=ctx.snapshot_exempt_children,
        )
        assert "Ready to close: Yes" in report
        assert "blocked" not in report.lower()
