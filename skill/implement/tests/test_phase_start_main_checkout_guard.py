"""Pre-claim main-checkout guard for ``implement.py phase_start``.

A concurrent session that writes into the main checkout (instead of its own
linked worktree) used to be detected only by the finish gate's live-repo
mutation guard — by which point an unrelated item's finish was blocked
(AH-0MWFH7P4D0058L0A). ``phase_start`` now takes a read-only snapshot of the
main checkout **before the claim** and refuses when the checkout holds foreign
tracked-file changes, so the offending session is stopped proactively and the
item is left ``open`` with no claim and no assignee.

The tests drive ``phase_start`` with its external boundaries (``wl``, git,
extension loading, signal handlers, state file, node_modules) patched, and
assert on observable behaviour: the returned report, the offending path named
in the refusal, and the ``StatusLifecycle`` calls made.
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

PARENT = "AH-GUARDTEST"


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
def _mocked_phase_start(
    *,
    update_status: MagicMock,
    status_output: str,
    description: str = "",
    expected_dirty: tuple[str, ...] = (),
    linked_worktree: bool = False,
):
    """Patch every external boundary of ``phase_start``.

    Args:
        update_status: Installed as the ``StatusLifecycle`` class so claim and
            reset calls can be observed.
        status_output: The porcelain ``git status`` text the guard classifies.
        description: The work item description (Key Files relevance source).
        expected_dirty: Project-declared expected-dirty prefixes.
        linked_worktree: When True, simulate a start invoked from inside a
            linked worktree (the guard is skipped).
    """
    worktree_path = str(Path("/tmp/wt").resolve())
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active", return_value=False)
        )
        stack.enter_context(
            patch.object(implement, "git_status", return_value=status_output)
        )
        stack.enter_context(
            patch.object(
                implement,
                "_current_checkout_is_linked_worktree",
                return_value=linked_worktree,
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "check_orphaned_stashes",
                return_value=_clean_stash_result(),
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "wl_show",
                return_value={
                    "id": PARENT,
                    "title": "Guard test item",
                    "description": description,
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
        stack.enter_context(
            patch.object(implement, "_get_repo_root", return_value="/tmp/repo")
        )
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "_is_worktree", return_value=False))
        stack.enter_context(
            patch.object(implement, "git_worktree_add", return_value=True)
        )
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
        stack.enter_context(patch.object(implement, "load_implement_extension", return_value=None))
        stack.enter_context(
            patch.object(
                implement, "_parse_expected_dirty", return_value=expected_dirty
            )
        )
        stack.enter_context(patch.object(implement, "StatusLifecycle", update_status))
        stack.enter_context(
            patch.object(implement, "worktree_path_for", return_value=worktree_path)
        )
        yield


def test_foreign_mutation_refuses_before_claim_and_names_the_path():
    """A foreign tracked-file mutation overlapping Key Files is refused.

    The guard runs before the claim, names the offending path and leaves the
    item unclaimed (AH-0MWFH7P4D0058L0A AC2/AC3).
    """
    lifecycle = MagicMock()
    description = (
        "## Key Files\n\n"
        "- `skill/implement/scripts/implement.py` — the guard\n"
    )
    with _mocked_phase_start(
        update_status=lifecycle,
        status_output="## dev...dev\n M skill/implement/scripts/implement.py\n",
        description=description,
    ):
        report = implement.phase_start(PARENT, json_output=True)

    assert report["success"] is False
    assert report["dirty_worktree"] is True
    assert report["main_checkout_pre_check"]["verdict"] == "fail"
    assert report["main_checkout_pre_check"]["offending"] == [
        "skill/implement/scripts/implement.py"
    ]
    assert "skill/implement/scripts/implement.py" in report["message"]
    assert "ask the operator" in report["message"]
    # Never claimed, never assigned, no reset write.
    assert lifecycle.update_status.call_count == 0, lifecycle.update_status.call_args_list


def test_foreign_mutation_without_key_files_is_refused_conservatively():
    """With no Key Files section, any tracked-file change is foreign.

    Relevance cannot be judged, so the guard stays conservative and names
    every dirty path (AH-0MWFH7P4D0058L0A AC2).
    """
    lifecycle = MagicMock()
    with _mocked_phase_start(
        update_status=lifecycle,
        status_output="## dev...dev\n M src/mystery.ts\n",
        description="no key files section here",
    ):
        report = implement.phase_start(PARENT, json_output=True)

    assert report["success"] is False
    assert report["dirty_worktree"] is True
    assert report["main_checkout_pre_check"]["verdict"] == "fail"
    assert report["main_checkout_pre_check"]["offending"] == ["src/mystery.ts"]
    assert "src/mystery.ts" in report["message"]
    assert lifecycle.update_status.call_count == 0


def test_clean_main_checkout_proceeds_and_claims():
    """A clean main checkout starts normally and claims the item (AC4)."""
    lifecycle = MagicMock()
    with _mocked_phase_start(
        update_status=lifecycle,
        status_output="## dev...dev\n",
    ):
        report = implement.phase_start(PARENT, json_output=True)

    assert report["success"] is True, report
    assert report["dirty_worktree"] is False
    assert report["main_checkout_pre_check"]["verdict"] == "pass"
    # The item is claimed after the guard passes.
    called = [call.args for call in lifecycle.update_status.call_args_list]
    assert (PARENT, "in_progress") in called, called


def test_expected_dirty_paths_do_not_block_the_start():
    """Project-declared expected-dirty paths proceed (AC5)."""
    lifecycle = MagicMock()
    with _mocked_phase_start(
        update_status=lifecycle,
        status_output="## dev...dev\n M standups/2026-11-14.md\n",
        expected_dirty=("standups/",),
    ):
        report = implement.phase_start(PARENT, json_output=True)

    assert report["success"] is True, report
    assert report["dirty_worktree"] is False
    assert report["dirty_worktree_left_behind"] == []
    assert report["main_checkout_pre_check"]["verdict"] == "pass"


def test_irrelevant_dirty_files_are_left_behind_and_start_proceeds():
    """Dirt the Key Files prove unrelated proceeds without touching it.

    The parent constraint requires the gate not to break an operator's own
    intended main-checkout changes when they do not overlap the work.
    """
    lifecycle = MagicMock()
    description = (
        "## Key Files\n\n"
        "- `skill/implement/scripts/implement.py`\n"
    )
    with _mocked_phase_start(
        update_status=lifecycle,
        status_output="## dev...dev\n M standups/2026-11-14.md\n",
        description=description,
    ):
        report = implement.phase_start(PARENT, json_output=True)

    assert report["success"] is True, report
    assert report["dirty_worktree"] is False
    assert report["dirty_worktree_left_behind"] == ["standups/2026-11-14.md"]
    assert report["main_checkout_pre_check"]["verdict"] == "pass"


def test_snapshot_is_recorded_before_the_claim():
    """The report records the snapshot the guard reasoned over (AC1)."""
    lifecycle = MagicMock()
    description = "## Key Files\n\n- `skill/implement/scripts/implement.py`\n"
    with _mocked_phase_start(
        update_status=lifecycle,
        status_output="## dev...dev\n M skill/implement/scripts/implement.py\n",
        description=description,
    ):
        report = implement.phase_start(PARENT, json_output=True)

    pre_check = report["main_checkout_pre_check"]
    assert pre_check["snapshot_taken_at"]
    assert pre_check["snapshot_status"] == {
        "skill/implement/scripts/implement.py": " M"
    }


def test_guard_is_skipped_inside_a_linked_worktree():
    """Resuming from a linked worktree keeps the historical skip.

    The main checkout is not the surface being written to when the session is
    already inside its own worktree.
    """
    lifecycle = MagicMock()
    with _mocked_phase_start(
        update_status=lifecycle,
        status_output="## dev...dev\n M skill/implement/scripts/implement.py\n",
        linked_worktree=True,
    ):
        report = implement.phase_start(PARENT, json_output=True)

    assert report["success"] is True, report
    assert report["main_checkout_pre_check"]["verdict"] == "skipped"
    assert report["dirty_worktree"] is False
