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
def _mocked_phase_start(*, update_status, git_dirty: bool = False):
    """Patch every external boundary of ``phase_start``.

    Args:
        update_status: The mock (or callable) installed as the
            ``StatusLifecycle`` class so claim/update calls can be observed.
        git_dirty: When True, the safety gate sees a dirty tree and aborts
            before worktree creation.
    """
    status = "## dev\n M dirty.py\n" if git_dirty else "## dev\n"
    worktree_path = str(Path("/tmp/wt").resolve())
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active", return_value=False)
        )
        stack.enter_context(patch.object(implement, "git_status", return_value=status))
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
        stack.enter_context(patch.object(implement, "git_worktree_add", return_value=True))
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


def test_claim_happens_before_dirty_gate_without_stage(caplog):
    """The claim precedes the dirty-tree gate and stays status-only.

    Even when the safety gate aborts the phase, the item must have been
    claimed (status-only) first, then reset to ``open`` (SA-0MUY9PMB7001ENMP).
    """
    lifecycle = MagicMock()
    with (
        caplog.at_level(logging.DEBUG, logger=LOG_NAME),
        _mocked_phase_start(update_status=lifecycle, git_dirty=True),
    ):
        report = implement.phase_start("SA-TEST", json_output=True)

    assert report["success"] is False
    assert report.get("dirty_worktree") is True

    calls = lifecycle.update_status.call_args_list
    assert calls[0].args == ("SA-TEST", "in_progress")
    assert "stage" not in calls[0].kwargs
    assert calls[1].args == ("SA-TEST", "open")
    assert "Failed to update stage" not in caplog.text


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
