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
from audit.tests.wl_helpers import stateful_wl_side_effect


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests.

    ``_call_pi`` acquires the real cross-process audit semaphore before
    launching the (mocked) subprocess. Under concurrent audit load the
    semaphore can saturate, making these timing-path unit tests flaky (see
    SA-0MSCDC4750019G9Y, SA-0MSCDC76A007JCJK). Replace it with a
    null-context so the mocked return paths are exercised directly.

    The real semaphore behavior is covered separately by
    ``test_audit_runner_concurrency.py``.
    """
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield

class TestVerdictDrivenStatusLifecycle:
    """Tests for the verdict-driven status transition in cmd_issue's finally.

    The audit runner must leave the work item in a state consistent with its
    audit verdict (SA-0MSAWFTZX003T042):

      - Ready to close: Yes → status=completed, stage=in_review (stage kept
        as 'done' when the item is already in a terminal done stage)
      - Ready to close: No → status=open, stage=plan_complete
      - Failure / unparseable verdict (infrastructure failure) → restore the
        captured pre-audit status/stage + cleared assignee; the item is
        never demoted to open unless the verdict was an explicit No
      - Freshness-gate skip → no lifecycle transitions
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_runner(self, updates, status="open", stage="plan_complete",
                     description="", children=None, fail_children_show=False,
                     parent_id=None):
        """Build a mock runner that records every ``wl update`` command.

        Handles the exact ``wl`` command sequence issued by ``cmd_issue``:
        status capture, in_progress claim, children fetch, and the
        verdict-driven terminal transition in the ``finally`` block.
        """
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)

            if "update" in cmd_str:
                updates.append(list(cmd))
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )

            # Original status/stage capture → wl show <id> --json.
            # The captured work item carries an optional parentId so tests can
            # exercise the top-level vs child producer-review gating.
            if "show" in cmd_str and "--children" not in cmd_str:
                wi = {"id": "TEST-1", "status": status, "stage": stage}
                if parent_id is not None:
                    wi["parentId"] = parent_id
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True, "workItem": wi}),
                    stderr="",
                )

            # wl show <id> --children --json (optionally failing)
            if "--children" in cmd_str:
                if fail_children_show:
                    return SimpleNamespace(returncode=1, stdout="", stderr="boom")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": status,
                            "stage": stage,
                        },
                        "children": children or [],
                    }),
                    stderr="",
                )

            # Fallback for any unexpected command
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
        return mock_runner

    def _run_issue(self, updates, verdict_report, runner=None, **runner_kwargs):
        """Run cmd_issue with a controlled report verdict and no real subprocesses."""
        mock_runner = runner or self._make_runner(updates, **runner_kwargs)
        with (
            mock.patch.object(
                audit_runner, "_assemble_issue_report",
                return_value=verdict_report,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

    def _last_update(self, updates):
        """Return the last wl update command recorded (the terminal transition).

        Strips a leading ``--worklog-dir <path>`` pair that StatusLifecycle
        injects when the audit runner is invoked from inside a git worktree,
        so the assertions below are cwd-independent.
        """
        assert updates, "expected at least one wl update command"
        cmd = updates[-1]
        if cmd[:1] == ["wl"] and len(cmd) >= 3 and cmd[1] == "--worklog-dir":
            cmd = ["wl"] + cmd[3:]
        return cmd

    # ------------------------------------------------------------------
    # Ready to close: Yes
    # ------------------------------------------------------------------

    def test_ready_yes_sets_completed_in_review(self):
        """AC1: Ready to close: Yes → status=completed, stage=in_review.

        Applies regardless of the pre-audit status (here: in_progress). The
        item has no parent, so the terminal update also sets
        --needs-producer-review yes (SA-0MSSVKYEW008PJ9H).
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="in_progress", stage="in_progress",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--needs-producer-review", "yes", "--json",
        ]

    def test_ready_yes_child_does_not_set_producer_review(self):
        """AC2: Ready to close: Yes on a child item (has a parent) does not
        set --needs-producer-review — the parent's review covers the subtree.
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="in_progress", stage="in_progress",
            parent_id="PARENT-1",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review", "--json",
        ]

    def test_ready_yes_keeps_terminal_done_stage(self):
        """AC1: Ready to close: Yes keeps a pre-existing 'done' stage, and a
        top-level item still gets --needs-producer-review yes."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="completed", stage="done",
        )
        last = self._last_update(updates)
        assert "--status" in last and "completed" in last
        assert "--stage" not in last  # stage stays 'done'
        assert "--needs-producer-review" in last and "yes" in last

    def test_ready_yes_child_keeps_terminal_done_stage(self):
        """AC2: A child keeps a pre-existing 'done' stage and does NOT get the
        producer-review flag on the terminal update."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="completed", stage="done",
            parent_id="PARENT-1",
        )
        last = self._last_update(updates)
        assert "--status" in last and "completed" in last
        assert "--stage" not in last  # stage stays 'done'
        assert "--needs-producer-review" not in last

    def test_ready_yes_idempotent_on_completed_in_review(self):
        """AC6: Re-auditing a completed/in_review item with Yes stays completed/in_review."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="completed", stage="in_review",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--needs-producer-review", "yes", "--json",
        ]

    # ------------------------------------------------------------------
    # Ready to close: No
    # ------------------------------------------------------------------

    def test_ready_no_sets_open_plan_complete(self):
        """AC2: Ready to close: No → status=open, stage=plan_complete.

        Applies regardless of the pre-audit status (here: completed/in_review,
        i.e. a failing re-audit demotes the item).
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: No\n\n## Summary\n2 unmet.",
            status="completed", stage="in_review",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "open", "--stage", "plan_complete", "--json",
        ]

    def test_ready_no_moves_open_item_to_plan_complete(self):
        """AC2: No on an already-open item still lands at open/plan_complete."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: No\n\n## Summary\nunmet.",
            status="open", stage="in_progress",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "open", "--stage", "plan_complete", "--json",
        ]

    # ------------------------------------------------------------------
    # Failure / unparseable verdict
    # ------------------------------------------------------------------

    def test_failure_restores_safe_state_and_clears_assignee(self):
        """AC4: On failure the item is never left in_progress; assignee cleared."""
        updates = []
        # wl show --children fails → early exit with script_failure recorded
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes",
            status="open", stage="plan_complete",
            fail_children_show=True,
        )
        last = self._last_update(updates)
        assert "--status" in last and "open" in last
        assert "--stage" in last and "plan_complete" in last
        assert "--assignee" in last and "" in last

    def test_failure_on_in_progress_item_restores_pre_audit_state(self):
        """AC2: An infra failure while the pre-audit status was in_progress
        restores in_progress (assignee cleared) — never demotes to open.
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes",
            status="in_progress", stage="in_progress",
            fail_children_show=True,
        )
        last = self._last_update(updates)
        assert "--status" in last and "in_progress" in last
        assert "--stage" in last and "in_progress" in last
        assert "--assignee" in last and "" in last

    def test_failure_on_in_review_item_keeps_in_review(self):
        """AC2: An infra failure on a completed/in_review item (e.g. a re-audit
        hitting a model timeout) keeps it at completed/in_review so the item is
        not kicked back to the actionable queue.
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes",
            status="completed", stage="in_review",
            fail_children_show=True,
        )
        last = self._last_update(updates)
        assert "--status" in last and "completed" in last
        assert "--stage" in last and "in_review" in last
        assert "--assignee" in last and "" in last

    def test_failure_takes_precedence_over_parseable_yes_report(self):
        """AC7: An infra failure combined with an otherwise-parseable Yes report
        must NOT advance the item — the failure means the audit did not complete
        cleanly, so the verdict cannot be trusted. The item stays at its
        pre-audit state (here in_progress), never completed/in_review.
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="in_progress", stage="in_progress",
            fail_children_show=True,
        )
        last = self._last_update(updates)
        assert "--status" in last and "in_progress" in last
        assert "--stage" in last and "in_progress" in last
        assert "--assignee" in last and "" in last
        # The item must NOT advance to completed — failure takes precedence.
        assert "completed" not in last

    def test_unparseable_verdict_falls_back_to_safe_state(self):
        """AC4: An unparseable verdict must not blindly set completed/open."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="## Summary\nNo verdict line present",
            status="completed", stage="in_review",
        )
        last = self._last_update(updates)
        # Restored to the captured pre-audit state, assignee cleared
        assert "--status" in last and "completed" in last
        assert "--stage" in last and "in_review" in last
        assert "--assignee" in last and "" in last

    # ------------------------------------------------------------------
    # Freshness gate skip
    # ------------------------------------------------------------------

    def test_freshness_skip_performs_no_transitions(self):
        """AC5: A fresh audit skips with zero status/stage transitions."""
        updates = []
        mock_runner = self._make_runner(updates)
        with mock.patch.object(
            audit_runner, "_check_audit_freshness",
            return_value="Skipping: audit still fresh\n<existing report>",
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=mock_runner,
            )
        assert rc == 0
        assert updates == []

    # ------------------------------------------------------------------
    # Pre-flight affirmation guard (SA-0MSL1Z1WU005O5IY)
    # ------------------------------------------------------------------

    def test_preflight_guard_aborts_on_in_progress_item(self):
        """AC2: an in_progress item without --force does not start an audit.

        The guard aborts BEFORE the status lifecycle: no ``wl update`` is
        issued, no report is produced, and the pre-audit state is preserved.
        """
        updates = []
        mock_runner = self._make_runner(
            updates, status="in_progress", stage="in_progress",
        )
        rc = audit_runner.cmd_issue(
            "TEST-1", persist=False, force=False, runner=mock_runner,
        )
        assert rc == 1
        assert updates == [], "guard must abort before any wl update"

    def test_preflight_guard_bypassed_with_force(self):
        """AC2: --force bypasses the guard and the audit proceeds normally."""
        updates = []
        rc = self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="in_progress", stage="in_progress",
        )
        assert rc == 0
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--needs-producer-review", "yes", "--json",
        ]

    def test_preflight_guard_allows_open_items(self):
        """AC2: an open item audits normally without --force."""
        updates = []
        mock_runner = self._make_runner(
            updates, status="open", stage="plan_complete",
        )
        with mock.patch.object(
            audit_runner, "_assemble_issue_report",
            return_value="Ready to close: Yes\n\n## Summary\nAll met.",
        ), mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [], "fixes_applied": 0},
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=mock_runner,
            )
        assert rc == 0
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--needs-producer-review", "yes", "--json",
        ]

    # ------------------------------------------------------------------
    # Infra-failure fallback verdicts (SA-0MSG9SLGI002OF7V)
    #
    # A "Ready to close: No" verdict produced solely from infrastructure-
    # failure fallbacks (concurrency-limit timeout, provider error,
    # unparseable Pi output, Phase-2 deep-analysis timeout) must restore the
    # captured pre-audit status/stage (assignee cleared) — it must NEVER
    # demote a completed/in_review item to open/plan_complete. Only an
    # explicit model "No" with genuine parseable verdicts may demote.
    # ------------------------------------------------------------------

    def _run_issue_fallback(self, updates, pi_side_effect, *,
                            description="", children=None,
                            status="completed", stage="in_review",
                            audit_children=False):
        """Run cmd_issue through the REAL AC-screening fallback blocks.

        *pi_side_effect* is a callable(issue_id, context, prompt, **kwargs)
        returning the Pi result dict for each call. *description* must
        contain acceptance criteria so the parent AC screening executes and
        its fallback block is reachable. The terminal ``wl update`` is
        recorded in *updates* for assertion.

        *audit_children* opts into the full per-child flow (child Phase 2
        deep analysis); the parent-first default inherits passed children
        instead (SA-0MSKB6VJA005N43F).
        """
        mock_runner = self._make_runner(
            updates, status=status, stage=stage,
            description=description, children=children,
        )
        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                side_effect=pi_side_effect,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=audit_children,
            )

    def _assert_restored_completed_in_review(self, updates):
        """Assert the terminal wl update restores completed/in_review with a
        cleared assignee and does NOT demote to open/plan_complete."""
        last = self._last_update(updates)
        assert "--status" in last and "completed" in last
        assert "--stage" in last and "in_review" in last
        assert "--assignee" in last and "" in last
        assert "open" not in last  # never demoted to the actionable queue

    @staticmethod
    def _met_array(num_acs):
        """A parseable all-met verdict array covering *num_acs* criteria."""
        return {
            "verdict": "met",
            "evidence": "file.py:1",
            "extracted_text": json.dumps([
                {"index": i, "verdict": "met", "evidence": "file.py:1"}
                for i in range(num_acs)
            ]),
            "elapsed_seconds": 0.1,
        }

    def test_concurrency_fallback_restores_completed_in_review(self):
        """AC1: concurrency-limit fallback restores, never demotes.

        A parent AC-screening Pi result carrying ``_concurrency_timeout``
        falls back to diagnostic ``partial`` verdicts. The assembled report
        ends "Ready to close: No" but the item must be restored to its
        pre-audit completed/in_review state (assignee cleared), not demoted.
        """
        updates = []

        def _pi(issue_id, context, prompt, **kwargs):
            return {
                "verdict": "unmet",
                "evidence": (
                    "Audit concurrency limit reached: semaphore 'audit' busy: "
                    "no slot free within 300.0s (max_workers=5)"
                ),
                "raw_stdout": "", "raw_stderr": "", "extracted_text": "",
                "_concurrency_timeout": True,
                "elapsed_seconds": 0.1,
            }

        self._run_issue_fallback(
            updates, _pi,
            description="## Acceptance Criteria\n1. AC one\n2. AC two",
        )
        self._assert_restored_completed_in_review(updates)

    def test_provider_error_fallback_restores_completed_in_review(self):
        """AC2: provider-error fallback restores, never demotes.

        A parent AC-screening Pi result carrying ``_provider_error`` degrades
        the verdicts to ``partial`` with provider diagnostics; Phase 2
        output is unparseable (no error markers) so no script_failure is
        recorded — the fallback "No" must still restore, not demote.
        """
        updates = []

        def _pi(issue_id, context, prompt, **kwargs):
            if context == "parent":
                return {
                    "verdict": "unmet",
                    "evidence": "Pi provider error: finish_reason: error",
                    "raw_stdout": "", "raw_stderr": "",
                    "extracted_text": "",
                    "_provider_error": True,
                    "_provider_error_message": "finish_reason: error",
                    "elapsed_seconds": 0.1,
                }
            # Phase 2: unparseable output (no error markers) so no
            # script_failure is recorded — the fallback "No" is the only
            # signal driving the lifecycle decision.
            return {"verdict": "unmet", "evidence": "", "extracted_text": "not json"}

        self._run_issue_fallback(
            updates, _pi,
            description="## Acceptance Criteria\n1. AC one\n2. AC two",
        )
        self._assert_restored_completed_in_review(updates)

    def test_unparseable_output_fallback_restores_completed_in_review(self):
        """AC3: unparseable-output fallback restores, never demotes.

        A parent AC-screening Pi result with non-JSON text and no error
        markers falls back to ``partial`` verdicts; the fallback "No" must
        restore the pre-audit completed/in_review state.
        """
        updates = []

        def _pi(issue_id, context, prompt, **kwargs):
            return {
                "verdict": "unmet", "evidence": "",
                "extracted_text": "the model output is not json",
            }

        self._run_issue_fallback(
            updates, _pi,
            description="## Acceptance Criteria\n1. AC one\n2. AC two",
        )
        self._assert_restored_completed_in_review(updates)

    def test_phase2_child_timeout_restores_completed_in_review(self):
        """AC4: Phase-2 child deep-analysis timeout restores, never demotes.

        The child's Phase 2 deep-analysis call times out (``_timeout``
        marker): the child ACs degrade to ``partial`` WITHOUT recording a
        script_failure (the child timeout path has no failure callback), so
        the report ends "Ready to close: No" with script_failure=None — the
        item must be restored, not demoted.
        """
        updates = []
        child = {
            "id": "CHILD-1", "title": "Child", "status": "open",
            "stage": "in_review",
            "description": "## Acceptance Criteria\n1. Child AC one",
        }

        def _pi(issue_id, context, prompt, **kwargs):
            if context.startswith("phase2_child"):
                return {
                    "verdict": "unmet",
                    "evidence": (
                        "Pi model call timed out after 600s. Manual audit required."
                    ),
                    "raw_stdout": "", "raw_stderr": "", "extracted_text": "",
                    "_timeout": True,
                    "elapsed_seconds": 0.1,
                }
            return self._met_array(2)

        self._run_issue_fallback(
            updates, _pi,
            description="## Acceptance Criteria\n1. AC one\n2. AC two",
            children=[child],
            audit_children=True,  # child Phase 2 deep analysis is opt-in (SA-0MSKB6VJA005N43F)
        )
        self._assert_restored_completed_in_review(updates)



class TestLifecycleVerification:
    """Post-update lifecycle readback verification (WL-0MSVVFBJ2003RRYK).

    The terminal ``wl update`` may exit 0 while the item's status/stage
    silently stays unchanged (a swallowed update) — the audit runner must
    verify the transition through an independent ``wl show`` readback,
    retry transient failures AND verification mismatches, then fail loudly
    with a non-zero exit instead of reporting a silent success:

      - Ready to close: Yes → item MUST end verified completed/in_review
      - Ready to close: No  → item MUST end verified open/plan_complete
      - Infra failure       → item MUST end verified at pre-audit state
      - A swallowed update  → non-zero exit + clear diagnostic + best-effort
        restore of the pre-audit state (WL-0MSWFRM800073Y81)
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_swallow_runner(self, updates, status="open", stage="idea",
                             parent_id=None, description="",
                             fail_children_show=False):
        """Build a runner whose ``wl update`` ALWAYS reports success but never
        applies — the silently-swallowed update this work item targets.

        ``wl show`` keeps returning the fixed pre-audit state, so the
        post-update readback verification can never confirm the transition.
        """
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str:
                updates.append(list(cmd))
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                wi = {"id": "TEST-1", "status": status, "stage": stage}
                if parent_id is not None:
                    wi["parentId"] = parent_id
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True, "workItem": wi}),
                    stderr="",
                )
            if "--children" in cmd_str:
                if fail_children_show:
                    return SimpleNamespace(returncode=1, stdout="", stderr="boom")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1", "description": description,
                            "status": status, "stage": stage,
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _run_issue(self, updates, verdict_report, runner=None, **runner_kwargs):
        """Run cmd_issue with a controlled report verdict and a swallow runner."""
        mock_runner = runner or self._make_swallow_runner(updates, **runner_kwargs)
        with (
            mock.patch.object(
                audit_runner, "_assemble_issue_report",
                return_value=verdict_report,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

    @staticmethod
    def _update_statuses(updates):
        """Return the (status, stage) tuples of every recorded wl update."""
        out = []
        for cmd in updates:
            status = stage = None
            for i, tok in enumerate(cmd):
                if tok == "--status" and i + 1 < len(cmd):
                    status = cmd[i + 1]
                elif tok == "--stage" and i + 1 < len(cmd):
                    stage = cmd[i + 1]
            out.append((status, stage))
        return out

    # ------------------------------------------------------------------
    # Swallowed update detection (AC1/AC3: never a silent success)
    # ------------------------------------------------------------------

    def test_swallowed_yes_update_fails_nonzero_with_diagnostic(self, capsys):
        """AC1: a 'Yes' verdict whose wl update is swallowed (exit 0, state
        unchanged) exits non-zero with a clear diagnostic — never rc 0."""
        updates = []
        rc = self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="open", stage="idea",
        )
        assert rc != 0, "a swallowed lifecycle update must fail the run"
        err = capsys.readouterr().err
        assert "Failed to apply terminal status transition" in err
        assert "TEST-1" in err
        # The terminal update was retried up to _STATUS_RESTORE_MAX_ATTEMPTS
        # times (the restore to pre-audit open/idea is a separate update).
        terminal_updates = [
            (s, t) for s, t in self._update_statuses(updates)
            if s == "completed"
        ]
        assert len(terminal_updates) == 3, (
            "expected the terminal update to be retried 3 times, got "
            f"{len(terminal_updates)}"
        )
        assert all(s == "completed" and t == "in_review"
                   for s, t in terminal_updates)

    def test_swallowed_yes_update_restores_pre_audit_state(self, capsys):
        """AC2 (WL-0MSWFRM800073Y81): after retries are exhausted the runner
        restores the captured pre-audit state and logs the restore."""
        updates = []
        rc = self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="open", stage="idea",
        )
        assert rc != 0
        # The final wl update must restore open/idea and clear the assignee.
        last = updates[-1]
        assert "--status" in last and last[last.index("--status") + 1] == "open"
        assert "--stage" in last and last[last.index("--stage") + 1] == "idea"
        assert "--assignee" in last and last[last.index("--assignee") + 1] == ""
        err = capsys.readouterr().err
        assert "Restored TEST-1 to pre-audit state" in err

    def test_swallowed_no_update_fails_nonzero(self, capsys):
        """AC2: a 'No' verdict whose demote update is swallowed fails loudly."""
        updates = []
        rc = self._run_issue(
            updates,
            verdict_report="Ready to close: No\n\n## Summary\n2 unmet.",
            status="completed", stage="in_review",
        )
        assert rc != 0, "a swallowed demote must fail the run"
        err = capsys.readouterr().err
        assert "Failed to apply terminal status transition" in err
        terminal_updates = [
            (s, t) for s, t in self._update_statuses(updates)
            if s == "open"
        ]
        assert len(terminal_updates) == 3
        assert all(s == "open" and t == "plan_complete"
                   for s, t in terminal_updates)

    def test_swallowed_restore_update_fails_nonzero(self, capsys):
        """AC2: an infra-failure restore whose update is swallowed fails loudly.

        The claim (``--status in_progress``) applies but the terminal restore
        is swallowed, so the item is left in_progress — the readback cannot
        confirm the pre-audit state and the run must surface the failure.
        """
        updates = []
        state = {"status": "open", "stage": "plan_complete"}

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str and "--status" in cmd_str:
                updates.append(list(cmd))
                # The in_progress claim applies; every later update is
                # silently swallowed.
                if "in_progress" in cmd_str:
                    state["status"] = "in_progress"
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": state["status"],
                                     "stage": state["stage"]},
                    }),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner = mock.MagicMock()
        mock_runner.side_effect = _side_effect
        rc = self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            runner=mock_runner,
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "Failed to apply terminal status transition" in err

    # ------------------------------------------------------------------
    # Verification passes when the update applies (stateful wl)
    # ------------------------------------------------------------------

    def test_yes_verification_passes_when_update_applies(self):
        """AC1: a real (stateful) worklog reflects the terminal update, so the
        readback verification passes and the run exits 0."""
        updates = []
        mock_runner = mock.MagicMock()
        state = {"status": "open", "stage": "idea"}

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "update" in cmd_str:
                updates.append(list(cmd))
                for i, tok in enumerate(cmd):
                    if tok == "--status" and i + 1 < len(cmd):
                        state["status"] = cmd[i + 1]
                    elif tok == "--stage" and i + 1 < len(cmd):
                        state["stage"] = cmd[i + 1]
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": state["status"],
                                     "stage": state["stage"]},
                    }),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "description": "",
                                     "status": "open", "stage": "idea"},
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        rc = self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            runner=mock_runner,
        )
        assert rc == 0, "an applied+verified transition must exit 0"
        # Exactly one terminal update (no retries) after the claim.
        terminal_updates = [
            (s, t) for s, t in self._update_statuses(updates)
            if s == "completed"
        ]
        assert terminal_updates == [("completed", "in_review")]
