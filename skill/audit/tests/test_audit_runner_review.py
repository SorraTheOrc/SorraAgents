"""Regression tests for the child-exemption snapshot / no-side-effect-demotion
contract (SA-0MUJAPC680078396).

A parent audit that re-screens a child whose content changed must not:

1. evaluate its own closure against the child's *post-re-audit* stage (the
   stage may have been demoted by the parent-triggered cascade); or
2. demote a child that was already ``in_review``/``done`` at audit start.

Acceptance criteria pinned here:

* **AC1 (snapshot semantics)** — the parent's closure decision uses the
  child-exemption state captured at audit start, so an otherwise-complete
  parent is reported ``Yes`` even if a cascade-triggered child re-audit
  demotes a child.
* **AC2 (no side-effect demotion)** — a cascade-triggered child audit with a
  ``No`` verdict restores the child's pre-audit stage instead of pushing it
  back into the actionable queue.
* **AC3 (no regression to genuine blocking)** — a pre-review child (no
  snapshot exemption) still blocks the parent, and an *independent* audit of
  an ``in_review`` child still demotes it.
* **AC4 (verdict fidelity)** — the no-demotion path covers ``No`` verdicts
  driven by unverifiable/out-of-manifest ACs (a cascade re-audit of an
  already-exempt child is non-destructive).
* **AC5 (regression test)** — this file.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner
from audit.tests.wl_helpers import stateful_wl_side_effect


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


def _met_ac(text: str = "AC") -> dict:
    return {
        "text": text,
        "verdict": audit_runner.VERDICT_MET,
        "evidence": "verified",
    }


def _child(child_id: str, *, stage: str, status: str = "in_progress",
           child_audit_ready: bool | None = None,
           ac_results: list[dict] | None = None) -> dict:
    child = {
        "id": child_id,
        "title": f"Child {child_id}",
        "status": status,
        "stage": stage,
        "ac_results": ac_results if ac_results is not None else [_met_ac()],
    }
    if child_audit_ready is not None:
        child["child_audit_ready"] = child_audit_ready
    return child


class TestChildExemptionSnapshot:
    """AC1/AC3/AC5: the parent's closure decision honours the pre-audit
    child-exemption snapshot captured before any child re-audit runs."""

    # ------------------------------------------------------------------
    # _assemble_issue_report (markdown path)
    # ------------------------------------------------------------------

    def test_snapshot_keeps_parent_yes_when_child_demoted(self):
        """AC1: an in_review child demoted to plan_complete by a
        cascade-triggered re-audit does not block the parent when the child
        was exempt at audit start."""
        ac_results = [_met_ac("Parent AC")]
        # Post-re-audit: the child was demoted and its audit says not-ready.
        child_results = [
            _child("CHILD-1", stage="plan_complete", child_audit_ready=False)
        ]
        report = audit_runner._assemble_issue_report(
            {"id": "PARENT-1"}, ac_results, child_results,
            model="test-model", model_source="remote",
            snapshot_exempt={"CHILD-1"},
        )
        assert "Ready to close: Yes" in report

    def test_pre_review_child_still_blocks_without_snapshot(self):
        """AC3: a genuinely pre-review child (not snapshot-exempt) still
        blocks the parent's closure."""
        ac_results = [_met_ac("Parent AC")]
        child_results = [
            _child("CHILD-1", stage="plan_complete", child_audit_ready=False)
        ]
        report = audit_runner._assemble_issue_report(
            {"id": "PARENT-1"}, ac_results, child_results,
            model="test-model", model_source="remote",
            snapshot_exempt=set(),
        )
        assert "Ready to close: No" in report

    def test_snapshot_child_with_not_ready_audit_does_not_block(self):
        """AC1/AC4: a snapshot-exempt child whose persisted audit says
        not-ready no longer contributes a blocking verdict."""
        ac_results = [_met_ac("Parent AC")]
        child_results = [
            _child("CHILD-1", stage="plan_complete", child_audit_ready=False)
        ]
        # Without the snapshot the not-ready verdict blocks; with it, it does not.
        blocked = audit_runner._assemble_issue_report(
            {"id": "PARENT-1"}, ac_results, child_results,
            model="test-model", model_source="remote",
        )
        exempt = audit_runner._assemble_issue_report(
            {"id": "PARENT-1"}, ac_results, child_results,
            model="test-model", model_source="remote",
            snapshot_exempt={"CHILD-1"},
        )
        assert "Ready to close: No" in blocked
        assert "Ready to close: Yes" in exempt

    # ------------------------------------------------------------------
    # _build_issue_json (JSON path — must not drift from markdown)
    # ------------------------------------------------------------------

    def test_json_snapshot_keeps_ready_true_when_child_demoted(self):
        """AC1: the JSON verdict path honours the snapshot identically."""
        ac_results = [_met_ac("Parent AC")]
        child_results = [
            _child("CHILD-1", stage="plan_complete", child_audit_ready=False)
        ]
        payload = audit_runner._build_issue_json(
            {"id": "PARENT-1"}, ac_results, child_results,
            snapshot_exempt={"CHILD-1"},
        )
        assert payload["ready_to_close"] is True

    def test_json_pre_review_child_still_blocks_without_snapshot(self):
        """AC3: the JSON verdict path still blocks a genuinely pre-review
        child."""
        ac_results = [_met_ac("Parent AC")]
        child_results = [
            _child("CHILD-1", stage="plan_complete", child_audit_ready=False)
        ]
        payload = audit_runner._build_issue_json(
            {"id": "PARENT-1"}, ac_results, child_results,
            snapshot_exempt=set(),
        )
        assert payload["ready_to_close"] is False

    # ------------------------------------------------------------------
    # _has_phase1_blocking_issues (Phase 2 gate)
    # ------------------------------------------------------------------

    def test_phase1_gate_exempts_snapshot_child(self):
        """AC1: the Phase 2 gate treats a snapshot-exempt child as
        non-blocking even after demotion."""
        child_results = [
            _child("CHILD-1", stage="plan_complete", child_audit_ready=False)
        ]
        blocked, _reason = audit_runner._has_phase1_blocking_issues(
            [], child_results, snapshot_exempt={"CHILD-1"},
        )
        assert blocked is False

    def test_phase1_gate_blocks_pre_review_child(self):
        """AC3: without the snapshot, a pre-review child blocks the gate."""
        child_results = [
            _child("CHILD-1", stage="plan_complete", child_audit_ready=False)
        ]
        blocked, reason = audit_runner._has_phase1_blocking_issues(
            [], child_results, snapshot_exempt=set(),
        )
        assert blocked is True
        assert "CHILD-1" in reason or "Child" in reason

    # ------------------------------------------------------------------
    # _capture_exempt_children_snapshot
    # ------------------------------------------------------------------

    @staticmethod
    def _make_ctx(children: list[dict]) -> audit_runner._AuditContext:
        ctx = audit_runner._AuditContext(
            issue_id="PARENT-1", persist=False, timeout=None,
            parent_timeout=None, pi_bin="pi", model=None,
            model_source="default", runner=lambda cmd: None,
            json_mode=False, debug_log=None, force=False,
            worklog_dir=None, batch_phase2=False, green_run=None,
            audit_children=False, max_child_audits=None, run_tests=False,
        )
        ctx.children = children
        return ctx

    def test_capture_collects_in_review_and_done_children_only(self):
        """The snapshot captures children that were in_review or
        completed/done at audit start — and nothing else."""
        ctx = self._make_ctx([
            _child("CHILD-IR", stage="in_review", status="in_progress"),
            _child("CHILD-DONE", stage="done", status="completed"),
            _child("CHILD-PLAN", stage="plan_complete", status="open"),
            _child("CHILD-BLOCKED", stage="plan_complete", status="blocked"),
        ])
        audit_runner._capture_exempt_children_snapshot(ctx)
        assert ctx.snapshot_exempt_children == {"CHILD-IR", "CHILD-DONE"}


