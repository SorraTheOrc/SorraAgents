"""Race-clobber regression tests for the audit runner lifecycle restore
(SA-0MW7Z77KI005MK0B, F1).

The audit runner transitions the target item out of ``completed``/``in_review``
(to ``in_progress``) and restores it on every exit path.  Under concurrency a
``wl sync`` running in another session (pull -> merge -> push, last-writer-wins
on a stale snapshot) races the restore, and the item is left stuck at a stale
``open``/``plan_complete`` — a worklog data-integrity regression.

These tests introduce a deterministic clobber injection: an injectable runner
that reverts the item to a stale snapshot at a scripted point, modelling the
concurrent ``wl sync`` (WL-0MUXA5RI2009EUTJ class).  They exercise the
production restore paths — no re-implementation of the lifecycle logic — and
were red before the fix in :func:`audit_runner._apply_terminal_lifecycle`,
:func:`audit_runner._restore_pre_audit_state_on_failure` and
:func:`audit_runner._claim_audit_lifecycle`.

Red run (pre-fix) recorded in the work-item comment: every clobber test below
left the item at ``open``/``plan_complete`` (or ``in_progress``) because the
restore was a single unverified write and the claim sat outside the guarded
region.  Green after the fixes.
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
    """Neutralize the host-wide audit semaphore for deterministic tests."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


@pytest.fixture(autouse=True)
def _no_settle_sleep(monkeypatch):
    """Disable the settle sleep so the race tests run instantly and deterministically.

    The bounded settle *interval* is replaced, not the re-verification: the
    settle re-read and one re-apply still execute exactly as in production.
    """
    monkeypatch.setenv(audit_runner.AUDIT_STATUS_SETTLE_DELAY_ENV, "0")


@pytest.fixture(autouse=True)
def _hermetic_cmd_issue(monkeypatch, tmp_path):
    """Keep the ``cmd_issue`` race tests network- and side-effect-free.

    The audit lease is a cross-host proxy call (enabled by default) and the
    checkpoint store may create files; neither is under test here. Tests that
    exercise those setup steps (the exception-after-claim cases) patch the
    specific helper themselves, which wins over this fixture. The abort state
    file is redirected into the per-test tmp dir so the ``cmd_issue`` flows
    never touch ``/tmp`` or a real worklog.
    """
    monkeypatch.setenv(audit_runner.AUDIT_LEASE_ENABLED_ENV, "0")
    monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
    with mock.patch.object(
        audit_runner, "_open_checkpoint_store", return_value=None,
    ):
        yield


