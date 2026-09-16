"""Tests for the budget-exceeded result contract (SA-0MU32T6O0001UALR).

Verifies (child SA-0MU33X1D7008H5XL):

- AC1: when the parent-process elapsed-time guard trips, the affected child
  is recorded ``partial (budget exceeded)`` with a diagnostic naming the
  budget and the remediation (``--parent-timeout`` / ``AUDIT_PARENT_TIMEOUT``)
  — never a bare "Skipped due to audit timeout" skip that hides the cause.
- AC2: the checkpoint is written IMMEDIATELY when children are marked
  budget-exceeded (mid-phase persistence), and the checkpoint is kept when
  budget-exceeded children remain (a resumed run can re-audit them).
- AC3: no bare skip messages remain in the skip path.

The end-to-end resume flow (re-auditing budget-exceeded children without
re-running completed phases) is covered by SA-0MU33XG8P004GX8K.
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
from audit.scripts.checkpoint_store import (
    PHASE_CHILDREN,
    STATUS_COMPLETED,
    CheckpointStore,
)
from audit.tests.wl_helpers import stateful_wl_side_effect

HEAD = "b" * 40


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore (same pattern as the other
    audit test modules — SA-0MSCDC4750019G9Y)."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


# ---------------------------------------------------------------------------
# Unit: _budget_exceeded_ac_result (AC1)
# ---------------------------------------------------------------------------


class TestBudgetExceededAcResult:
    def test_verdict_is_partial_not_met(self):
        """AC1 + fail-closed: a budget-exceeded AC is `partial`, never `met`."""
        ac = audit_runner._budget_exceeded_ac_result(800.0, 710.0)
        assert ac["verdict"] == "partial"
        assert ac["verdict"] != "met"

    def test_text_is_canonical(self):
        """AC1: the canonical `partial (budget exceeded)` text is used."""
        ac = audit_runner._budget_exceeded_ac_result(800.0, 710.0)
        assert ac["text"] == "partial (budget exceeded)"
        assert ac["text"] == audit_runner.BUDGET_EXCEEDED_TEXT

    def test_evidence_names_budget_and_remediation(self):
        """AC1: the diagnostic names the elapsed time, the budget, and the
        --parent-timeout / AUDIT_PARENT_TIMEOUT remediation."""
        ac = audit_runner._budget_exceeded_ac_result(837.4, 710.0)
        evidence = ac["evidence"]
        assert "837s" in evidence
        assert "710s" in evidence
        assert "--parent-timeout" in evidence
        assert "AUDIT_PARENT_TIMEOUT" in evidence

    def test_evidence_explicitly_says_unverified(self):
        """AC1: the diagnostic states the ACs were NOT verified (no silent
        pass)."""
        evidence = audit_runner._budget_exceeded_ac_result(800.0, 710.0)["evidence"]
        assert "NOT verified" in evidence

    def test_no_bare_skip_text(self):
        """AC3: the canonical text is not the old bare skip message."""
        ac = audit_runner._budget_exceeded_ac_result(800.0, 710.0)
        assert "Skipped due to audit timeout" not in ac["text"]
        assert "Skipped due to audit timeout" not in ac["evidence"]


# ---------------------------------------------------------------------------
# Unit: checkpoint budget-exceeded persistence (AC2)
# ---------------------------------------------------------------------------


class TestCheckpointBudgetExceeded:
    def _open_store(self, tmp_path, issue_id="TEST-1", head=HEAD):
        return CheckpointStore(issue_id, head, tmp_path)

    def test_mark_persists_immediately(self, tmp_path):
        """AC2: mark_child_budget_exceeded writes the checkpoint file at once."""
        store = self._open_store(tmp_path)
        assert not store.path().exists()
        store.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        assert store.path().exists(), (
            "the checkpoint must be written immediately (mid-phase), not "
            "deferred to phase completion"
        )

    def test_marker_records_elapsed_and_budget(self, tmp_path):
        """AC1/AC2: the marker captures elapsed time, budget, and a timestamp."""
        store = self._open_store(tmp_path)
        store.mark_child_budget_exceeded("CHILD-1", 837.5, 710)
        markers = store.budget_exceeded_children()
        assert "CHILD-1" in markers
        m = markers["CHILD-1"]
        assert m["elapsed_s"] == 837.5
        assert m["budget_s"] == 710.0
        assert "recorded_at" in m

    def test_marker_survives_reopen(self, tmp_path):
        """AC2: a reopened store (resume) sees the budget-exceeded markers."""
        store = self._open_store(tmp_path)
        store.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        reopened = self._open_store(tmp_path)
        assert reopened.budget_exceeded_children()["CHILD-1"]["elapsed_s"] == 800.0

    def test_multiple_children_tracked(self, tmp_path):
        """AC1/AC2: multiple budget-exceeded children are recorded separately."""
        store = self._open_store(tmp_path)
        store.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        store.mark_child_budget_exceeded("CHILD-2", 900.0, 710)
        assert set(store.budget_exceeded_children()) == {"CHILD-1", "CHILD-2"}

    def test_empty_when_none_marked(self, tmp_path):
        store = self._open_store(tmp_path)
        assert store.budget_exceeded_children() == {}

    def test_phase_marking_does_not_disturb_markers(self, tmp_path):
        """mark_started/mark_completed must not clobber budget-exceeded data."""
        store = self._open_store(tmp_path)
        store.mark_started(PHASE_CHILDREN)
        store.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        store.mark_completed(PHASE_CHILDREN, {"child_results": []})
        assert store.budget_exceeded_children()["CHILD-1"]["elapsed_s"] == 800.0


class TestRecordChildBudgetExceeded:
    def test_none_checkpoint_is_noop(self):
        """A disabled checkpoint is a safe no-op (best-effort)."""
        audit_runner._record_child_budget_exceeded(None, "CHILD-1", 800.0, 710.0)

    def test_records_and_emits_trace(self, tmp_path, capsys):
        """AC2: the helper records the marker and emits a stderr trace."""
        store = CheckpointStore("TEST-1", HEAD, tmp_path)
        audit_runner._record_child_budget_exceeded(store, "CHILD-1", 800.0, 710.0)
        assert store.budget_exceeded_children()["CHILD-1"]["budget_s"] == 710.0
        err = capsys.readouterr().err
        assert "budget-exceeded child CHILD-1" in err


# ---------------------------------------------------------------------------
# Integration: cmd_issue records budget-exceeded + checkpoint on guard trip
# ---------------------------------------------------------------------------


class TestBudgetExceededIntegration:
    def _make_runner(self, child_stage="in_review"):
        """Mock runner: a parent with one child, and a resolvable git HEAD so
        checkpointing is enabled."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)

            # Checkpointing needs a resolvable git HEAD sha.
            if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                return SimpleNamespace(
                    returncode=0, stdout=HEAD, stderr=""
                )

            if "show" in cmd_str and "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n- AC1: parent criterion",
                            "status": "in_progress",
                        },
                        "children": [{
                            "id": "CHILD-1",
                            "title": "Child Issue",
                            "status": "completed",
                            "stage": child_stage,
                            "description": "## Acceptance Criteria\n- CAC1: child criterion",
                        }],
                    }),
                    stderr="",
                )

            # StatusLifecycle.show -> wl show <id> --json
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )

            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )

            # Fallback (audit-show etc.) -> no audit data
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
        return mock_runner

    def _run(self, tmp_path, elapsed=800.0, checkpoint=False):
        """Run cmd_issue with a fake clock reporting `elapsed` seconds since
        the guard start marker; pi and code quality are mocked."""
        clock = {"n": 0, "t0": 1000.0}

        def _fake_monotonic():
            clock["n"] += 1
            if clock["n"] == 1:
                return clock["t0"]
            return clock["t0"] + elapsed

        pi_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "mocked"}]',
        }

        def _passthrough_phase2(work_item, ac_results, child_results, **kwargs):
            return (ac_results, child_results, True)

        with (
            mock.patch.object(
                audit_runner.time, "monotonic", side_effect=_fake_monotonic
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", return_value=pi_result
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis",
                side_effect=_passthrough_phase2,
            ),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True,
                runner=self._make_runner(),
                json_mode=True,
                parent_timeout=None,
                audit_children=True,
                checkpoint_dir=str(tmp_path) if checkpoint else None,
            )

    def test_guard_trip_records_partial_and_keeps_checkpoint(self, tmp_path, capsys):
        """AC1/AC2: when the guard trips, the child is `partial (budget
        exceeded)`, the checkpoint is written with the marker, and the
        checkpoint is KEPT (not cleared) so a resume can re-audit the child."""
        rc = self._run(tmp_path, elapsed=800.0, checkpoint=True)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert rc == 0

        child = payload["children"][0]
        ac = child["ac_results"][0]
        assert ac["verdict"] == "partial"
        assert ac["text"] == "partial (budget exceeded)"
        assert "710s" in ac["evidence"]
        assert "--parent-timeout" in ac["evidence"]
        assert child.get("budget_exceeded") is True

        # The checkpoint file must exist with the per-child marker.
        cp_path = tmp_path / "TEST-1.checkpoint.json"
        assert cp_path.exists(), "checkpoint must be kept when budget-exceeded"
        raw = json.loads(cp_path.read_text(encoding="utf-8"))
        exceeded = raw["phases"][PHASE_CHILDREN]["budget_exceeded"]
        assert "CHILD-1" in exceeded
        assert exceeded["CHILD-1"]["budget_s"] == 710.0

        assert "budget-exceeded children remain; checkpoint kept" in captured.err

    def test_no_bare_skip_message_in_skip_path(self, tmp_path, capsys):
        """AC3: the skip path emits no bare 'Skipped due to audit timeout'."""
        self._run(tmp_path, elapsed=800.0, checkpoint=False)
        captured = capsys.readouterr()
        assert "Skipped due to audit timeout" not in captured.err
        assert "Skipped due to audit timeout" not in captured.out

    def test_normal_run_still_clears_checkpoint(self, tmp_path, capsys):
        """A normal run (no budget trip) still clears the checkpoint."""
        rc = self._run(tmp_path, elapsed=30.0, checkpoint=True)
        assert rc == 0
        assert not (tmp_path / "TEST-1.checkpoint.json").exists(), (
            "a completed audit must not leave a stale checkpoint behind"
        )