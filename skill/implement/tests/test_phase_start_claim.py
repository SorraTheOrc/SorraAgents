"""Regression tests for ``implement.py phase_start`` claim semantics.

``phase_start`` must claim a work item with a **status-only** update that
leaves the item's existing workflow stage untouched, and a genuine claim
failure must be surfaced (logged at ERROR and reported as a phase failure)
rather than swallowed behind a warning (SA-0MUY9PMB7001ENMP).

The tests exercise ``phase_start`` with its external boundaries (``wl``,
git, signal handlers, state file, node_modules symlinks) patched, and assert
observable behaviour: the ``StatusLifecycle.update_status`` calls made, the
returned report, and the emitted log records.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

LOG_NAME = "implement.scripts.implement"


def _clean_stash_result() -> dict:
    """An empty, non-orphaned stash inspection result."""
    return {
        "total_stashes": 0,
        "orphaned_stashes": [],
        "matched_stashes": [],
        "has_orphaned": False,
        "warning": None,
    }


@contextlib.contextmanager
def _mocked_phase_start(*, update_status, git_dirty: bool = False, worktree_add: bool = True):
    """Patch every external boundary of ``phase_start``.

    Args:
        update_status: The mock (or callable) installed as the
            ``StatusLifecycle`` class so claim/update calls can be observed.
        git_dirty: When True, the safety gate sees a dirty tree and aborts
            before worktree creation.
        worktree_add: When False, ``git_worktree_add`` fails so the phase
            aborts with a clean-up reset.
    """
    status = "## dev\n M dirty.py\n" if git_dirty else "## dev\n"
    worktree_path = str(Path("/tmp/wt").resolve())
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active", return_value=False)
        )
        stack.enter_context(patch.object(implement, "git_status", return_value=status))
        # Force the main-checkout pre-check to run regardless of the test
        # runner's own cwd (a linked worktree during `implement.py finish`).
        stack.enter_context(
            patch.object(
                implement,
                "_current_checkout_is_linked_worktree",
                return_value=False,
            )
        )
        stack.enter_context(
            patch.object(implement, "git_has_dirty_files", return_value=git_dirty)
        )
        stack.enter_context(
            patch.object(
                implement, "check_orphaned_stashes", return_value=_clean_stash_result()
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "wl_show",
                return_value={
                    "id": "SA-TEST",
                    "title": "Test item",
                    "risk": "Medium",
                    "effort": "Small",
                },
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "_check_and_evaluate_risk_effort",
                return_value={
                    "success": True,
                    "estimates_provided": True,
                    "risk": "Medium",
                    "effort": "Small",
                },
            )
        )
        stack.enter_context(patch.object(implement, "_get_repo_root", return_value="/tmp/repo"))
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "_is_worktree", return_value=False))
        stack.enter_context(patch.object(implement, "git_worktree_add", return_value=worktree_add))
        stack.enter_context(
            patch.object(implement, "_ensure_node_modules_symlink", return_value=True)
        )
        stack.enter_context(
            patch.object(implement, "_ensure_nested_node_modules_symlinks", return_value=0)
        )
        stack.enter_context(patch.object(implement, "_ensure_submodules", return_value=True))
        stack.enter_context(patch.object(implement, "_store_signal_globals"))
        stack.enter_context(patch.object(implement, "_register_signal_handlers"))
        stack.enter_context(patch.object(implement, "write_state"))
        stack.enter_context(patch.object(implement, "wl_add_comment"))
        stack.enter_context(patch.object(implement, "StatusLifecycle", update_status))
        stack.enter_context(
            patch.object(implement, "worktree_path_for", return_value=worktree_path)
        )
        yield


def test_claim_is_status_only_and_leaves_stage_untouched(caplog):
    """A normal start claims once with a status-only update; no stage attempt.

    ``wl`` applies status and stage atomically, so the retired
    ``stage="in_progress"`` made the update fail and the failure was swallowed
    as a warning. The claim must now be status-only and the work item's
    existing stage left unchanged (SA-0MUY9PMB7001ENMP).
    """
    lifecycle = MagicMock()
    with (
        caplog.at_level(logging.DEBUG, logger=LOG_NAME),
        _mocked_phase_start(update_status=lifecycle),
    ):
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is True, report

    # Exactly one lifecycle update — the status-only claim.
    assert lifecycle.update_status.call_count == 1
    args, kwargs = lifecycle.update_status.call_args
    assert args[0] == "SA-TEST"
    assert args[1] in ("in_progress", "in-progress")
    assert "stage" not in kwargs, f"claim must not advance the stage: {kwargs}"

    # No swallowed stage-update warning anywhere in the run.
    assert "Failed to update stage" not in caplog.text


def test_dirty_gate_precedes_claim_and_leaves_item_unclaimed(caplog):
    """The main-checkout pre-check runs BEFORE the claim and is read-only.

    A dirty/foreign main checkout must stop the start *before* the item is
    claimed, so it stays ``open`` with no assignee and no scheduling hold
    (AH-0MWFH7P4D0058L0A AC3). The guard must not write a claim or a reset
    (AC6).
    """
    lifecycle = MagicMock()
    with (
        caplog.at_level(logging.DEBUG, logger=LOG_NAME),
        _mocked_phase_start(update_status=lifecycle, git_dirty=True),
    ):
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is False
    assert report.get("dirty_worktree") is True
    assert "ask the operator" in report["message"]
    # The guard refuses before the claim: nothing was claimed, so nothing is
    # reset either (the item is left open, unassigned).
    assert lifecycle.update_status.call_count == 0, (
        "the pre-claim guard must not claim or reset the item: "
        f"{lifecycle.update_status.call_args_list}"
    )
    assert "Failed to update stage" not in caplog.text


def test_worktree_failure_reset_releases_assignee(caplog):
    """A worktree-creation failure resets to open AND clears the assignee.

    The claim (with its assignee hold) happens before the worktree is
    created; when creation fails the phase must release the item to ``open``
    and clear the assignee so it is never left on a scheduling hold.
    """
    lifecycle = MagicMock()
    with (
        caplog.at_level(logging.DEBUG, logger=LOG_NAME),
        _mocked_phase_start(update_status=lifecycle, worktree_add=False),
    ):
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is False
    assert "Failed to create worktree" in report["message"]

    calls = lifecycle.update_status.call_args_list
    assert calls[0].args == ("SA-TEST", "in_progress")
    # The failure reset releases the assignee hold.
    assert calls[-1].args == ("SA-TEST", "open")
    assert calls[-1].kwargs.get("assignee") == ""


def test_claim_failure_is_reported_and_logged_at_error(caplog):
    """A genuine claim failure is surfaced, not silently swallowed.

    The failure must be logged at ERROR (regardless of json output mode) and
    reported as a phase failure with the underlying ``wl`` error detail
    (SA-0MUY9PMB7001ENMP).
    """
    lifecycle = MagicMock()
    lifecycle.update_status.side_effect = RuntimeError("wl exploded")
    with (
        caplog.at_level(logging.DEBUG, logger=LOG_NAME),
        _mocked_phase_start(update_status=lifecycle),
    ):
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is False
    assert "Failed to claim" in report["message"]
    assert "wl exploded" in report["message"]

    error_messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    ]
    assert any("Failed to claim" in msg for msg in error_messages)
    assert any("wl exploded" in msg for msg in error_messages)


def test_child_lock_lives_in_the_isolated_store_not_the_real_one(
    isolated_implement_child_lock_store,
):
    """The per-child flock is taken in the isolated store, never the repo's.

    ``phase_start`` acquires the real per-work-item ``flock`` before any of
    its mocked steps. Synthetic ids share the repository's ``SA`` prefix, so
    an un-isolated run wrote ``<repo>/.worklog/locks/child-<id>.lock`` into
    the developer's live store; two concurrent pytest processes then raced
    the same lock and one failed with "Work item SA-TEST is held by …"
    (SA-0MVWJ6U3J003TLRZ).
    """
    item_id = "SA-LOCKISOLATION"
    with _mocked_phase_start(update_status=MagicMock()):
        report = implement.phase_start(item_id, json_output=True)

    assert report["success"] is True, report

    lock_name = f"child-{item_id}.lock"
    isolated_lock = isolated_implement_child_lock_store / "locks" / lock_name
    real_lock = REPO_ROOT / ".worklog" / "locks" / lock_name
    assert isolated_lock.is_file(), (
        "phase_start must take its lock in the isolated store "
        f"({isolated_lock})"
    )
    assert not real_lock.exists(), (
        f"phase_start leaked a lock into the live worklog store ({real_lock})"
    )
