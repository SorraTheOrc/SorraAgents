#!/usr/bin/env python3
"""Unit and integration tests for StatusLifecycle context manager."""  # noqa: EXE001

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

from shared import status_lifecycle as status_lifecycle_module
from shared.status_lifecycle import ClaimError, StatusLifecycle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_run():
    """Fixture that patches subprocess.run and returns the mock.

    Also neutralizes ``worklog_dir_flag`` and the prefix-to-sibling scan so
    the wl command contents in these lifecycle tests are deterministic
    regardless of where pytest was invoked (e.g. from inside a git worktree,
    where the flag would otherwise be injected to point at the main
    checkout's .worklog). Flag-injection logic is covered separately by
    ``TestWorklogDirDetection``, the dedicated injection tests, and
    ``skill/shared/tests/test_shared_worklog_resolution.py``.
    """
    with (
        mock.patch("shared.status_lifecycle.subprocess.run") as m,
        mock.patch(
            "shared.status_lifecycle.worklog_dir_flag",
            return_value=[],
        ),
        mock.patch(
            "shared.status_lifecycle._find_worklog_dir_by_prefix",
            return_value=None,
        ),
    ):
        yield m


def _make_wl_show_proc(
    status: str = "open",
    stage: str = "idea",
    deleted_by: str = "",
    assignee: str = "",
    needs_producer_review: bool = False,
):
    """Build a mock CompletedProcess for ``wl show <id> --json``.

    ``deleted_by`` (SA-0MUJBLZC60085DI9) populates the soft-delete
    attribution field ``deletedBy``; an empty value means the item is live.

    ``assignee`` reports the claim already on the item (including the
    producer identity ``Map``, which is a normal claim since
    SA-0MW7QTAHW0030FGV). ``needs_producer_review`` marks the item as the
    persistent hold; releasing the assignee never clears the flag.
    """
    data = {
        "success": True,
        "workItem": {
            "id": "TEST-123",
            "status": status,
            "stage": stage,
            "deletedBy": deleted_by,
            "assignee": assignee,
            "needsProducerReview": needs_producer_review,
        },
    }
    return subprocess.CompletedProcess(
        args=["wl", "show", "TEST-123", "--json"],
        returncode=0,
        stdout=json.dumps(data),
        stderr="",
    )


def _make_wl_update_proc():
    """Build a mock CompletedProcess for a successful ``wl update``."""
    return subprocess.CompletedProcess(
        args=["wl", "update", "TEST-123", "--status", "in-progress", "--json"],
        returncode=0,
        stdout=json.dumps({"success": True}),
        stderr="",
    )