class TestCascadeNoSideEffectDemotion:
    """AC2/AC3/AC4/AC5: a parent-triggered cascade re-audit must not demote an
    already-exempt child, while an independent (non-cascade) audit of the same
    child keeps its normal lifecycle."""

    def _make_runner(self, updates, status, stage, parent_id=None):
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str:
                updates.append(list(cmd))
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str:
                wi = {"id": "TEST-1", "status": status, "stage": stage}
                if parent_id is not None:
                    wi["parentId"] = parent_id
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True, "workItem": wi}),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1", "description": "",
                            "status": status, "stage": stage,
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
        return mock_runner

    def _run_issue(self, updates, report, **runner_kwargs):
        runner = self._make_runner(updates, **runner_kwargs)
        with (
            mock.patch.object(
                audit_runner, "_assemble_issue_report", return_value=report,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=runner,
            )

    @staticmethod
    def _last_update(updates):
        assert updates, "expected at least one wl update command"
        cmd = updates[-1]
        if cmd[:1] == ["wl"] and len(cmd) >= 3 and cmd[1] == "--worklog-dir":
            cmd = ["wl"] + cmd[3:]
        return cmd

    def test_cascade_no_verdict_restores_in_review_child(self):
        """AC2: a cascade-triggered 'No' on an in_review child restores
        completed/in_review instead of demoting to open/plan_complete."""
        updates = []
        with mock.patch.dict(
            os.environ, {audit_runner.AUDIT_CASCADE_AUDIT_ENV: "1"}, clear=False,
        ):
            self._run_issue(
                updates,
                report="Ready to close: No\n\n## Summary\nunverifiable AC.",
                status="completed", stage="in_review",
            )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--assignee", "", "--json",
        ]

    def test_cascade_no_verdict_restores_done_child(self):
        """AC2: a cascade-triggered 'No' on a completed/done child keeps the
        terminal stage."""
        updates = []
        with mock.patch.dict(
            os.environ, {audit_runner.AUDIT_CASCADE_AUDIT_ENV: "1"}, clear=False,
        ):
            self._run_issue(
                updates,
                report="Ready to close: No\n\n## Summary\nunverifiable AC.",
                status="completed", stage="done",
            )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "done",
            "--assignee", "", "--json",
        ]

    def test_independent_no_verdict_demotes_in_review_child(self):
        """AC3: an audit NOT triggered by a parent cascade keeps the normal
        demotion — an explicit independent 'No' still returns the item to the
        actionable queue."""
        updates = []
        # Ensure no stale cascade marker leaks in from the environment.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(audit_runner.AUDIT_CASCADE_AUDIT_ENV, None)
            self._run_issue(
                updates,
                report="Ready to close: No\n\n## Summary\ngenuine gap.",
                status="completed", stage="in_review",
            )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "open", "--stage", "plan_complete", "--json",
        ]

    def test_cascade_no_verdict_demotes_pre_review_child(self):
        """AC3: a cascade-triggered 'No' on a child that was already
        pre-review is still a genuine block — it demotes as before."""
        updates = []
        with mock.patch.dict(
            os.environ, {audit_runner.AUDIT_CASCADE_AUDIT_ENV: "1"}, clear=False,
        ):
            self._run_issue(
                updates,
                report="Ready to close: No\n\n## Summary\ngenuine gap.",
                status="open", stage="plan_complete",
            )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "open", "--stage", "plan_complete", "--json",
        ]

    def test_cascade_yes_verdict_still_advances(self):
        """AC2 guard: the cascade suppression only affects 'No' verdicts — a
        passing cascade re-audit still advances the item to the review queue."""
        updates = []
        with mock.patch.dict(
            os.environ, {audit_runner.AUDIT_CASCADE_AUDIT_ENV: "1"}, clear=False,
        ):
            self._run_issue(
                updates,
                report="Ready to close: Yes\n\n## Summary\nall met.",
                status="completed", stage="in_review",
            )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--needs-producer-review", "yes", "--json",
        ]
