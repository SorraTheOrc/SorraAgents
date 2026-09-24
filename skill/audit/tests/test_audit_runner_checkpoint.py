"""Integration tests: audit runner phase checkpoints (SA-0MT6EZUS9004FJ9T).

Verifies the checkpoint contract wired into ``audit_runner.py``:

1. ``_phase1_parent_screening`` saves a ``phase1_parent`` checkpoint with the
   parent AC results, and a resumed run SKIPS the Phase 1 Pi call entirely.
2. ``_phase_children`` saves ``phase1_children`` (after child orchestration)
   and ``phase2`` (final state); a resumed run skips completed phases and
   only re-executes the incomplete segment — never duplicating Pi work and
   never reusing stale state from a different git HEAD.
3. ``cmd_issue`` clears the checkpoint file after a successful full run so a
   completed audit never leaves stale partial results behind.
4. Resume emits a clear trace to stderr (which phase completed / which was
   interrupted) — the timeout-reporting surface for operators.
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
    PHASE_PARENT,
    PHASE_PHASE2,
    STATUS_COMPLETED,
    CheckpointStore,
)
from audit.tests.wl_helpers import stateful_wl_side_effect

HEAD_BASE = "c" * 40
HEAD_OTHER = "d" * 40


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore (same pattern as the other
    audit test modules — SA-0MSCDC4750019G9Y)."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


def _open_store(tmp_path, issue_id="TEST-1", head=HEAD_BASE, **kw):
    return CheckpointStore(issue_id, head, tmp_path, **kw)


def _make_ctx(checkpoint=None, **overrides):
    runner = mock.MagicMock()
    runner.side_effect = None
    defaults = {
        "issue_id": "TEST-1", "persist": False, "timeout": None,
        "parent_timeout": None,
        "pi_bin": "pi", "model": None, "model_source": "default",
        "runner": runner,
        "json_mode": False, "debug_log": None, "force": False,
        "worklog_dir": None,
        "batch_phase2": False, "green_run": None, "audit_children": False,
        "max_child_audits": None, "run_tests": False,
    }
    defaults.update(overrides)
    ctx = audit_runner._AuditContext(**defaults)
    ctx.checkpoint = checkpoint
    ctx.acs = ["AC1: x"]
    ctx.cq_findings = []
    ctx.fp_screen_results = []
    ctx.work_item = {
        "id": "TEST-1", "title": "T",
        "description": "## Acceptance Criteria\n- AC1: x",
        "effort": "Large", "risk": "High",
    }
    ctx.ac_results = [{"text": "AC1: x", "verdict": "met", "evidence": "e"}]
    ctx.children = []
    ctx.resolved_model = "model"
    ctx.resolved_phase1_model = "model"
    return ctx


# ---------------------------------------------------------------------------
# Phase 1 parent screening
# ---------------------------------------------------------------------------


class TestPhase1ParentCheckpoint:
    def _patch_parent_screen(self, verdict="met"):
        def _fake_call(issue_id, context, prompt, model, pi_bin, debug_log,
                       timeout, ac_fallback_used, on_runtime_error,
                       failure_label, child_screen=False, enable_tools=True,
                       priority=None):
            return (
                {"verdict": verdict, "evidence": "x"},
                [{"index": 0, "verdict": verdict, "evidence": "x"}],
                "raw",
            )
        return mock.patch.object(
            audit_runner, "_call_phase1_screen", side_effect=_fake_call
        )

    def test_saves_checkpoint_after_parent_screening(self, tmp_path, capsys):
        store = _open_store(tmp_path)
        ctx = _make_ctx(checkpoint=store)
        with self._patch_parent_screen(), mock.patch.object(
            audit_runner, "_validate_file_scope_manifest", return_value=None
        ), mock.patch.object(
            audit_runner, "_build_file_scope_manifest", return_value="manifest"
        ):
            audit_runner._phase1_parent_screening(ctx)
        assert store.phase_status(PHASE_PARENT) == STATUS_COMPLETED
        state = store.accumulated_state()
        assert state["ac_results"] == ctx.ac_results
        assert "Phase 1 parent screening completed" in capsys.readouterr().err

    def test_resume_skips_parent_screening_pi_call(self, tmp_path, capsys):
        # A prior run completed phase1_parent and was then killed.
        store = _open_store(tmp_path)
        saved_acs = [{"text": "AC1: x", "verdict": "met", "evidence": "saved"}]
        store.mark_completed(PHASE_PARENT, {"ac_results": saved_acs})
        reopened = _open_store(tmp_path)
        assert reopened.is_resuming is True
        ctx = _make_ctx(checkpoint=reopened)
        with self._patch_parent_screen() as screen, mock.patch.object(
            audit_runner, "_validate_file_scope_manifest", return_value=None
        ):
            audit_runner._phase1_parent_screening(ctx)
        screen.assert_not_called()
        assert ctx.ac_results == saved_acs
        err = capsys.readouterr().err
        assert "Skipping completed phase" in err

    def test_no_checkpoint_means_normal_screening(self, tmp_path):
        ctx = _make_ctx(checkpoint=None)
        with self._patch_parent_screen() as screen, mock.patch.object(
            audit_runner, "_validate_file_scope_manifest", return_value=None
        ), mock.patch.object(
            audit_runner, "_build_file_scope_manifest", return_value="manifest"
        ):
            audit_runner._phase1_parent_screening(ctx)
        screen.assert_called_once()
        assert ctx.ac_results[0]["verdict"] == "met"


# ---------------------------------------------------------------------------
# Phase children (orchestration + Phase 2 gate)
# ---------------------------------------------------------------------------


class TestPhaseChildrenCheckpoint:
    def _deep_side_effect(self, phase2_completed=True):
        def _fake_deep(issue, ac_results, child_results, **kw):
            return ac_results, child_results, phase2_completed
        return _fake_deep

    def test_run_saves_children_and_phase2_checkpoints(self, tmp_path):
        """A normal (non-resumed) children phase saves both checkpoints."""
        store = _open_store(tmp_path)
        ctx = _make_ctx(checkpoint=store)
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=self._deep_side_effect(),
        ):
            rc = audit_runner._phase_children(ctx)
        assert rc is None
        assert store.phase_status(PHASE_CHILDREN) == STATUS_COMPLETED
        assert store.phase_status(PHASE_PHASE2) == STATUS_COMPLETED
        state = store.accumulated_state()
        assert state["ac_results"] == ctx.ac_results
        assert state["phase2_completed"] is True

    def test_full_resume_skips_children_phase_entirely(self, tmp_path):
        """phase2 completed in a prior run → _phase_children restores and
        returns without any orchestration or deep-analysis work."""
        store = _open_store(tmp_path)
        saved = {
            "ac_results": [{"text": "AC1: x", "verdict": "met", "evidence": "deep"}],
            "child_results": [{"id": "CHILD-1", "child_audit_ready": True}],
            "child_persist_results": [{"id": "CHILD-1", "success": True}],
            "phase2_completed": True,
            "phase2_skip_note": None,
        }
        store.mark_completed(PHASE_PARENT, {"ac_results": saved["ac_results"]})
        store.mark_completed(PHASE_CHILDREN, {k: v for k, v in saved.items()
                                              if k != "phase2_completed"})
        store.mark_completed(PHASE_PHASE2, saved)
        reopened = _open_store(tmp_path)
        ctx = _make_ctx(checkpoint=reopened)
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=self._deep_side_effect(),
        ) as deep, mock.patch.object(
            audit_runner, "_phase1_review_child_acs", return_value=(0, [])
        ) as review:
            rc = audit_runner._phase_children(ctx)
        assert rc is None
        deep.assert_not_called()
        review.assert_not_called()
        assert ctx.ac_results == saved["ac_results"]
        assert ctx.child_results == saved["child_results"]
        assert ctx.phase2_completed is True
        assert ctx.child_persist_results == saved["child_persist_results"]

    def test_children_phase_resume_runs_deep_for_pending_only(self, tmp_path):
        """phase1_children completed → orchestration skipped, but children
        that still need Phase 2 deep analysis get it exactly once."""
        store = _open_store(tmp_path)
        child_results = [
            {"id": "CHILD-1", "child_audit_ready": False,
             "ac_results": [{"text": "AC1", "verdict": "met", "evidence": "s1"}]},
            {"id": "CHILD-2", "child_audit_ready": True,
             "ac_results": [{"text": "AC1", "verdict": "met",
                             "evidence": "inherited"}]},
        ]
        saved = {
            "ac_results": [{"text": "AC1: x", "verdict": "met", "evidence": "deep"}],
            "child_results": child_results,
            "phase2_completed": True,   # parent deep done in the prior run
            "phase2_skip_note": None,
        }
        store.mark_completed(PHASE_PARENT, {"ac_results": saved["ac_results"]})
        store.mark_completed(PHASE_CHILDREN, saved)
        reopened = _open_store(tmp_path)
        ctx = _make_ctx(checkpoint=reopened)
        calls = []
        def _deep(issue, ac_results, child_results, **kw):
            calls.append(kw.get("skip_parent_deep"))
            return ac_results, child_results, True
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis", side_effect=_deep
        ) as deep, mock.patch.object(
            audit_runner, "_phase1_review_child_acs", return_value=(0, [])
        ) as review:
            rc = audit_runner._phase_children(ctx)
        assert rc is None
        review.assert_not_called()          # orchestration skipped
        deep.assert_called_once()
        assert calls == [True]              # parent deep never duplicated
        # The final phase2 checkpoint records the completed segment.
        # Reopen the store from disk (the in-hand object predates the run).
        final = _open_store(tmp_path)
        assert final.phase_status(PHASE_PHASE2) == STATUS_COMPLETED
        assert final.accumulated_state()["child_results"] == child_results

    def test_children_phase_resume_with_no_pending_keeps_phase2_state(
        self, tmp_path
    ):
        """When no child needs deep analysis, the resumed run does not call
        deep analysis and preserves the prior phase2 completion state."""
        store = _open_store(tmp_path)
        saved = {
            "ac_results": [{"text": "AC1: x", "verdict": "met", "evidence": "deep"}],
            "child_results": [],
            "phase2_completed": True,
            "phase2_skip_note": None,
        }
        store.mark_completed(PHASE_PARENT, {"ac_results": saved["ac_results"]})
        store.mark_completed(PHASE_CHILDREN, saved)
        reopened = _open_store(tmp_path)
        ctx = _make_ctx(checkpoint=reopened)
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=self._deep_side_effect(),
        ) as deep:
            rc = audit_runner._phase_children(ctx)
        assert rc is None
        deep.assert_not_called()
        assert ctx.phase2_completed is True

    def test_parent_only_checkpoint_restarts_children(self, tmp_path):
        """Only phase1_parent completed → the children phase reruns in full
        (deep analysis with the parent today, skip_parent_deep=False)."""
        store = _open_store(tmp_path)
        saved_acs = [{"text": "AC1: x", "verdict": "met", "evidence": "saved"}]
        store.mark_completed(PHASE_PARENT, {"ac_results": saved_acs})
        reopened = _open_store(tmp_path)
        ctx = _make_ctx(checkpoint=reopened)
        calls = []
        def _deep(issue, ac_results, child_results, **kw):
            calls.append(kw.get("skip_parent_deep"))
            return ac_results, child_results, True
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis", side_effect=_deep
        ):
            rc = audit_runner._phase_children(ctx)
        assert rc is None
        deep_calls = calls
        # Parent-first parent deep is genuinely re-run: the checkpoint only
        # saved Phase 1 screening, so the Phase 2 parent analysis must happen
        # with the parent today (kwarg unset — full parent deep, never skipped).
        assert deep_calls == [None]
        final = _open_store(tmp_path)
        assert final.phase_status(PHASE_CHILDREN) == STATUS_COMPLETED
        assert final.phase_status(PHASE_PHASE2) == STATUS_COMPLETED

    def test_resume_reports_interrupted_phase(self, tmp_path, capsys):
        """The resume banner names the phase the previous run died in."""
        store = _open_store(tmp_path)
        store.mark_completed(PHASE_PARENT, {"ac_results": [{"v": "met"}]})
        store.mark_started(PHASE_CHILDREN)  # interrupted here
        reopened = _open_store(tmp_path)
        ctx = _make_ctx(checkpoint=reopened)
        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=self._deep_side_effect(),
        ):
            audit_runner._phase_children(ctx)
        # The interrupted phase is not treated as completed → children rerun.
        final = _open_store(tmp_path)
        assert final.phase_status(PHASE_CHILDREN) == STATUS_COMPLETED
        assert final.phase_status(PHASE_PHASE2) == STATUS_COMPLETED


# ---------------------------------------------------------------------------
# cmd_issue integration
# ---------------------------------------------------------------------------


def _make_full_runner(head=HEAD_BASE):
    """Mock runner for a full cmd_issue("TEST-1", force=True, persist=False)
    with a single AC (parent screening hits the mocked Pi call)."""
    mock_runner = mock.MagicMock()

    def _side_effect(cmd):
        cmd_str = " ".join(cmd)
        if "rev-parse" in cmd_str:
            return SimpleNamespace(returncode=0, stdout=head, stderr="")
        if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "TEST-1", "status": "open",
                        "description": "## Acceptance Criteria\n- AC1: x",
                    },
                }),
                stderr="",
            )
        if "update" in cmd_str:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )
        if "--children" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "TEST-1", "status": "in_progress",
                        "description": "## Acceptance Criteria\n- AC1: x",
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


def _phase1_screen_side_effect(issue_id, context, prompt, model, pi_bin,
                               debug_log, timeout, ac_fallback_used,
                               on_runtime_error, failure_label,
                               child_screen=False, enable_tools=True,
                               priority=None):
    return (
        {"verdict": "met", "evidence": "x"},
        [{"index": 0, "verdict": "met", "evidence": "x"}],
        "raw",
    )


class TestCmdIssueCheckpoint:
    def _patch_pipeline(self, deep_result=True):
        """Mock the Pi-bound parts of the pipeline so cmd_issue is fast and
        deterministic; everything else (fetch, gates, report) stays real."""
        patchers = [
            mock.patch.object(
                audit_runner, "_call_phase1_screen",
                side_effect=_phase1_screen_side_effect,
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis",
                side_effect=lambda issue, ac_results, child_results, **kw: (
                    ac_results, child_results, deep_result,
                ),
            ),
            mock.patch.object(
                audit_runner, "_validate_file_scope_manifest", return_value=None
            ),
            mock.patch("code_review.scripts.code_quality.run_code_quality",
                       return_value={"success": True, "findings": [],
                                     "fixes_applied": 0}),
        ]
        return contextlib.ExitStack(), patchers

    def test_full_run_writes_checkpoint_then_clears_it(self, tmp_path):
        """A successful full audit writes phase checkpoints during the run
        and removes the checkpoint file once the report completes."""
        store = _open_store(tmp_path, force=True)
        runner = _make_full_runner()
        stack, patchers = self._patch_pipeline()
        with stack:
            for p in patchers:
                stack.enter_context(p)  # ExitStack owns them -> stopped on exit
            observed = {}
            real_report = audit_runner._phase_report

            def _wrapped_report(ctx):
                rc = real_report(ctx)
                if ctx.checkpoint is not None:
                    observed["exists_during_report"] = ctx.checkpoint.path().exists()
                return rc

            with mock.patch.object(
                audit_runner, "_phase_report", side_effect=_wrapped_report
            ):
                rc = audit_runner.cmd_issue(
                    "TEST-1", persist=False, force=True, runner=runner,
                    checkpoint_dir=str(tmp_path),
                )
        assert rc == 0
        assert observed["exists_during_report"] is True
        assert not store.path().exists()  # cleared after success

    def test_resume_skips_parent_pi_call_and_clears(self, tmp_path, capsys):
        """A resume run from a phase1_parent checkpoint skips the Phase 1 Pi
        call, reports the resume, and clears the checkpoint on completion."""
        seed = _open_store(tmp_path)
        seed.mark_completed(
            PHASE_PARENT,
            {"ac_results": [{"text": "AC1: x", "verdict": "met",
                             "evidence": "saved"}]},
        )
        runner = _make_full_runner()
        stack, patchers = self._patch_pipeline()
        with stack:
            for p in patchers:
                stack.enter_context(p)  # ExitStack owns them -> stopped on exit
            with mock.patch.object(
                audit_runner, "_call_phase1_screen",
                side_effect=_phase1_screen_side_effect,
            ) as screen:
                rc = audit_runner.cmd_issue(
                    "TEST-1", persist=False, force=False, runner=runner,
                    checkpoint_dir=str(tmp_path),
                )
        assert rc == 0
        # phase1_parent completed in the seeded store -> the resumed run skips
        # the Phase 1 Pi call entirely (the most expensive segment) and reports
        # the resume + skip on stderr (AC2/AC4).
        screen.assert_not_called()
        err = capsys.readouterr().err
        assert "[checkpoint] Resuming audit" in err
        assert "Skipping completed phase" in err
        # The successful run clears its checkpoint: no stale partial state left.
        assert not _open_store(tmp_path).path().exists()

    def test_stale_head_checkpoint_not_resumed(self, tmp_path, capsys):
        """A checkpoint written under a DIFFERENT git HEAD is ignored — the
        parent screening runs again (never reuse results from another HEAD)."""
        store = _open_store(tmp_path, head=HEAD_OTHER)
        store.mark_completed(
            PHASE_PARENT,
            {"ac_results": [{"text": "AC1: x", "verdict": "met",
                             "evidence": "stale"}]},
        )
        runner = _make_full_runner(head=HEAD_BASE)
        stack, patchers = self._patch_pipeline()
        with stack:
            for p in patchers:
                stack.enter_context(p)  # ExitStack owns them -> stopped on exit
            with mock.patch.object(
                audit_runner, "_call_phase1_screen",
                side_effect=_phase1_screen_side_effect,
            ) as screen:
                rc = audit_runner.cmd_issue(
                    "TEST-1", persist=False, force=False, runner=runner,
                    checkpoint_dir=str(tmp_path),
                )
        assert rc == 0
        # HEAD mismatch -> the stale file is ignored (fresh start at HEAD_BASE):
        # the parent screening runs again, and no resume banner appears —
        # results from another git HEAD are never reused (AC2/AC5 safety).
        screen.assert_called_once()
        err = capsys.readouterr().err
        assert "[checkpoint] Resuming audit" not in err
        assert "Phase checkpointing enabled" in err
        # The new-head run wrote (and cleared) its own checkpoint file; a
        # reopened store at HEAD_BASE is fresh, and no partial file remains.
        fresh = _open_store(tmp_path, head=HEAD_BASE)
        assert fresh.is_resuming is False
        assert not fresh.path().exists()

# ---------------------------------------------------------------------------
# Budget-exceeded resume completion (SA-0MU32T6O0001UALR / SA-0MU33XG8P004GX8K)
# ---------------------------------------------------------------------------


class TestBudgetExceededReaudit:
    """A resumed run re-audits exactly the children a prior run skipped for
    budget reasons, while completed phases/children are never re-run."""

    def _seed_resume_store(self, tmp_path, child_results):
        """Seed a checkpoint whose phase1_parent + phase1_children completed,
        then reopen it as a resuming store."""
        store = _open_store(tmp_path)
        store.mark_completed(
            PHASE_PARENT,
            {"ac_results": [{"text": "AC1: x", "verdict": "met",
                             "evidence": "parent"}]},
        )
        store.mark_completed(PHASE_CHILDREN, {
            "ac_results": [{"text": "AC1: x", "verdict": "met",
                            "evidence": "parent"}],
            "child_results": child_results,
            "child_persist_results": [],
            "phase2_completed": False,
            "phase2_skip_note": None,
        })
        return _open_store(tmp_path)

    def test_helper_reaudits_only_budget_exceeded_child(self, tmp_path, capsys):
        """AC1: the helper screens exactly the budget-exceeded child using the
        full child dict (description), replaces the placeholder verdicts and
        clears the checkpoint marker — leaving completed children untouched."""
        store = _open_store(tmp_path)
        store.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        child_results = [
            {"id": "CHILD-1", "title": "C1", "status": "open",
             "child_audit_ready": False, "budget_exceeded": True,
             "ac_results": [{"text": "partial (budget exceeded)",
                             "verdict": "partial", "evidence": "budget"}]},
            {"id": "CHILD-2", "title": "C2", "status": "completed",
             "child_audit_ready": True,
             "ac_results": [{"text": "AC2", "verdict": "met",
                             "evidence": "done"}]},
        ]
        ctx = _make_ctx(checkpoint=store)
        ctx.children = [
            {"id": "CHILD-1", "title": "C1",
             "description": "## Acceptance Criteria\n- AC1: c1"},
            {"id": "CHILD-2", "title": "C2",
             "description": "## Acceptance Criteria\n- AC2: c2"},
        ]
        persisted: list[dict] = []
        with mock.patch.object(
            audit_runner, "_phase1_review_child_acs",
            return_value=(0, [{"text": "AC1: c1", "verdict": "met",
                               "evidence": "reaudited"}]),
        ) as review:
            result, rc = audit_runner._reaudit_budget_exceeded_children(
                ctx, child_results, {"CHILD-1"}, persisted,
            )

        assert rc is None
        review.assert_called_once()
        # The full child dict (with description) was passed, not the
        # description-less restored result.
        assert "AC1: c1" in review.call_args.args[1]["description"]
        # Placeholder replaced and marker removed.
        assert result[0]["ac_results"][0]["evidence"] == "reaudited"
        assert result[0]["reaudited_after_budget_exceeded"] is True
        assert "budget_exceeded" not in result[0]
        assert store.budget_exceeded_children() == {}
        # Completed child untouched (never re-screened).
        assert result[1]["ac_results"][0]["evidence"] == "done"
        assert "reaudited_after_budget_exceeded" not in result[1]
        assert "[checkpoint] Re-auditing 1 budget-exceeded child(ren)" in \
            capsys.readouterr().err

    def test_helper_persists_reaudited_child_on_success(self, tmp_path):
        """AC1: with persist set, a re-audited child's report is persisted and
        recorded in child_persist_results (no orphaned in-memory verdict)."""
        store = _open_store(tmp_path)
        store.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        child_results = [{
            "id": "CHILD-1", "title": "C1", "status": "open", "stage": "",
            "child_audit_ready": False, "budget_exceeded": True,
            "ac_results": [{"text": "partial (budget exceeded)",
                            "verdict": "partial", "evidence": "budget"}],
        }]
        ctx = _make_ctx(checkpoint=store)
        ctx.persist = True
        ctx.children = [{"id": "CHILD-1", "title": "C1",
                         "description": "## Acceptance Criteria\n- AC1: c1"}]
        persisted: list[dict] = []
        with mock.patch.object(
            audit_runner, "_phase1_review_child_acs",
            return_value=(0, [{"text": "AC1: c1", "verdict": "met",
                               "evidence": "reaudited"}]),
        ), mock.patch.object(
            audit_runner, "_persist_child_audit",
            return_value=(0, "report"),
        ) as persist_mock:
            _result, rc = audit_runner._reaudit_budget_exceeded_children(
                ctx, child_results, {"CHILD-1"}, persisted,
            )
        assert rc is None
        persist_mock.assert_called_once()
        assert persisted == [{"id": "CHILD-1", "title": "C1",
                              "success": True}]

    def test_helper_persist_failure_aborts(self, tmp_path):
        """A re-audited child whose audit fails to persist aborts the run
        (mirrors the normal path's fatal child-persist rule)."""
        store = _open_store(tmp_path)
        store.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        child_results = [{
            "id": "CHILD-1", "title": "C1", "status": "open", "stage": "",
            "child_audit_ready": False, "budget_exceeded": True,
            "ac_results": [{"text": "partial (budget exceeded)",
                            "verdict": "partial", "evidence": "budget"}],
        }]
        ctx = _make_ctx(checkpoint=store)
        ctx.persist = True
        ctx.children = [{"id": "CHILD-1", "title": "C1",
                         "description": "## Acceptance Criteria\n- AC1: c1"}]
        with mock.patch.object(
            audit_runner, "_phase1_review_child_acs",
            return_value=(0, [{"text": "AC1: c1", "verdict": "met",
                               "evidence": "reaudited"}]),
        ), mock.patch.object(
            audit_runner, "_persist_child_audit", return_value=(1, "report"),
        ):
            _result, rc = audit_runner._reaudit_budget_exceeded_children(
                ctx, child_results, {"CHILD-1"}, [],
            )
        assert rc == 1

    def test_resume_reaudits_without_rerunning_completed_phase(
        self, tmp_path, capsys
    ):
        """AC1-AC3: _phase_children on a resumed run re-audits the
        budget-exceeded child, skips the completed child, clears the
        checkpoint marker and proceeds to Phase 2 — the completed parent
        phase is never re-run."""
        child_results = [
            {"id": "CHILD-1", "title": "C1", "status": "completed",
             "stage": "in_review",
             "child_audit_ready": False, "budget_exceeded": True,
             "ac_results": [{"text": "partial (budget exceeded)",
                             "verdict": "partial", "evidence": "budget"}]},
            {"id": "CHILD-2", "title": "C2", "status": "completed",
             "stage": "done", "child_audit_ready": True,
             "ac_results": [{"text": "AC2", "verdict": "met",
                             "evidence": "done"}]},
        ]
        reopened = self._seed_resume_store(tmp_path, child_results)
        reopened.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        reopened = _open_store(tmp_path)
        assert reopened.is_resuming is True
        ctx = _make_ctx(checkpoint=reopened)
        ctx.audit_children = True
        ctx.children = [
            {"id": "CHILD-1", "title": "C1",
             "description": "## Acceptance Criteria\n- AC1: c1"},
            {"id": "CHILD-2", "title": "C2",
             "description": "## Acceptance Criteria\n- AC2: c2"},
        ]
        with mock.patch.object(
            audit_runner, "_phase1_review_child_acs",
            return_value=(0, [{"text": "AC1: c1", "verdict": "met",
                               "evidence": "reaudited"}]),
        ) as review, mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=lambda issue, ac, cr, **kw: (ac, cr, True),
        ) as deep:
            rc = audit_runner._phase_children(ctx)

        assert rc is None
        # Only the budget-exceeded child was re-screened.
        review.assert_called_once()
        assert review.call_args.args[0] == 0
        assert ctx.child_results[0]["reaudited_after_budget_exceeded"] is True
        assert ctx.child_results[0]["ac_results"][0]["evidence"] == "reaudited"
        assert "budget_exceeded" not in ctx.child_results[0]
        assert ctx.child_results[1]["ac_results"][0]["evidence"] == "done"
        # Marker cleared → the checkpoint can be cleared on success.
        assert reopened.budget_exceeded_children() == {}
        assert deep.called
        err = capsys.readouterr().err
        assert "Re-auditing 1 budget-exceeded child(ren)" in err
        assert "Re-audited budget-exceeded child CHILD-1" in err

    def test_resume_without_markers_skips_reaudit(self, tmp_path):
        """A resume with no budget-exceeded markers is unchanged: no Phase 1
        child screening is re-run."""
        child_results = [
            {"id": "CHILD-2", "title": "C2", "status": "open",
             "child_audit_ready": False,
             "ac_results": [{"text": "AC2", "verdict": "met",
                             "evidence": "screened"}]},
        ]
        reopened = self._seed_resume_store(tmp_path, child_results)
        ctx = _make_ctx(checkpoint=reopened)
        ctx.audit_children = True
        ctx.children = [{"id": "CHILD-2", "title": "C2",
                         "description": "## Acceptance Criteria\n- AC2: c2"}]
        with mock.patch.object(
            audit_runner, "_phase1_review_child_acs", return_value=(0, [])
        ) as review, mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=lambda issue, ac, cr, **kw: (ac, cr, True),
        ):
            rc = audit_runner._phase_children(ctx)
        assert rc is None
        review.assert_not_called()
        assert ctx.child_results[0]["ac_results"][0]["evidence"] == "screened"

    def test_completed_budget_marker_from_other_head_not_reused(self, tmp_path):
        """Safety: a budget-exceeded marker written under a different git HEAD
        is never loaded, so the resumed run does not act on a stale child."""
        seed = _open_store(tmp_path, head=HEAD_OTHER)
        seed.mark_child_budget_exceeded("CHILD-1", 800.0, 710)
        current = _open_store(tmp_path, head=HEAD_BASE)
        assert current.is_resuming is False
        assert current.budget_exceeded_children() == {}