def _make_wl_failure_proc(returncode: int = 1, stderr: str = "error", stdout: str = ""):
    """Build a mock CompletedProcess for a failed ``wl`` command."""
    return subprocess.CompletedProcess(
        args=["wl", "update", "TEST-123", "--status", "in-progress", "--json"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ===========================================================================
# Unit tests (mocked subprocess)
# ===========================================================================


class TestStatusLifecycleUnit:
    """Unit tests using mocked subprocess.run."""

    def test_normal_lifecycle_open_to_completed(self, mock_run):
        """Normal: open → in_progress (entry) → completed (exit)."""
        # Simulate: wl show returns open, wl update succeeds
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),   # __enter__: capture original
            _make_wl_update_proc(),               # __enter__: set in_progress
            _make_wl_update_proc(),               # __exit__: set completed
        ]

        with StatusLifecycle("TEST-123"):
            pass

        # Verify calls
        assert mock_run.call_count == 3
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ["wl", "show", "TEST-123", "--json"]
        assert calls[1] == ["wl", "update", "TEST-123", "--status", "in-progress", "--json"]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "completed", "--json"]

    def test_failure_lifecycle_restores_original(self, mock_run):
        """Exception: open → in_progress → restore to open."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),  # restore
        ]

        with pytest.raises(ValueError, match="test error"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                raise ValueError("test error")

        # Verify: original status restored (open), no assignee (wasn't set)
        assert mock_run.call_count == 3
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "open", "--json"]
        assert "--assignee" not in calls[2]

    def test_idempotent_already_in_progress(self, mock_run):
        """Idempotent: already in-progress → still works (no unnecessary update)."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress"),  # already in-progress
            _make_wl_update_proc(),                      # set in-progress (no-op)
            _make_wl_update_proc(),                      # set completed
        ]

        with StatusLifecycle("TEST-123"):
            pass

        assert mock_run.call_count == 3
        calls = [c.args[0] for c in mock_run.call_args_list]
        # Still calls update in-progress (harmless) and then completed
        assert "in-progress" in calls[1]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "completed", "--json"]

    def test_stage_advancement_on_success(self, mock_run):
        """Successful exit with target_stage sets both completed + stage."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", target_stage="in_review"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]
        assert "--stage" in calls[2]
        assert "in_review" in calls[2]

    # ------------------------------------------------------------------
    # Stage validation (SA-0MUY9PMB7001ENMP): fail fast before invoking wl
    # ------------------------------------------------------------------

    def test_update_status_rejects_invalid_stage_before_wl(self, mock_run):
        """An invalid stage raises ValueError and never invokes ``wl``.

        ``in_progress`` is a status, not a stage; wl applies status and
        stage atomically, so letting the invalid stage reach wl would cancel
        the status change. Validation must fail fast (SA-0MUY9PMB7001ENMP).
        """
        with pytest.raises(ValueError, match="in_progress"):
            StatusLifecycle.update_status("TEST-123", "in-progress", stage="in_progress")

        # The wl CLI must not have been invoked at all.
        assert mock_run.call_count == 0

    def test_update_status_invalid_stage_lists_valid_stages(self, mock_run):
        """The validation error enumerates the valid wl stages."""
        with pytest.raises(ValueError) as excinfo:
            StatusLifecycle.update_status("TEST-123", "in-progress", stage="bogus")

        message = str(excinfo.value)
        for stage in ("idea", "intake_complete", "plan_complete", "in_review", "done"):
            assert stage in message
        assert mock_run.call_count == 0

    def test_update_status_accepts_valid_stage(self, mock_run):
        """A valid stage is forwarded to ``wl update`` unchanged."""
        mock_run.side_effect = [
            _make_wl_show_proc(assignee=""),  # claim read (nothing to release)
            _make_wl_update_proc(),            # the transition itself
        ]

        StatusLifecycle.update_status("TEST-123", "completed", stage="in_review")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[1] == [
            "wl", "update", "TEST-123", "--status", "completed",
            "--json", "--stage", "in_review",
        ]

    def test_update_status_omits_stage_when_none(self, mock_run):
        """When no stage is supplied, no ``--stage`` flag is emitted."""
        mock_run.side_effect = [_make_wl_update_proc()]

        StatusLifecycle.update_status("TEST-123", "in-progress")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert "--stage" not in calls[0]

    @staticmethod
    def _release_value(call: list[str]) -> str | None:
        """Return the ``--assignee`` value of a ``wl update`` argv (or None)."""
        if "--assignee" not in call:
            return None
        return call[call.index("--assignee") + 1]

    # ------------------------------------------------------------------
    # Direct durable writes release too (SA-0MW0F91KN004J06U)
    # ------------------------------------------------------------------

    def test_update_status_releases_external_claim_on_terminal_write(self, mock_run):
        """A direct ``update_status`` terminal write releases a pre-claim.

        Prose-only skills (``plan``) and direct durable writes
        (``tree_coverage._advance_stage``, ``effort-and-risk``'s status
        restore) never enter the context manager, so the choke point itself
        must release the workflow pre-claim — the exact gap the producer
        review rejected (the plan skill left the item on its agent's hold).
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress", assignee="plan-agent"),
            _make_wl_update_proc(),
        ]

        StatusLifecycle.update_status(
            "TEST-123", "open", stage="plan_complete"
        )

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ["wl", "show", "TEST-123", "--json"]
        assert self._release_value(calls[1]) == ""
        assert "--stage" in calls[1] and "plan_complete" in calls[1]

    def test_update_status_stage_advance_releases_external_claim(self, mock_run):
        """The AC-coverage stage advance (status ``open``) releases a pre-claim."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress", assignee="intake-agent"),
            _make_wl_update_proc(),
        ]

        StatusLifecycle.update_status("TEST-123", "open", stage="intake_complete")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[-1]) == ""

    def test_update_status_claim_never_releases(self, mock_run):
        """A transition *to* in-progress is a claim, never a release."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", assignee="current-agent"),
            _make_wl_update_proc(),
        ]

        StatusLifecycle.update_status("TEST-123", "in-progress")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[-1]) is None

    def test_update_status_releases_producer_identity(self, mock_run):
        """The producer identity ``Map`` is released like any other claim.

        ``Map`` is not a persistent hold; leaving it on a planned item strands
        it as a scheduling hold (SA-0MW7QTAHW0030FGV).
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress", assignee="Map"),
            _make_wl_update_proc(),
        ]

        StatusLifecycle.update_status("TEST-123", "open", stage="plan_complete")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[-1]) == ""

    def test_update_status_releases_assignee_on_needs_producer_review_item(self, mock_run):
        """A ``needsProducerReview`` item keeps the flag but releases the assignee.

        The flag is the persistent hold, never the assignee; the release must
        not clear it (SA-0MW7QTAHW0030FGV AC4).
        """
        mock_run.side_effect = [
            _make_wl_show_proc(
                status="in-progress",
                assignee="audit-agent",
                needs_producer_review=True,
            ),
            _make_wl_update_proc(),
        ]

        StatusLifecycle.update_status("TEST-123", "blocked")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[-1]) == ""
        # The flag is never written by this transition, so the store keeps it.
        assert "--needs-producer-review" not in calls[-1]

    def test_update_status_handoff_releases_assignee_and_flags_review(self, mock_run):
        """Flagging producer review in the same write still releases the claim.

        The flag is the hold; a lingering assignee beside it would make the
        item indistinguishable from a stranded claim (SA-0MW7QTAHW0030FGV).
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress", assignee="Map"),
            _make_wl_update_proc(),
        ]

        StatusLifecycle.update_status(
            "TEST-123", "open", needs_producer_review=True
        )

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[-1]) == ""
        # The review flag itself is still written, so the hold is sanctioned.
        assert "--needs-producer-review" in calls[-1]
        assert "yes" in calls[-1]

    def test_update_status_explicit_assignee_is_written_verbatim(self, mock_run):
        """An explicit assignee disables the automatic release (no claim read)."""
        mock_run.side_effect = [_make_wl_update_proc()]

        StatusLifecycle.update_status("TEST-123", "completed", assignee="")

        calls = [c.args[0] for c in mock_run.call_args_list]
        # Only the update ran — an explicit value needs no inspection.
        assert len(calls) == 1
        assert self._release_value(calls[0]) == ""

    def test_update_status_release_opt_out_never_releases(self, mock_run):
        """``release_assignee=False`` keeps the hold even on a terminal write."""
        mock_run.side_effect = [_make_wl_update_proc()]

        StatusLifecycle.update_status(
            "TEST-123", "completed", release_assignee=False
        )

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[0]) is None

    def test_update_status_tolerates_unreadable_item(self, mock_run):
        """An unreadable item never blocks the transition (release is a backstop)."""
        mock_run.side_effect = [
            _make_wl_failure_proc(stdout=json.dumps({
                "success": False, "error": "work item not found",
            })),
            _make_wl_update_proc(),
        ]

        StatusLifecycle.update_status("TEST-123", "open", stage="plan_complete")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[-1][:4] == ["wl", "update", "TEST-123", "--status"]
        assert self._release_value(calls[-1]) is None

    def test_assignee_held_on_entry_released_on_success(self, mock_run):
        """A claim the context manager holds is released on the success exit.

        Implement/intake claim with an assignee (a scheduling hold) and must
        clear it when the run completes; the release is folded into the
        terminal ``completed`` update (assignee-hold convention). Release is
        the **default** — no opt-in flag is passed (SA-0MVVF6OJY000JZBL).
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", assignee="bot"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        # Entry sets assignee
        assert calls[1][:5] == ["wl", "update", "TEST-123", "--status", "in-progress"]
        assert calls[1][calls[1].index("--assignee") + 1] == "bot"
        # Exit is completed AND releases the hold it set
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]
        assert self._release_value(calls[2]) == ""

    def test_success_exit_releases_preexisting_producer_identity(self, mock_run):
        """A pre-existing ``Map`` claim is released on the success exit.

        The context manager was constructed without ``assignee=``, but the
        producer identity is a normal claim: leaving it behind after a run
        strands the item (SA-0MW7QTAHW0030FGV).
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", assignee="Map"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]
        assert self._release_value(calls[2]) == ""

    def test_success_exit_releases_externally_set_agent_claim(self, mock_run):
        """A pre-existing claim the run never held is released.

        The workflow claims (``AGENTS_GLOBAL.md`` step 1) *before* a skill
        runs, so a pre-set claim must not survive the skill's own terminal
        transition (SA-0MW0F91KN004J06U). No assignee is exempt.
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress", assignee="refactor-agent"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]
        assert self._release_value(calls[2]) == ""

    def test_failure_exit_releases_externally_set_agent_claim(self, mock_run):
        """A rollback also releases a pre-existing claim."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress", assignee="audit-agent"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with pytest.raises(RuntimeError):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                raise RuntimeError("boom")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == [
            "wl", "update", "TEST-123", "--status", "in-progress"
        ]
        assert self._release_value(calls[2]) == ""

    def test_restore_on_exit_releases_externally_set_agent_claim(self, mock_run):
        """Read-only skills release the pre-claim they never set themselves.

        ``find-related`` / ``refactor`` use ``restore_on_exit=True``; before
        the widened rule the externally pre-set claim survived the run
        (SA-0MW0F91KN004J06U).
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", assignee="find-related-runner"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", restore_on_exit=True):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "open"]
        assert self._release_value(calls[2]) == ""

    def test_needs_producer_review_flag_survives_assignee_release(self, mock_run):
        """The ``needsProducerReview`` flag survives, but the assignee is released.

        The flag — never the assignee — is the persistent hold
        (SA-0MW7QTAHW0030FGV AC4).
        """
        mock_run.side_effect = [
            _make_wl_show_proc(
                status="in-progress",
                assignee="audit-agent",
                needs_producer_review=True,
            ),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[2]) == ""
        # The exit write never passes the flag, so the store keeps it set.
        assert "--needs-producer-review" not in calls[2]

    def test_assignee_cleared_on_failure(self, mock_run):
        """Failure with assignee restores status AND clears assignee."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),  # restore
        ]

        with pytest.raises(RuntimeError):  # noqa: SIM117
            with StatusLifecycle("TEST-123", assignee="bot"):
                raise RuntimeError("boom")

        calls = [c.args[0] for c in mock_run.call_args_list]
        # Restore status AND clear assignee
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "open"]
        assert self._release_value(calls[2]) == ""

    def test_failure_exit_releases_preexisting_producer_identity(self, mock_run):
        """A rollback releases a pre-existing ``Map`` claim too."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", assignee="Map"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with pytest.raises(RuntimeError):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                raise RuntimeError("boom")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "open"]
        assert self._release_value(calls[2]) == ""

    def test_restore_on_exit_releases_held_assignee(self, mock_run):
        """restore_on_exit restores the status AND releases a held assignee."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", assignee="bot", restore_on_exit=True):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "open"]
        assert self._release_value(calls[2]) == ""

    def test_restore_on_exit_releases_preexisting_producer_identity(self, mock_run):
        """Read-only analysis releases a pre-existing ``Map`` claim too."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", assignee="Map"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", restore_on_exit=True):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "open"]
        assert self._release_value(calls[2]) == ""

    def test_preserve_assignee_on_exit_keeps_hold_on_success(self, mock_run):
        """The documented opt-out preserves the hold on the success exit."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle(
            "TEST-123", assignee="bot", preserve_assignee_on_exit=True
        ):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]
        assert self._release_value(calls[2]) is None

    def test_preserve_assignee_on_exit_keeps_hold_on_failure(self, mock_run):
        """The opt-out also preserves the hold on a rollback exit."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with pytest.raises(RuntimeError):  # noqa: SIM117
            with StatusLifecycle(
                "TEST-123", assignee="bot", preserve_assignee_on_exit=True
            ):
                raise RuntimeError("boom")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "open"]
        assert self._release_value(calls[2]) is None

    def test_preserve_assignee_on_exit_keeps_hold_on_restore(self, mock_run):
        """The opt-out preserves the hold on a restore_on_exit exit."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle(
            "TEST-123",
            assignee="bot",
            restore_on_exit=True,
            preserve_assignee_on_exit=True,
        ):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "open"]
        assert self._release_value(calls[2]) is None

    def test_hold_is_held_during_run_and_released_on_exit(self, mock_run):
        """The hold is held during the run and only released on exit."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", assignee="bot"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        # Entry still sets the assignee, so the claim is held while working...
        assert calls[1][calls[1].index("--assignee") + 1] == "bot"
        # ...and the exit clears it together with the completed transition.
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]
        assert self._release_value(calls[2]) == ""

    def test_legacy_clear_flag_is_deprecated_and_has_no_effect(self, mock_run):
        """``clear_assignee_on_exit`` warns; release is already the default.

        The legacy flag cannot change the outcome. A pre-existing ``Map``
        claim is released by the default rule even when the flag is passed.
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", assignee="Map"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with (
            pytest.warns(DeprecationWarning, match="clear_assignee_on_exit"),
            StatusLifecycle("TEST-123", clear_assignee_on_exit=True),
        ):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]
        assert self._release_value(calls[2]) == ""

    def test_legacy_clear_flag_warns_when_false_too(self, mock_run):
        """Passing the deprecated flag at all (even ``False``) warns."""
        with pytest.warns(DeprecationWarning, match="clear_assignee_on_exit"):
            StatusLifecycle("TEST-123", clear_assignee_on_exit=False)

        assert mock_run.call_count == 0

    def test_legacy_flag_cannot_contradict_the_preserve_opt_out(self, mock_run):
        """Two flags never contradict: the documented opt-out wins."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with (
            pytest.warns(DeprecationWarning, match="clear_assignee_on_exit"),
            StatusLifecycle(
                "TEST-123",
                assignee="bot",
                clear_assignee_on_exit=True,
                preserve_assignee_on_exit=True,
            ),
        ):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[2]) is None

    def test_release_is_idempotent_across_runs(self, mock_run):
        """Releasing an already-released assignee is a harmless no-op write."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
            _make_wl_show_proc(status="completed", stage="in_review"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", assignee="bot"):
            pass
        with StatusLifecycle("TEST-123", assignee="bot"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert self._release_value(calls[2]) == ""
        assert self._release_value(calls[5]) == ""

    def test_wl_show_failure_then_update_fails(self, mock_run):
        """wl show failure is caught, but in_progress update failure raises."""
        mock_run.side_effect = [
            _make_wl_failure_proc(returncode=1, stderr="not found"),  # show fails
            _make_wl_failure_proc(returncode=1, stderr="update failed"),  # update fails
        ]

        with pytest.raises(RuntimeError, match="update failed"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                pass

    def test_wl_update_failure_on_entry_raises(self, mock_run):
        """wl update failure on entry raises and does NOT restore."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_failure_proc(returncode=1, stderr="permission denied"),
        ]

        with pytest.raises(RuntimeError, match="permission denied"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                pass

    def test_wl_update_failure_on_exit_raises(self, mock_run):
        """wl update failure on successful exit raises (caller catches)."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_failure_proc(returncode=1, stderr="timeout"),
        ]

        with pytest.raises(RuntimeError, match="timeout"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                pass

    def test_wl_update_failure_on_restore_logged(self, mock_run, caplog):
        """wl update failure during exception restore is logged, not raised."""
        import logging
        caplog.set_level(logging.WARNING)

        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_failure_proc(returncode=1, stderr="restore failed"),
        ]

        with pytest.raises(ValueError, match="inner error"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                raise ValueError("inner error")

        # The original exception is reraised, restore failure is logged
        assert "Failed to restore" in caplog.text
        assert "TEST-123" in caplog.text

    def test_completed_already(self, mock_run):
        """Starting from completed: capture, set in_progress, set completed on exit."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="completed"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123"):
            pass

    def test_restore_on_exit_success_restores_original(self, mock_run):
        """restore_on_exit: success restores original status, never completed.

        Read-only skills (find-related, refactor, effort-and-risk) must not
        advance the item to `completed`, which wl only allows for
        in_review/done stages.
        """
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),   # __enter__: capture original
            _make_wl_update_proc(),               # __enter__: set in_progress
            _make_wl_update_proc(),               # __exit__: restore to open
        ]

        with StatusLifecycle("TEST-123", restore_on_exit=True):
            pass

        assert mock_run.call_count == 3
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ["wl", "show", "TEST-123", "--json"]
        assert calls[1] == ["wl", "update", "TEST-123", "--status", "in-progress", "--json"]
        # Restored, NOT advanced to completed
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "open", "--json"]

    def test_restore_on_exit_failure_restores_original(self, mock_run):
        """restore_on_exit: exception restores the original status."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with pytest.raises(ValueError, match="boom"):  # noqa: SIM117
            with StatusLifecycle("TEST-123", restore_on_exit=True):
                raise ValueError("boom")

        assert mock_run.call_count == 3
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "open", "--json"]

    def test_restore_on_exit_preserves_stage(self, mock_run):
        """restore_on_exit: stage is never modified on exit."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", stage="plan_complete"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", restore_on_exit=True):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert "--stage" not in calls[2], (
            f"restore_on_exit must not touch stage: {calls[2]}"
        )
        # Still sets in-progress on entry, restores original on exit
        assert "in-progress" in calls[1]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "open", "--json"]

    # ------------------------------------------------------------------
    # Error detail propagation (AC: no more empty error strings)
    # ------------------------------------------------------------------

    def test_error_detail_parses_stdout_json_error(self, mock_run):
        """Failed wl command surfaces the stdout JSON error, not an empty string.

        wl prints its error as JSON on stdout (e.g.
        ``{"success": false, "error": "..."}``) with a non-zero exit code
        and empty stderr. The raised error must include that detail.
        """
        mock_run.side_effect = [
            _make_wl_failure_proc(
                returncode=1,
                stdout=json.dumps({
                    "success": False,
                    "error": "Worklog system is not initialized. Run \"worklog init\" first.",
                }),
                stderr="",
            ),
        ]

        with pytest.raises(RuntimeError, match="Worklog system is not initialized"):
            StatusLifecycle.update_status("TEST-123", "in-progress")

    def test_error_detail_prefers_stdout_error_over_stderr(self, mock_run):
        """stdout JSON error detail takes precedence over stderr text."""
        mock_run.side_effect = [
            _make_wl_failure_proc(
                returncode=1,
                stdout=json.dumps({"success": False, "error": "stdout error detail"}),
                stderr="stderr detail",
            ),
        ]

        with pytest.raises(RuntimeError, match="stdout error detail"):
            StatusLifecycle.update_status("TEST-123", "in-progress")

    def test_error_detail_falls_back_to_stderr(self, mock_run):
        """When stdout is empty, stderr detail is used."""
        mock_run.side_effect = [
            _make_wl_failure_proc(returncode=1, stdout="", stderr="stderr detail"),
        ]

        with pytest.raises(RuntimeError, match="stderr detail"):
            StatusLifecycle.update_status("TEST-123", "in-progress")

    def test_error_detail_includes_plain_stdout(self, mock_run):
        """Non-JSON stdout is included verbatim as fallback detail."""
        mock_run.side_effect = [
            _make_wl_failure_proc(returncode=1, stdout="something went wrong", stderr=""),
        ]

        with pytest.raises(RuntimeError, match="something went wrong"):
            StatusLifecycle.update_status("TEST-123", "in-progress")

    # ------------------------------------------------------------------
    # worklog-dir injection (cwd-independent wl invocation)
    # ------------------------------------------------------------------

    def test_worklog_dir_flag_injected_when_detected(self, mock_run):
        """When a worklog dir is resolved, --worklog-dir is injected after wl."""
        with mock.patch(
            "shared.status_lifecycle.worklog_dir_flag",
            return_value=["--worklog-dir", "/fake/proj/.worklog"],
        ):
            mock_run.side_effect = [_make_wl_update_proc()]

            StatusLifecycle.update_status("TEST-123", "in-progress")

            calls = [c.args[0] for c in mock_run.call_args_list]
            assert calls[0][:4] == [
                "wl", "--worklog-dir", "/fake/proj/.worklog", "update",
            ]
            assert "--status" in calls[0]

    def test_no_flag_when_cwd_is_worklog_root(self, mock_run):
        """When cwd is already a worklog root, no flag is injected."""
        with mock.patch(
            "shared.status_lifecycle.worklog_dir_flag",
            return_value=[],
        ):
            mock_run.side_effect = [_make_wl_update_proc()]

            StatusLifecycle.update_status("TEST-123", "in-progress")

            calls = [c.args[0] for c in mock_run.call_args_list]
            assert calls[0] == ["wl", "update", "TEST-123", "--status", "in-progress", "--json"]

    # ------------------------------------------------------------------
    # Claim invariant (SA-0MTFTFUIH000UWM9): require_claimed / ensure_claimed
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("in_progress", "in-progress"),
            ("in-progress", "in-progress"),
            ("open", "open"),
            ("completed", "completed"),
            ("", ""),
        ],
    )
    def test_normalise_status(self, raw, expected):
        """_normalise_status canonicalises underscores to hyphens (AC3)."""
        assert status_lifecycle_module._normalise_status(raw) == expected

    def test_require_claimed_passes_when_in_progress(self, mock_run):
        """require_claimed returns the show payload when status is in-progress (hyphenated)."""
        mock_run.side_effect = [_make_wl_show_proc(status="in-progress")]
        data = StatusLifecycle.require_claimed("TEST-123")
        assert data["workItem"]["status"] == "in-progress"
        assert mock_run.call_count == 1
        assert mock_run.call_args_list[0].args[0] == ["wl", "show", "TEST-123", "--json"]

    def test_require_claimed_raises_claim_error_when_open(self, mock_run):
        """require_claimed raises ClaimError (subclass of RuntimeError) when open."""
        mock_run.side_effect = [_make_wl_show_proc(status="open")]
        with pytest.raises(ClaimError, match="must be in-progress"):
            StatusLifecycle.require_claimed("TEST-123")
        # Legacy handlers catching RuntimeError must still catch ClaimError
        mock_run.side_effect = [_make_wl_show_proc(status="open")]
        with pytest.raises(RuntimeError, match="must be in-progress"):
            StatusLifecycle.require_claimed("TEST-123")

    def test_require_claimed_raises_claim_error_when_completed(self, mock_run):
        """require_claimed raises ClaimError for a completed item (AC2).

        A completed item is not claimable, so the guard must reject it just
        as it rejects an open item (regression guard for the hyphen/
        underscore comparison bug this work item fixes).
        """
        mock_run.side_effect = [_make_wl_show_proc(status="completed")]
        with pytest.raises(ClaimError, match="must be in-progress"):
            StatusLifecycle.require_claimed("TEST-123")

    def test_require_claimed_uses_runner(self, mock_run):
        """require_claimed honors an injected runner."""
        seen: list[list[str]] = []

        def runner(cmd: list[str]):
            seen.append(cmd)
            return _make_wl_show_proc(status="in-progress")

        StatusLifecycle.require_claimed("TEST-123", runner=runner)
        assert seen[0][0] == "wl"

    def test_ensure_claimed_noop_when_in_progress(self, mock_run):
        """ensure_claimed is a no-op (show only) when already in-progress (hyphenated)."""
        mock_run.side_effect = [_make_wl_show_proc(status="in-progress")]
        data = StatusLifecycle.ensure_claimed("TEST-123")
        assert data["workItem"]["status"] == "in-progress"
        assert mock_run.call_count == 1

    def test_ensure_claimed_reclaims_when_open(self, mock_run):
        """ensure_claimed reclaims via wl update when status is open."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
        ]
        StatusLifecycle.ensure_claimed("TEST-123")
        assert mock_run.call_count == 2
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ["wl", "show", "TEST-123", "--json"]
        assert calls[1][:5] == ["wl", "update", "TEST-123", "--status", "in-progress"]

    def test_ensure_claimed_propagates_assignee_on_reclaim(self, mock_run):
        """ensure_claimed passes assignee through to wl update when reclaiming."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
        ]
        StatusLifecycle.ensure_claimed("TEST-123", assignee="bot")
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert "--assignee" in calls[1]
        assert "bot" in calls[1]

    def test_reclaim_alias_is_idempotent(self, mock_run):
        """reclaim() is an alias for ensure_claimed with identical behavior."""
        mock_run.side_effect = [_make_wl_show_proc(status="in-progress")]
        data = StatusLifecycle.reclaim("TEST-123")
        assert data["workItem"]["status"] == "in-progress"
        assert mock_run.call_count == 1

    # ------------------------------------------------------------------
    # Hyphen vs underscore distinction (SA-0MTYMFLZD004O2VV)
    # ------------------------------------------------------------------

    def test_require_claimed_passes_with_hyphenated_status(self, mock_run):
        """require_claimed must pass when wl returns 'in-progress' (hyphen).

        The wl CLI normalises underscore → hyphen on write and returns
        'in-progress' in JSON. Before the fix, the guard compared against
        'in_progress' (underscore) and always raised ClaimError.
        """
        mock_run.side_effect = [_make_wl_show_proc(status="in-progress")]
        data = StatusLifecycle.require_claimed("TEST-123")
        assert data["workItem"]["status"] == "in-progress"
        assert mock_run.call_count == 1

    def test_require_claimed_passes_with_underscore_status(self, mock_run):
        """require_claimed accepts the underscore variant via normalisation (AC1).

        Historically the guard compared against a single literal spelling.
        LP-0MUESDZVW006YJ1N normalises ``_`` → ``-`` so either spelling
        returned by ``wl`` satisfies the claim guard.
        """
        mock_run.side_effect = [_make_wl_show_proc(status="in_progress")]
        data = StatusLifecycle.require_claimed("TEST-123")
        assert data["workItem"]["status"] == "in_progress"
        assert mock_run.call_count == 1

    def test_ensure_claimed_noop_with_hyphenated_status(self, mock_run):
        """ensure_claimed must be a no-op when wl returns 'in-progress' (hyphen).

        Before the fix, ensure_claimed never recognised an existing claim
        because it compared against 'in_progress' (underscore), so it
        performed a redundant write on every call.
        """
        mock_run.side_effect = [_make_wl_show_proc(status="in-progress")]
        data = StatusLifecycle.ensure_claimed("TEST-123")
        assert data["workItem"]["status"] == "in-progress"
        assert mock_run.call_count == 1  # show only, no update

    def test_ensure_claimed_noop_with_underscore_status(self, mock_run):
        """ensure_claimed recognises the underscore variant as already claimed (AC3).

        The sibling comparison must normalise consistently with
        require_claimed, otherwise an underscore status triggers a redundant
        write on every call.
        """
        mock_run.side_effect = [_make_wl_show_proc(status="in_progress")]
        data = StatusLifecycle.ensure_claimed("TEST-123")
        assert data["workItem"]["status"] == "in_progress"
        assert mock_run.call_count == 1  # show only, no update

    def test_ensure_claimed_reclaims_with_hyphenated_status(self, mock_run):
        """ensure_claimed reclaims via wl update when status differs from 'in-progress'."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
        ]
        StatusLifecycle.ensure_claimed("TEST-123")
        assert mock_run.call_count == 2
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[1][:5] == ["wl", "update", "TEST-123", "--status", "in-progress"]
        assert "in_progress" not in calls[1], (
            "ensure_claimed should not emit underscore variant"
        )

    def test_pause_approval_reclaim_lifecycle(self, mock_run):
        """Pause→approval→reclaim window stays continuously in-progress (hyphenated).

        Reproduces the AH-0MTFPDKDU006QUDC gap (12:08:51Z released to open, never
        reclaimed after 12:09:42Z approval): a resumed run that forgot to
        re-claim would fail the require_claimed guard; with the re-claim it
        passes and stays in-progress through mutation.
        """
        # Pre-approval: item is open (paused for approval)
        mock_run.side_effect = [_make_wl_show_proc(status="open")]
        with pytest.raises(ClaimError):
            StatusLifecycle.require_claimed("TEST-123")
        # Post-approval re-claim: ensure_claimed restores in-progress
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
        ]
        StatusLifecycle.ensure_claimed("TEST-123")
        # Subsequent mutation guard now passes
        mock_run.side_effect = [_make_wl_show_proc(status="in-progress")]
        data = StatusLifecycle.require_claimed("TEST-123")
        assert data["workItem"]["status"] == "in-progress"

    # ------------------------------------------------------------------
    # Soft-delete guard (SA-0MUJBLZC60085DI9): a soft-deleted item must
    # never be claimed — otherwise the downtime dispatcher re-selects it.
    # ------------------------------------------------------------------

    def test_require_claimed_raises_claim_error_when_soft_deleted(self, mock_run):
        """require_claimed refuses a soft-deleted item even when status is in-progress (AC2)."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress", deleted_by="someone@example.com"),
        ]
        with pytest.raises(ClaimError, match="soft-deleted"):
            StatusLifecycle.require_claimed("TEST-123")
        # Only the read happened; no mutation/claim was attempted.
        assert mock_run.call_count == 1

    def test_require_claimed_soft_deleted_error_names_item(self, mock_run):
        """The soft-delete refusal names the deleted item for the operator (AC2)."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", deleted_by="deleter@example.com"),
        ]
        with pytest.raises(ClaimError) as excinfo:
            StatusLifecycle.require_claimed("TEST-123")
        assert "TEST-123" in str(excinfo.value)

    def test_ensure_claimed_refuses_soft_deleted_without_write(self, mock_run):
        """ensure_claimed raises ClaimError and never issues a reclaim write (AC2)."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open", deleted_by="deleter@example.com"),
        ]
        with pytest.raises(ClaimError, match="soft-deleted"):
            StatusLifecycle.ensure_claimed("TEST-123")
        # show only — the reclaim `wl update` must never run for a deleted item.
        assert mock_run.call_count == 1

    def test_ensure_claimed_refuses_soft_deleted_in_progress(self, mock_run):
        """ensure_claimed refuses a soft-deleted item even if it looks in-progress (AC2)."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="in-progress", deleted_by="deleter@example.com"),
        ]
        with pytest.raises(ClaimError, match="soft-deleted"):
            StatusLifecycle.ensure_claimed("TEST-123")
        assert mock_run.call_count == 1

    def test_soft_deleted_guards_ignore_blank_deleted_by(self, mock_run):
        """An empty/whitespace deletedBy is not a deletion (live item proceeds)."""
        mock_run.side_effect = [_make_wl_show_proc(status="in-progress", deleted_by="   ")]
        data = StatusLifecycle.require_claimed("TEST-123")
        assert data["workItem"]["status"] == "in-progress"


