"""End-to-end regression: budget exhaustion → checkpoint → resume.

Simulates the dominant genuine audit failure mode (SA-0MU32T6O0001UALR): the
parent-process elapsed-time budget trips mid-phase, a child is recorded
``partial (budget exceeded)`` instead of being silently skipped, a resumable
checkpoint is written, and a subsequent run re-audits exactly that child
without re-running any completed phase.

Covers child SA-0MU33W0T3008FSD1 AC1-AC4:

1. Budget exhaustion → the child is recorded ``partial (budget exceeded)``.
2. The checkpoint file is written at the budget-exhaustion point (with the
   per-child marker) and KEPT for resume.
3. The resume re-audits the previously budget-exceeded child to a real
   verdict and clears the checkpoint.
4. Completed phases (parent Phase 1 screening and the parent Phase 2 deep
   analysis) are skipped on resume — never re-run.

The module deliberately drives the real ``cmd_issue`` pipeline so the
recording, checkpoint persistence and resume wiring are exercised end-to-end
(with a mocked Pi/code-quality boundary and a fake monotonic clock).
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
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
    PHASE_PARENT,
    STATUS_COMPLETED,
    CheckpointStore,
)
from audit.tests.wl_helpers import stateful_wl_side_effect

HEAD = "e" * 40


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore (SA-0MSCDC4750019G9Y)."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


def _checkpoint_path(tmp_path: Path, issue_id: str = "TEST-1") -> Path:
    return tmp_path / f"{issue_id}.checkpoint.json"


def _read_markers(tmp_path: Path, issue_id: str = "TEST-1") -> dict:
    raw = json.loads(_checkpoint_path(tmp_path, issue_id).read_text(encoding="utf-8"))
    return raw["phases"][PHASE_CHILDREN].get("budget_exceeded") or {}


class _Harness:
    """Drives cmd_issue with a controllable clock and captured screens."""

    def __init__(self, tmp_path: Path, child_stage: str = "in_review",
                 child_status: str = "completed"):
        self.tmp_path = tmp_path
        self.child_stage = child_stage
        self.child_status = child_status
        self.screen_contexts: list[str] = []
        self.deep_calls: list[dict] = []

    def _make_runner(self):
        runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                return SimpleNamespace(returncode=0, stdout=HEAD, stderr="")
            if "show" in cmd_str and "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n- AC1: parent",
                            "status": "in_progress",
                        },
                        "children": [{
                            "id": "CHILD-1",
                            "title": "Child Issue",
                            "status": self.child_status,
                            "stage": self.child_stage,
                            "description": "## Acceptance Criteria\n- CAC1: child",
                        }],
                    }),
                    stderr="",
                )
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
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        runner.side_effect = stateful_wl_side_effect(_side_effect)
        return runner

    def _fake_screen(self, issue_id, context, prompt, model, pi_bin, debug_log,
                     timeout, ac_fallback_used, on_runtime_error, failure_label,
                     child_screen=False, enable_tools=True, priority=None):
        self.screen_contexts.append(context)
        return (
            {"verdict": "met", "evidence": "x"},
            [{"index": 0, "verdict": "met", "evidence": "screened"}],
            "raw",
        )

    def _fake_deep(self, issue, ac_results, child_results, **kwargs):
        self.deep_calls.append(kwargs)
        return ac_results, child_results, True

    def run(self, elapsed: float, force: bool = False):
        """Run one cmd_issue, with a fake clock reporting *elapsed* seconds
        after the guard-start marker. Returns (rc, stdout, stderr)."""
        clock = {"n": 0, "t0": 1000.0}

        def _fake_monotonic():
            clock["n"] += 1
            if clock["n"] == 1:
                return clock["t0"]
            return clock["t0"] + elapsed

        with (
            mock.patch.object(
                audit_runner.time, "monotonic", side_effect=_fake_monotonic
            ),
            mock.patch.object(
                audit_runner, "_call_phase1_screen", side_effect=self._fake_screen
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis", side_effect=self._fake_deep
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = audit_runner.cmd_issue(
                    "TEST-1", persist=False, force=force,
                    runner=self._make_runner(), json_mode=True,
                    parent_timeout=None, audit_children=True,
                    checkpoint_dir=str(self.tmp_path),
                )
        return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# AC1 + AC2: budget exhaustion records the child and writes the checkpoint
# ---------------------------------------------------------------------------


class TestBudgetExhaustionRecording:
    def test_child_recorded_partial_and_checkpoint_written(self, tmp_path):
        """AC1/AC2: the guard trips → child `partial (budget exceeded)` with a
        diagnostic naming the budget + remediation, and the checkpoint file is
        written with the marker and kept for resume."""
        h = _Harness(tmp_path)
        rc, out, err = h.run(elapsed=800.0)

        assert rc == 0
        payload = json.loads(out)
        ac = payload["children"][0]["ac_results"][0]
        assert ac["verdict"] == "partial"
        assert ac["text"] == "partial (budget exceeded)"
        assert "710s" in ac["evidence"]
        assert "--parent-timeout" in ac["evidence"]
        assert "AUDIT_PARENT_TIMEOUT" in ac["evidence"]
        assert payload["children"][0]["budget_exceeded"] is True

        # Checkpoint written at the exhaustion point and KEPT (markers remain).
        assert _checkpoint_path(tmp_path).exists()
        markers = _read_markers(tmp_path)
        assert "CHILD-1" in markers
        assert markers["CHILD-1"]["budget_s"] == 710.0

        # AC3 (no bare skip): the old hidden-cause message is gone.
        assert "Skipped due to audit timeout" not in err
        assert "Skipped due to audit timeout" not in out

    def test_completed_phases_recorded_in_checkpoint(self, tmp_path):
        """The prior run records phase1_parent/phase1_children/phase2 so a
        resume can skip them (AC4 prerequisite)."""
        h = _Harness(tmp_path)
        h.run(elapsed=800.0)
        store = CheckpointStore("TEST-1", HEAD, tmp_path)
        assert store.is_resuming is True
        assert store.phase_status(PHASE_PARENT) == STATUS_COMPLETED
        assert store.phase_status(PHASE_CHILDREN) == STATUS_COMPLETED


# ---------------------------------------------------------------------------
# AC3 + AC4: resume re-audits the child without re-running completed phases
# ---------------------------------------------------------------------------


class TestBudgetExceededResume:
    def test_resume_completes_budget_exceeded_child(self, tmp_path):
        """AC3: a resumed run re-audits the budget-exceeded child to a real
        verdict and clears the checkpoint on success."""
        h = _Harness(tmp_path)
        h.run(elapsed=800.0)  # run 1: budget trip
        assert _read_markers(tmp_path)  # marker persisted

        h.screen_contexts.clear()
        rc, out, err = h.run(elapsed=30.0)  # run 2: resume within budget

        assert rc == 0
        payload = json.loads(out)
        ac = payload["children"][0]["ac_results"][0]
        assert ac["verdict"] == "met"
        assert "budget exceeded" not in ac["text"].lower()
        # The child was re-screened and the marker cleared → checkpoint gone.
        assert any(c.startswith("child:") for c in h.screen_contexts)
        assert not _checkpoint_path(tmp_path).exists()
        assert "[checkpoint] Re-auditing 1 budget-exceeded child(ren)" in err

    def test_resume_skips_completed_phases(self, tmp_path):
        """AC4: the resumed run does NOT re-run the completed parent Phase 1
        screening and does NOT re-run the parent Phase 2 deep analysis."""
        h = _Harness(tmp_path)
        h.run(elapsed=800.0)

        h.screen_contexts.clear()
        h.deep_calls.clear()
        h.run(elapsed=30.0)

        # Parent Phase 1 screening (context "parent") was skipped entirely.
        assert "parent" not in h.screen_contexts
        assert any(c.startswith("child:") for c in h.screen_contexts)
        # Parent Phase 2 deep analysis was NOT re-run (children-only resume).
        assert h.deep_calls, "child deep analysis should still run"
        assert all(kwargs.get("skip_parent_deep") is True for kwargs in h.deep_calls)

    def test_resume_is_idempotent_when_no_markers_remain(self, tmp_path):
        """After a successful resume the checkpoint is gone; a third run is a
        fresh (non-resuming) run — no stale state is reused."""
        h = _Harness(tmp_path)
        h.run(elapsed=800.0)
        h.run(elapsed=30.0)
        assert not _checkpoint_path(tmp_path).exists()

        h.screen_contexts.clear()
        rc, _out, _err = h.run(elapsed=30.0)
        assert rc == 0
        # Fresh run: parent screening runs again (no checkpoint to resume).
        assert "parent" in h.screen_contexts