class _RaceRunner:
    """Stateful ``wl`` runner that injects a concurrent ``wl sync`` clobber.

    Models a real worklog: ``wl update --status/--stage/--assignee`` mutates the
    in-memory item so the runner's readback reflects what was written; ``wl show
    <id> --json`` returns the persisted item.  A clobber reverts the item to a
    stale snapshot at a scripted point, modelling another session's ``wl sync``
    winning the last-writer race:

    * ``clobber_after_updates`` — ``{update_seq: (status, stage)}`` applied
      AFTER the Nth ``wl update`` returns (the write lands, then a sync reverts
      it before the readback).
    * ``clobber_after_shows`` — ``{item_show_seq: (status, stage)}`` applied
      AFTER the Nth item readback returns (the readback was verified, then a
      sync reverts it before the settle re-read).
    * ``clobber_first_completed_write`` — clobber once, immediately after the
      first update that sets ``--status completed`` (the restore write).
    """

    def __init__(self, *, status="completed", stage="in_review",
                 assignee="agent", needs_producer_review=False):
        self.status = status
        self.stage = stage
        self.assignee = assignee
        self.needs_producer_review = needs_producer_review
        self.commands: list[list[str]] = []
        self.updates: list[list[str]] = []
        self.item_shows = 0
        self.update_seq = 0
        self.clobber_after_updates: dict[int, tuple[str, str]] = {}
        self.clobber_after_shows: dict[int, tuple[str, str]] = {}
        self.clobber_first_completed_write = False
        self.children_show_fails = False
        # CAS simulation: reject the first conditional write and, when set,
        # move the item to *cas_reject_first_to* first — modelling another
        # session winning the race between our read and our write.
        self.cas_reject_first_to: tuple[str, str] | None = None
        self.cas_rejections = 0

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _flag(cmd, name):
        if name in cmd:
            idx = cmd.index(name)
            if idx + 1 < len(cmd):
                return cmd[idx + 1]
        return None

    def _clobber(self, status, stage):
        self.status = status
        self.stage = stage

    def restore_updates(self, status="completed"):
        return [
            u for u in self.updates
            if self._flag(u, "--status") == status
        ]

    # -- runner ----------------------------------------------------------

    def __call__(self, cmd):
        cmd = [str(c) for c in cmd]
        self.commands.append(cmd)

        if "update" in cmd:
            self.update_seq += 1
            self.updates.append(cmd)
            new_status = self._flag(cmd, "--status")
            new_stage = self._flag(cmd, "--stage")
            new_assignee = self._flag(cmd, "--assignee")
            # Enforce the CAS guard like wl itself: a conditional write whose
            # expectation no longer matches is rejected (rc=1, "stale") and
            # mutates nothing.
            if_status = self._flag(cmd, "--if-status")
            if_stage = self._flag(cmd, "--if-stage")
            if self.cas_reject_first_to is not None:
                self._clobber(*self.cas_reject_first_to)
                self.cas_reject_first_to = None
                self.cas_rejections += 1
                return SimpleNamespace(
                    returncode=1,
                    stdout=json.dumps({
                        "success": False,
                        "error": "stale",
                        "message": (
                            "Conditional update skipped: item no longer matches "
                            "--if-status/--if-stage"
                        ),
                    }),
                    stderr="",
                )
            if (if_status is not None or if_stage is not None) and (
                (if_status is not None and if_status != self.status)
                or (if_stage is not None and if_stage != self.stage)
            ):
                self.cas_rejections += 1
                return SimpleNamespace(
                    returncode=1,
                    stdout=json.dumps({
                        "success": False,
                        "error": "stale",
                        "message": (
                            "Conditional update skipped: item no longer matches "
                            "--if-status/--if-stage"
                        ),
                    }),
                    stderr="",
                )
            if new_status is not None:
                self.status = new_status
            if new_stage is not None:
                self.stage = new_stage
            if new_assignee is not None:
                self.assignee = new_assignee
            if "--needs-producer-review" in cmd:
                self.needs_producer_review = (
                    self._flag(cmd, "--needs-producer-review") == "yes"
                )
            result = SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )
            clobber = self.clobber_after_updates.get(self.update_seq)
            if clobber is not None:
                self._clobber(*clobber)
            if (
                self.clobber_first_completed_write
                and new_status == "completed"
            ):
                self._clobber("open", "plan_complete")
                self.clobber_first_completed_write = False
            return result

        if "audit-show" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "audit": None}),
                stderr="",
            )
        if "audit-set" in cmd:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        if "show" in cmd:
            idx = cmd.index("show")
            item_id = cmd[idx + 1] if idx + 1 < len(cmd) else ""
            if "--children" in cmd:
                if self.children_show_fails:
                    return SimpleNamespace(returncode=1, stdout="", stderr="boom")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": item_id, "description": "",
                            "status": self.status, "stage": self.stage,
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            self.item_shows += 1
            result = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": item_id,
                        "status": self.status,
                        "stage": self.stage,
                        "assignee": self.assignee,
                        "needsProducerReview": self.needs_producer_review,
                        "parentId": None,
                    },
                }),
                stderr="",
            )
            clobber = self.clobber_after_shows.get(self.item_shows)
            if clobber is not None:
                self._clobber(*clobber)
            return result

        # dep / list / create / comment: minimal well-formed payloads.
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"success": True, "workItems": []}),
            stderr="",
        )


def _lifecycle_ctx(runner, *, original_status="completed",
                   original_stage="in_review", verdict="yes",
                   completed=True, script_failure=None):
    """Build a minimal ``_AuditContext`` for direct lifecycle calls."""
    ctx = audit_runner._AuditContext(
        issue_id="TEST-1", persist=False, timeout=None, parent_timeout=None,
        pi_bin="pi", model=None, model_source="test", runner=runner,
        json_mode=False, debug_log=None, force=True, worklog_dir=None,
        batch_phase2=False, green_run=None, audit_children=False,
        max_child_audits=None, run_tests=False,
    )
    ctx.audit_verdict = verdict
    ctx.audit_completed = completed
    ctx.script_failure = script_failure
    ctx.original_status = original_status
    ctx.original_stage = original_stage
    ctx.wi = {"id": "TEST-1", "parentId": None}
    # Skip the producer-review resolution (which may read the audit record);
    # the restore branch never consults it.
    ctx.producer_review = {"action": audit_runner.PRODUCER_REVIEW_UNTOUCHED}
    return ctx