# ===========================================================================
# Worklog-dir detection tests
# ===========================================================================


class TestWorklogDirDetection:
    """Tests for worklog-dir auto-detection (cwd-independent wl invocation)."""

    def test_flag_empty_when_cwd_is_worklog_root(self, tmp_path, monkeypatch):
        """Running from an initialized worklog root: no flag needed."""
        proj = tmp_path / "proj"
        (proj / ".worklog").mkdir(parents=True)
        (proj / ".worklog" / "initialized").write_text("{}")
        monkeypatch.chdir(proj)
        assert status_lifecycle_module.worklog_dir_flag() == []

    def test_flag_set_when_detection_resolves_dir(self, monkeypatch, tmp_path):
        """Non-root cwd + resolvable worklog dir -> flag returned."""
        monkeypatch.chdir(tmp_path)  # cwd has no .worklog
        wl_dir = tmp_path / "proj" / ".worklog"
        with mock.patch(
            "shared.status_lifecycle._detect_worklog_dir",
            return_value=wl_dir,
        ):
            assert status_lifecycle_module.worklog_dir_flag() == [
                "--worklog-dir", str(wl_dir),
            ]

    def test_flag_empty_when_no_dir_detected(self, monkeypatch, tmp_path):
        """Non-root cwd with no resolvable worklog dir -> no flag (graceful)."""
        monkeypatch.chdir(tmp_path)
        with mock.patch(
            "shared.status_lifecycle._detect_worklog_dir",
            return_value=None,
        ):
            assert status_lifecycle_module.worklog_dir_flag() == []

    def test_detect_cwd_direct(self, monkeypatch, tmp_path):
        """cwd/.worklog is found first."""
        proj = tmp_path / "proj"
        (proj / ".worklog").mkdir(parents=True)
        monkeypatch.chdir(proj)
        assert status_lifecycle_module._detect_worklog_dir() == proj / ".worklog"

    def test_detect_git_root(self, monkeypatch, tmp_path):
        """git rev-parse --show-toplevel/.worklog is used when cwd is a subdir."""
        root = tmp_path / "repo"
        (root / ".worklog").mkdir(parents=True)
        (root / "sub").mkdir(parents=True)
        monkeypatch.chdir(root / "sub")
        fake = subprocess.CompletedProcess(
            ["git", "rev-parse", "--show-toplevel"],
            0,
            stdout=str(root),
            stderr="",
        )
        with mock.patch("shared.status_lifecycle.subprocess.run", return_value=fake):
            assert status_lifecycle_module._detect_worklog_dir() == root / ".worklog"

    def test_detect_walk_up(self, monkeypatch, tmp_path):
        """Ancestor .worklog is found when git resolution fails."""
        proj = tmp_path / "proj"
        (proj / ".worklog").mkdir(parents=True)
        deep = proj / "a" / "b"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        fake = subprocess.CompletedProcess(
            ["git", "rev-parse", "--show-toplevel"],
            1,
            stdout="",
            stderr="",
        )
        with mock.patch("shared.status_lifecycle.subprocess.run", return_value=fake):
            assert status_lifecycle_module._detect_worklog_dir() == proj / ".worklog"

    def test_detect_none(self, monkeypatch):
        """No .worklog anywhere -> None.

        Hermetic: ``_detect_worklog_dir`` scans the real filesystem up the
        cwd ancestor chain, so chdir'ing to ``tmp_path`` (under ``/tmp``) is
        not isolated — a stray ``.worklog`` left in ``/tmp`` by other tooling
        makes the scan return it instead of ``None``. Point ``Path.cwd`` at a
        synthetic clean root so the test is order/environment independent
        (SA-0MSDXF3BP0022NDE).
        """
        clean_cwd = Path(f"/__no-worklog-{uuid.uuid4().hex}__/project/sub")
        monkeypatch.setattr(
            status_lifecycle_module.Path,
            "cwd",
            classmethod(lambda cls: clean_cwd),
        )
        fake = subprocess.CompletedProcess(
            ["git", "rev-parse", "--show-toplevel"],
            1,
            stdout="",
            stderr="",
        )
        with mock.patch("shared.status_lifecycle.subprocess.run", return_value=fake):
            assert status_lifecycle_module._detect_worklog_dir() is None


