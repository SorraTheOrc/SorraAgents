"""Regression tests: ``phase_finish`` terminal status on the no-op-push path.

The no-diff/no-op-push path (a verification-only item whose worktree branch
has no diff from ``origin/dev``) must persist the terminal transition
(``status=completed``, ``stage=in_review``) exactly like the push-with-diff
path — and, once the store read-back assertion lands, ``finish`` must fail
loudly instead of reporting success when the store does not hold the
terminal transition (OSL-0MVME1PI5005WU58 / OSL-0MVNZPQD4004567J,
OSL-0MVNZPQJZ000EE83).

The tests exercise ``phase_finish`` with its external boundaries (git, the
``wl``/``StatusLifecycle`` store, build/test runners, worktree removal)
patched, following the stub/runner style of
``test_phase_start_claim.py``: observable assertions only — the recorded
lifecycle calls, the volunteered store read-back in the report, and the
returned report.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

#: Stable full commit hash the fake git resolves every revision to.
#: ``phase_finish`` fast-forwards local ``dev`` before the push and then
#: asserts that the pushed commit is an ancestor of ``refs/remotes/origin/dev``
#: (SA-0MVPZG7A0000N66U). Both paths resolve their refs with
#: ``git rev-parse --verify``; modelling a published commit keeps those paths
#: exercised for real instead of tripping the fail-closed assertion, whose own
#: behaviour is covered by
#: ``tests/test_implement_finish_local_dev_verified_sync.py``.
_FAKE_COMMIT = "1" * 40


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    """Stand-in for ``run_cmd``: every git sub-command "succeeds".

    ``phase_finish`` runs a fast-forward block (fetch/checkout/merge) and a
    post-push ancestry assertion against the main checkout; neither must touch
    a real repository in tests. Revision lookups resolve to
    :data:`_FAKE_COMMIT` so both code paths succeed as they would in a healthy
    repository.
    """
    stdout = f"{_FAKE_COMMIT}\n" if len(cmd) > 1 and cmd[1] == "rev-parse" else ""
    return subprocess.CompletedProcess(
        args=cmd, returncode=0, stdout=stdout, stderr=""
    )


def _build_result() -> dict:
    return {
        "success": True,
        "stdout": "no build",
        "stderr": "",
        "exit_code": 0,
        "skipped": True,
    }


def _tests_result() -> dict:
    return {
        "success": True,
        "failures": [],
        "skipped": False,
        "tooling": "suite",
        "scope": "changed",
        "notices": [],
    }


def _cleanup_result() -> dict:
    return {
        "method": "wl_cleanup_worktree",
        "terminated": 0,
        "warning": "",
    }


@contextlib.contextmanager
def _mocked_phase_finish(*, update_status=None, store_show=None, tmp_path=None):
    """Patch every external boundary of ``phase_finish``.

    The real ``StatusLifecycle`` class is kept (so the context manager's
    enter/exit transitions actually run) with only its ``update_status`` and
    ``show`` boundaries patched; the remaining git/worktree/build/test
    boundaries are stubbed.

    Args:
        update_status: A ``MagicMock`` installed as
            ``StatusLifecycle.update_status`` (instance and class calls land
            on the same mock).
        store_show: The dict returned by ``StatusLifecycle.show`` — what the
            store holds when the read-back assertion runs (defaults to a
            terminal store: ``completed``/``in_review``).
        tmp_path: A pytest ``tmp_path`` to use as the worktree directory.
    """
    worktree = str((tmp_path or Path("/tmp/wt-finish-no-diff")).resolve())
    Path(worktree).mkdir(parents=True, exist_ok=True)
    state = implement.ImplementState(
        work_item_id="OSL-TEST",
        worktree_path=worktree,
        repo_root="/tmp/repo-root",
        parent_branch="dev",
    )
    if update_status is None:
        update_status = MagicMock()
    if store_show is None:
        store_show = {"workItem": {"status": "completed", "stage": "in_review"}}
    show = MagicMock(return_value=store_show)

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=worktree)
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(patch.object(implement, "run_build", return_value=_build_result()))
        # ``phase_finish`` runs its gate against the **would-be-commit tree**
        # through ``run_gate_suite`` (temp-index staging,
        # SA-0MVQHVGFR006W8LG) and, once green, stages the change set in the
        # real index through ``stage_tested_tree``. Both act on a real git
        # repository; this harness drives ``phase_finish`` from a bare temp
        # directory, so both boundaries are stubbed green (the gate's own
        # behaviour is covered by ``test_finish_gate_*.py``).
        stack.enter_context(
            patch.object(
                implement,
                "run_gate_suite",
                return_value=(
                    _tests_result(),
                    {
                        "tree": "abc1234",
                        "scope": "changed",
                        "cached": False,
                        "index_unchanged": True,
                    },
                ),
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "stage_tested_tree",
                return_value={
                    "success": True,
                    "tree": "abc1234",
                    "expected": "abc1234",
                },
            )
        )
        stack.enter_context(patch.object(implement, "run_tests", return_value=_tests_result()))
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        stack.enter_context(patch.object(implement, "run_cmd", side_effect=_fake_run_cmd))
        stack.enter_context(
            patch.object(implement, "cleanup_worktree_processes", return_value=_cleanup_result())
        )
        stack.enter_context(patch.object(implement, "remove_state"))
        stack.enter_context(patch.object(implement, "_remove_worktree", return_value=True))
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "wl_add_comment", return_value=True))
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", update_status)
        )
        stack.enter_context(patch.object(implement.StatusLifecycle, "show", show))
        yield {"update_status": update_status, "show": show}


def _terminal_update(update_status: MagicMock) -> tuple | None:
    """Return the ``(id, status)`` pair of the terminal ``completed`` write.

    The real ``StatusLifecycle.__exit__`` calls
    ``self.update_status(id, status="completed", stage="in_review")``
    (keyword ``status``), while the failure-path reset is positional
    (``update_status(id, "open")``) — scan for the keywords to stay
    independent of calling convention.
    """
    for call in update_status.call_args_list:
        if call.kwargs.get("status") == "completed" and call.kwargs.get("stage") == "in_review":
            return (call.args[0], "completed")
    return None


def _reset_update(update_status: MagicMock) -> tuple | None:
    """Return the ``(id, status)`` pair of the failure-path reset to ``open``."""
    for call in update_status.call_args_list:
        if len(call.args) >= 2 and call.args[1] == "open":
            return (call.args[0], "open")
    return None


def test_finish_no_diff_path_persists_terminal_status(tmp_path):
    """A no-op-push finish reaches the terminal transition and reads it back.

    ``git_commit`` sees an empty staged diff (no commit created), the push
    is a no-op success, yet the store must hold ``completed``/``in_review``
    afterwards — the same guarantee as the push-with-diff path
    (OSL-0MVME1PI5005WU58 AC3).
    """
    lifecycle = MagicMock()
    with _mocked_phase_finish(
        update_status=lifecycle, tmp_path=tmp_path
    ) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]

    # The real StatusLifecycle.__exit__ issued the terminal write.
    assert _terminal_update(mocks["update_status"]) == ("OSL-TEST", "completed")

    # The store read-back (the fail-loud guard) saw the terminal state.
    assert report["steps"]["status_readback"]["success"] is True
    assert report["steps"]["status_readback"]["status"] == "completed"
    assert report["steps"]["status_readback"]["stage"] == "in_review"


def test_finish_push_with_diff_path_persists_terminal_status(tmp_path):
    """The push-with-diff path is held to the same guarantee (companion).

    When a real commit was created and pushed, the store read-back must
    equally confirm the terminal transition — the no-op path is not treated
    differently (OSL-0MVME1PI5005WU58 AC5).
    """
    lifecycle = MagicMock()
    with _mocked_phase_finish(update_status=lifecycle, tmp_path=tmp_path) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert _terminal_update(mocks["update_status"]) == ("OSL-TEST", "completed")
    assert report["steps"]["status_readback"]["success"] is True


def test_finish_fails_loudly_when_terminal_status_missing_from_store(tmp_path):
    """Finish never reports success while the store is not terminal.

    If the terminal write is lost or overwritten (the recorded symptom on
    OSL-0MVMB44C9008IXZX: finish reported success while the item was still
    ``open``), the read-back assertion must fail the phase with an actionable
    message and release the item to ``open`` — a silent lifecycle lie is a
    hard failure, never a green report.
    """
    lifecycle = MagicMock()
    non_terminal_store = {
        "workItem": {"status": "open", "stage": "intake_complete"}
    }
    with _mocked_phase_finish(
        update_status=lifecycle, store_show=non_terminal_store, tmp_path=tmp_path
    ) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, (
        "finish must fail loudly when the store does not hold the terminal "
        f"transition; got: {report}"
    )
    assert "does not hold the terminal transition" in report["message"]
    # The item is released to open (StatusLifecycle's reset), not left claimed.
    assert _reset_update(mocks["update_status"]) == ("OSL-TEST", "open")
    # The reset also releases the assignee hold.
    resets = [
        c for c in mocks["update_status"].call_args_list
        if len(c.args) >= 2 and c.args[1] == "open"
    ]
    assert resets and resets[-1].kwargs.get("assignee") == ""


def test_finish_success_clears_assignee(tmp_path):
    """finish releases the assignee hold with the terminal completion.

    A non-empty assignee is a scheduling hold; a successful implement run
    must clear it together with the ``completed``/``in_review`` transition so
    the reviewed item is never left held (assignee-hold convention).
    """
    lifecycle = MagicMock()
    with _mocked_phase_finish(
        update_status=lifecycle, tmp_path=tmp_path
    ) as mocks:
        report = implement.phase_finish("OSL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]

    terminal_calls = [
        c for c in mocks["update_status"].call_args_list
        if c.kwargs.get("status") == "completed"
        and c.kwargs.get("stage") == "in_review"
    ]
    assert terminal_calls, "finish must issue the terminal transition"
    assert terminal_calls[-1].kwargs.get("assignee") == ""