class TestRestoreRaceClobber:
    """The restore must survive a concurrent ``wl sync`` lost update."""

    def test_aborted_audit_restore_survives_clobber_on_first_write(self):
        """AC3: the clobber lands on the first restore write; the retry
        re-applies it and the item ends verified ``completed``/``in_review``."""
        runner = _RaceRunner(status="completed", stage="in_review")
        # First restore write lands, then a concurrent sync reverts it.
        runner.clobber_after_updates = {1: ("open", "plan_complete")}
        ctx = _lifecycle_ctx(
            runner, verdict=None, completed=False,
            script_failure={"script": "x", "error": "boom"},
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert (runner.status, runner.stage) == ("completed", "in_review")
        # The restore was retried after the clobbered write (>= 2 attempts).
        assert len(runner.restore_updates()) >= 2

    def test_settle_reverification_recovers_after_post_readback_clobber(self):
        """AC2: a clobber AFTER the first verified readback is caught by the
        settle re-verification, which restores the item again."""
        runner = _RaceRunner(status="completed", stage="in_review")
        # The immediate readback is verified, THEN a sync clobbers the item.
        runner.clobber_after_shows = {1: ("open", "plan_complete")}
        ctx = _lifecycle_ctx(
            runner, verdict=None, completed=False,
            script_failure={"script": "x", "error": "boom"},
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert (runner.status, runner.stage) == ("completed", "in_review")
        # The immediate write + the settle re-apply = at least two writes.
        assert len(runner.restore_updates()) >= 2

    def test_happy_path_yes_advance_makes_no_extra_read(self):
        """AC6: a passing 'Yes' advance is written + read back once, with no
        settle re-read (no measurable audit-duration regression)."""
        runner = _RaceRunner(status="in_progress", stage="in_progress")
        ctx = _lifecycle_ctx(runner, verdict="yes", completed=True)

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert (runner.status, runner.stage) == ("completed", "in_review")
        assert runner.item_shows == 1, (
            "the happy-path advance must make exactly one readback, got "
            f"{runner.item_shows}"
        )


class _CmdIssueRaceMixin:
    """Shared ``cmd_issue`` driver for the end-to-end race scenarios."""

    def _run(self, runner):
        return audit_runner.cmd_issue(
            "TEST-1", persist=False, force=True, runner=runner,
        )


class TestClobberBetweenClaimAndRestore(_CmdIssueRaceMixin):
    """AC1: a clobber between the audit status change and its restore."""

    def test_clobber_between_claim_and_restore_ends_completed_in_review(self):
        runner = _RaceRunner(status="completed", stage="in_review")
        runner.children_show_fails = True  # abort the audit after the claim
        # The restore write (the first update to `completed`) is clobbered.
        runner.clobber_first_completed_write = True

        rc = self._run(runner)

        assert rc == 1  # the audit aborted (script failure)
        assert (runner.status, runner.stage) == ("completed", "in_review")
        # The item was claimed in_progress and then restored.
        assert runner.restore_updates(), "expected a completed restore write"

    def test_exception_after_claim_restores_pre_audit_state(self):
        """AC4: an exception raised after the ``in_progress`` claim (here the
        audit-lease acquisition) still restores the captured pre-audit state —
        the item is never left ``in_progress``."""
        runner = _RaceRunner(status="completed", stage="in_review")

        def _boom(issue_id):
            raise RuntimeError("audit lease acquisition exploded")

        with mock.patch.object(
            audit_runner, "audit_lease_begin", side_effect=_boom,
        ):
            rc = self._run(runner)

        assert rc == 1
        assert (runner.status, runner.stage) == ("completed", "in_review")
        assert runner.status != "in_progress"
        claims = [
            u for u in runner.updates
            if runner._flag(u, "--status") in ("in_progress", "in-progress")
        ]
        assert claims, "expected the in_progress claim before the failure"
        assert runner.restore_updates(), "expected a completed restore write"

    def test_checkpoint_store_exception_restores_pre_audit_state(self):
        """AC4: a checkpoint-store failure right after the claim still restores
        the captured pre-audit state."""
        runner = _RaceRunner(status="completed", stage="in_review")

        with mock.patch.object(
            audit_runner, "_open_checkpoint_store",
            side_effect=RuntimeError("checkpoint store exploded"),
        ):
            rc = self._run(runner)

        assert rc == 1
        assert (runner.status, runner.stage) == ("completed", "in_review")
        assert runner.status != "in_progress"


class TestRestorePreAuditStateOnFailure:
    """The dedicated failure-restore helper is verified and reports non-zero."""

    def test_verified_restore_returns_zero(self):
        runner = _RaceRunner(status="in_progress", stage="in_review")
        ctx = _lifecycle_ctx(
            runner, verdict=None, completed=False,
            script_failure={"script": "x", "error": "boom"},
        )
        rc = audit_runner._restore_pre_audit_state_on_failure(ctx)
        assert rc == 0
        assert (runner.status, runner.stage) == ("completed", "in_review")

    def test_unverified_restore_returns_nonzero(self):
        """An unverified restore is never reported as success."""
        runner = _RaceRunner(status="in_progress", stage="in_review")
        # Every write is immediately clobbered to an unrelated stale state,
        # and the settle re-apply cannot converge.
        runner.clobber_after_updates = {i: ("open", "idea") for i in range(1, 16)}
        ctx = _lifecycle_ctx(
            runner, verdict=None, completed=False,
            script_failure={"script": "x", "error": "boom"},
        )
        rc = audit_runner._restore_pre_audit_state_on_failure(ctx)
        assert rc != 0


class TestRestoreCasGuards:
    """Restore writes are CAS-guarded and skip a redundant write (AC5).

    The restore path must never blind-overwrite a state it did not observe:
    each write carries ``--if-status``/``--if-stage`` for the lifecycle read
    immediately before it, and when the item already carries the target state
    (and no claim to release) no write is issued at all.
    """

    @staticmethod
    def _cas(cmd, name):
        return _RaceRunner._flag(cmd, name)

    def test_restore_write_is_cas_guarded_on_the_observed_state(self):
        """AC5: the restore write is conditional on the state just read."""
        runner = _RaceRunner(status="in_progress", stage="in_progress")
        ctx = _lifecycle_ctx(
            runner, verdict=None, completed=False,
            script_failure={"script": "x", "error": "boom"},
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert (runner.status, runner.stage) == ("completed", "in_review")
        restore_writes = runner.restore_updates()
        assert restore_writes, "expected a restore write to completed/in_review"
        first = restore_writes[0]
        assert self._cas(first, "--if-status") == "in_progress", first
        assert self._cas(first, "--if-stage") == "in_progress", first

    def test_redundant_restore_write_is_skipped(self):
        """AC5: nothing to do when the item already carries the target state."""
        runner = _RaceRunner(status="completed", stage="in_review", assignee="")
        ctx = _lifecycle_ctx(
            runner, verdict=None, completed=False,
            script_failure={"script": "x", "error": "boom"},
        )

        rc = audit_runner._restore_pre_audit_state_on_failure(ctx)

        assert rc == 0
        assert runner.updates == [], (
            "an already-restored item must not be written again, got "
            f"{runner.updates}"
        )

    def test_claim_release_still_writes_when_state_already_matches(self):
        """AC5: the assignee-release write is not skipped by the CAS shortcut."""
        runner = _RaceRunner(status="completed", stage="in_review", assignee="agent")
        ctx = _lifecycle_ctx(
            runner, verdict=None, completed=False,
            script_failure={"script": "x", "error": "boom"},
        )

        rc = audit_runner._restore_pre_audit_state_on_failure(ctx)

        assert rc == 0
        assert runner.assignee == "", "a lingering claim must be released"
        assert runner.updates, "the claim release requires a write"

    def test_cas_rejection_is_retried_with_the_refreshed_state(self):
        """AC5: a lost CAS race is re-read and retried, never blind-retried."""
        runner = _RaceRunner(status="in_progress", stage="in_progress")
        # Another session claims the item between our read and our write.
        runner.cas_reject_first_to = ("in_progress", "plan_complete")
        ctx = _lifecycle_ctx(
            runner, verdict=None, completed=False,
            script_failure={"script": "x", "error": "boom"},
        )

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert (runner.status, runner.stage) == ("completed", "in_review")
        assert runner.cas_rejections == 1
        assert len(runner.updates) >= 2, runner.updates
        assert self._cas(runner.updates[0], "--if-status") == "in_progress"
        # The retry is grounded on the refreshed read, not the stale one.
        assert self._cas(runner.updates[1], "--if-status") == "in_progress"
        assert self._cas(runner.updates[1], "--if-stage") == "plan_complete"

    def test_happy_path_advance_is_not_cas_guarded(self):
        """AC6: the 'Yes' advance keeps one write and one readback, no CAS read."""
        runner = _RaceRunner(status="in_progress", stage="in_progress")
        ctx = _lifecycle_ctx(runner, verdict="yes", completed=True)

        rc = audit_runner._apply_terminal_lifecycle(ctx)

        assert rc == 0
        assert (runner.status, runner.stage) == ("completed", "in_review")
        assert runner.item_shows == 1, runner.item_shows
        assert self._cas(runner.updates[0], "--if-status") is None, runner.updates[0]