# ===========================================================================
# Integration tests (real wl CLI, optional)
# ===========================================================================

@pytest.mark.skipif(
    not bool(os.environ.get("RUN_INTEGRATION_TESTS")),
    reason="Integration tests require RUN_INTEGRATION_TESTS=1",
)
class TestStatusLifecycleIntegration:
    """Integration tests using the real wl CLI.

    These create a real work item, run the lifecycle, and verify transitions.
    """

    @pytest.fixture
    def temp_work_item(self):
        """Create a temporary work item for testing, clean up afterward."""
        result = subprocess.run(
            [
                "wl", "create",
                "--title", "Temp test item (StatusLifecycle integration)",
                "--description", "Auto-created by test_status_lifecycle.py",
                "--priority", "low",
                "--issue-type", "task",
                "--status", "open",
                "--json",
            ],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        work_item = data.get("workItem", data)
        item_id = work_item["id"]
        yield item_id
        # Cleanup: close the item
        subprocess.run(  # noqa: PLW1510
            ["wl", "update", item_id, "--status", "completed", "--json"],
            capture_output=True,
        )

    def test_status_transitions_real(self, temp_work_item):
        """Verify real status transitions: open → in_progress → completed."""
        item_id = temp_work_item

        # Verify starts as open
        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        initial = json.loads(show.stdout)
        assert initial["workItem"]["status"] == "open"

        # Run lifecycle
        with StatusLifecycle(item_id):
            # Inside the context: should be in_progress
            show = subprocess.run(
                ["wl", "show", item_id, "--json"],
                capture_output=True, text=True, check=True,
            )
            inside = json.loads(show.stdout)
            assert inside["workItem"]["status"] == "in-progress"

        # After context: should be completed
        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        after = json.loads(show.stdout)
        assert after["workItem"]["status"] == "completed"

    def test_failure_restores_status_real(self, temp_work_item):
        """Verify exception restores original status."""
        item_id = temp_work_item

        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        initial = json.loads(show.stdout)
        original_status = initial["workItem"]["status"]

        with pytest.raises(ValueError):  # noqa: SIM117
            with StatusLifecycle(item_id):
                raise ValueError("intentional failure")

        # After exception: status should be restored
        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        after = json.loads(show.stdout)
        assert after["workItem"]["status"] == original_status

    def test_stage_advancement_real(self, temp_work_item):
        """Verify stage advancement on success."""
        item_id = temp_work_item

        with StatusLifecycle(item_id, target_stage="in_review"):
            pass

        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        after = json.loads(show.stdout)
        assert after["workItem"]["status"] == "completed"
        assert after["workItem"]["stage"] == "in_review"

    def test_assignee_real(self, temp_work_item):
        """Verify assignee is set on entry."""
        item_id = temp_work_item

        with StatusLifecycle(item_id, assignee="integration-test"):
            show = subprocess.run(
                ["wl", "show", item_id, "--json"],
                capture_output=True, text=True, check=True,
            )
            inside = json.loads(show.stdout)
            assert inside["workItem"]["assignee"] == "integration-test"

    def test_assignee_released_on_success_exit_real(self, temp_work_item):
        """Verify the real store releases a held assignee by default."""
        item_id = temp_work_item

        with StatusLifecycle(item_id, assignee="integration-test"):
            pass

        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        after = json.loads(show.stdout)
        assert after["workItem"]["status"] == "completed"
        assert after["workItem"]["assignee"] == ""

    def test_preserve_assignee_on_exit_real(self, temp_work_item):
        """Verify the documented opt-out keeps the hold in the real store."""
        item_id = temp_work_item

        with StatusLifecycle(
            item_id, assignee="integration-test", preserve_assignee_on_exit=True
        ):
            pass

        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        after = json.loads(show.stdout)
        assert after["workItem"]["status"] == "completed"
        assert after["workItem"]["assignee"] == "integration-test"

    def test_work_item_not_found(self):
        """Verify RuntimeError for non-existent work item."""
        with pytest.raises(RuntimeError):  # noqa: SIM117
            with StatusLifecycle("NONEXISTENT-999"):
                pass


if __name__ == "__main__":
    pytest.main(sys.argv)
