"""Regression tests: ``implement.py phase_start`` outbound-blocker gate.

Contract (SA-0MVS1QS9J009DGPT):

- AC1: ``phase_start`` on a leaf whose **own** outbound (depends-on) blockers
  are unmet refuses to start and reports the blocker list — no claim, no
  worktree, no file writes (Assumption A3: the gate sits before the claim so a
  blocked leaf is never left ``in_progress`` with no worktree).
- AC3: unchanged behaviour when the item has no outbound edges or only
  terminal (including soft-deleted) blockers.
- Cross-project edges (a different id prefix) remain coordination notes, not
  gates (the documented SA-0MSQBM2FK005NW1T assumption).

The tests stub only the external boundaries of ``phase_start`` (``wl``, git,
signals, state file) — the same convention as ``test_phase_start_claim.py``
— and assert observable behaviour: the ``StatusLifecycle`` calls made, whether
a worktree was created, and the returned report.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

ITEM_ID = "SA-TEST"


def _clean_stash_result() -> dict:
    return {
        "total_stashes": 0,
        "orphaned_stashes": [],
        "matched_stashes": [],
        "has_orphaned": False,
        "warning": None,
    }


def _blocker(work_item_id: str, status: str = "open") -> dict:
    """A dependency-edge entry as returned by ``wl dep list`` outbound."""
    return {
        "id": work_item_id,
        "title": f"Blocker {work_item_id}",
        "status": status,
        "priority": "high",
        "direction": "depends-on",
    }


@contextlib.contextmanager
def _mocked_phase_start(*, update_status, blockers: list[dict] | None = None):
    """Patch every external boundary of ``phase_start``.

    Args:
        update_status: Mock installed as the ``StatusLifecycle`` class so
            claim/update calls can be observed.
        blockers: The item's outbound ``wl dep list`` edges.
    """
    worktree_path = str(Path("/tmp/wt").resolve())
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active", return_value=False)
        )
        stack.enter_context(
            patch.object(
                implement,
                "wl_dep_blockers",
                side_effect=lambda wid, **_: list(blockers or []),
            )
        )
        stack.enter_context(
            patch.object(implement, "_current_checkout_is_linked_worktree", return_value=False)
        )
        stack.enter_context(patch.object(implement, "git_status", return_value="## dev\n"))
        stack.enter_context(patch.object(implement, "git_has_dirty_files", return_value=False))
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
                    "id": ITEM_ID,
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
                implement, "_sync_parent_branch", return_value={"method": "skipped", "success": True}
            )
        )
        stack.enter_context(patch.object(implement, "_is_worktree", return_value=False))
        stack.enter_context(
            patch.object(implement, "_new_child_lock", side_effect=_permissive_lock)
        )
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


class _AcquiredLock:
    """A per-child lock stub that always grants ownership."""

    token = "test-token"

    def acquire(self):
        return MagicMock(acquired=True, reentrant=False, reclaimed=None, held_by=None, path=None)

    def release(self):
        return None


def _permissive_lock(_work_item_id):
    return _AcquiredLock()


def test_start_refuses_when_a_same_project_blocker_is_unmet():
    """An unmet same-project outbound blocker refuses the start (AC1)."""
    lifecycle = MagicMock()
    with _mocked_phase_start(
        update_status=lifecycle, blockers=[_blocker("SA-DEP", status="in-progress")]
    ):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is False
    assert report["blocked"] is True
    assert report["blocked_by"] == [
        {"id": "SA-DEP", "status": "in-progress", "title": "Blocker SA-DEP"}
    ]
    assert "SA-DEP" in report["message"]

    # No claim, no status write at all: the item is never left in_progress
    # with no worktree (Assumption A3).
    assert lifecycle.update_status.call_count == 0


def test_start_creates_no_worktree_when_blocked():
    """The gate precedes worktree creation (nothing is written)."""
    lifecycle = MagicMock()
    with (
        patch.object(
            implement, "git_worktree_add", return_value=True
        ) as worktree_add,
        _mocked_phase_start(
            update_status=lifecycle, blockers=[_blocker("SA-DEP", status="open")]
        ),
    ):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["blocked"] is True
    worktree_add.assert_not_called()


def test_start_proceeds_when_the_blocker_is_terminal():
    """A terminal blocker does not gate the start (AC3)."""
    lifecycle = MagicMock()
    with _mocked_phase_start(
        update_status=lifecycle, blockers=[_blocker("SA-DONE", status="completed")]
    ):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is True, report
    assert "blocked" not in report
    assert lifecycle.update_status.call_count == 1
    assert lifecycle.update_status.call_args.args == (ITEM_ID, "in_progress")


def test_start_proceeds_when_the_blocker_is_soft_deleted():
    """A soft-deleted blocker is terminal and never gates (AC3)."""
    lifecycle = MagicMock()
    with _mocked_phase_start(
        update_status=lifecycle, blockers=[_blocker("SA-GONE", status="deleted")]
    ):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is True, report
    assert "blocked" not in report


def test_start_reports_cross_project_blocker_as_coordination_note():
    """A cross-project edge is reported but does not refuse the start (AC3)."""
    lifecycle = MagicMock()
    with _mocked_phase_start(
        update_status=lifecycle, blockers=[_blocker("OSL-EXTERNAL", status="open")]
    ):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is True, report
    assert "blocked" not in report
    assert [b["id"] for b in report["coordination_blockers"]] == ["OSL-EXTERNAL"]
    assert lifecycle.update_status.call_count == 1


def test_start_proceeds_when_there_are_no_outbound_edges():
    """No edges → unchanged behaviour (AC3)."""
    lifecycle = MagicMock()
    with _mocked_phase_start(update_status=lifecycle, blockers=[]):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is True, report
    assert lifecycle.update_status.call_count == 1
