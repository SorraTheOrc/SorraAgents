"""Deferred worktree removal in ``phase_finish`` (SA-0MVTE8XAJ002IUDR).

``phase_finish`` used to delete the worktree as soon as the dev push landed.
A session that owns the worktree as its cwd (a driven child session, or any
operator session that opted in with ``--keep-worktree``) then lost its
working directory while still running — Pi's bash tool refuses every later
command with ``Working directory does not exist`` — exactly when the session
must render its report and verify the pushed commit.

The contract pinned here:

- **AC1 — driven finish defers removal.** With ``IMPLEMENT_DRIVE_ACTIVE=1``
  the finish phase neither cleans up worktree processes nor removes the
  worktree; the tree, its branch and ``.implement_state.json`` survive, the
  report carries ``steps.worktree_removal.deferred == true`` with a stable
  reason, and finish still reaches its terminal state.
- **AC2 — default removal unchanged.** Without the switches, finish still runs
  ``cleanup_worktree_processes`` + ``_remove_worktree`` and reports
  ``worktree_removal.deferred == false`` / ``worktree_removed == true``.
- **AC3 — explicit opt-in only.** ``finish --keep-worktree`` and
  ``IMPLEMENT_KEEP_WORKTREE=1`` each defer in an otherwise plain session, and
  combining them with ``IMPLEMENT_DRIVE_ACTIVE=1`` stays deferred
  (idempotent), with a stable precedence between the reasons.
- **AC4 — the signal handler honours deferral.** ``_handle_signal_cleanup``
  still releases the item but performs no worktree cleanup/removal when
  removal is deferred; the non-driven path still cleans up.
- **AC12 — ``abort`` is unchanged.** ``phase_abort`` keeps removing the
  worktree unconditionally, even under both switches (an aborted item is
  abandoned, not resumed).

The tests exercise ``phase_finish``/``phase_abort``/``_handle_signal_cleanup``
with their external boundaries (git, the ``wl`` store, build/test runners,
process cleanup and worktree removal) patched — the same patched-boundary
style as ``test_finish_no_diff_status.py`` — and assert observable behaviour
only: recorded calls, report fields, and the on-disk worktree/state file.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

#: Stable full commit hash the fake git resolves every revision to (the
#: fast-forward and the post-push ancestry assertion both ``rev-parse``).
_FAKE_COMMIT = "1" * 40

ITEM_ID = "SA-DEFER"


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    """Stand-in for ``run_cmd``: every git sub-command "succeeds"."""
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


def _gate_tree() -> dict:
    """The would-be-commit summary ``run_gate_suite`` records on the report."""
    return {
        "tree": "abc1234",
        "scope": "changed",
        "cached": False,
        "index_unchanged": True,
    }


def _cleanup_result() -> dict:
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


@contextlib.contextmanager
def _mocked_phase_finish(tmp_path: Path):
    """Patch every external boundary of ``phase_finish``.

    The real ``remove_state`` runs (it only unlinks the state file) so the
    tests observe whether the state file survives; ``_remove_worktree`` and
    ``cleanup_worktree_processes`` are mocked so the assertions can count the
    removal attempts without destroying the fixture directory.
    """
    worktree = str(tmp_path)
    state = implement.ImplementState(
        work_item_id=ITEM_ID,
        worktree_path=worktree,
        repo_root="/tmp/repo-root",
        parent_branch="dev",
    )
    state_file = tmp_path / implement.STATE_FILE_NAME
    state_file.write_text("{}")
    comments: list[str] = []

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=worktree)
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(
            patch.object(implement, "run_build", return_value=_build_result())
        )
        stack.enter_context(
            patch.object(
                implement,
                "run_gate_suite",
                return_value=(_tests_result(), _gate_tree()),
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
        stack.enter_context(
            patch.object(implement, "run_tests", return_value=_tests_result())
        )
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        stack.enter_context(patch.object(implement, "run_cmd", side_effect=_fake_run_cmd))
        cleanup = stack.enter_context(
            patch.object(
                implement, "cleanup_worktree_processes", return_value=_cleanup_result()
            )
        )
        removal = stack.enter_context(
            patch.object(implement, "_remove_worktree", return_value=True)
        )
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "wl_add_comment",
                side_effect=lambda wid, text: comments.append(text) or True,
            )
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", MagicMock())
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle,
                "show",
                MagicMock(
                    return_value={
                        "workItem": {"status": "completed", "stage": "in_review"}
                    }
                ),
            )
        )
        yield {
            "worktree": tmp_path,
            "state_file": state_file,
            "cleanup": cleanup,
            "removal": removal,
            "comments": comments,
        }


@pytest.fixture(autouse=True)
def _clean_deferral_env(monkeypatch):
    """Neutralise ambient deferral switches from the host environment.

    When the full suite runs inside a driven child session (``finish`` and the
    pre-push hook do exactly that) ``IMPLEMENT_DRIVE_ACTIVE=1`` is inherited;
    deleting it keeps the default-removal tests hermetic. Tests that need a
    switch set it explicitly.
    """
    monkeypatch.delenv(implement.DRIVE_RECURSION_ENV, raising=False)
    monkeypatch.delenv(implement.KEEP_WORKTREE_ENV, raising=False)


# ---------------------------------------------------------------------------
# AC1 — a driven finish defers removal
# ---------------------------------------------------------------------------


def test_driven_finish_defers_removal(tmp_path, monkeypatch):
    """``IMPLEMENT_DRIVE_ACTIVE=1`` keeps the tree, the state and the branch."""
    monkeypatch.setenv(implement.DRIVE_RECURSION_ENV, "1")

    with _mocked_phase_finish(tmp_path) as mocks:
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert mocks["cleanup"].call_count == 0, "process cleanup must be skipped"
    assert mocks["removal"].call_count == 0, "worktree removal must be skipped"
    assert mocks["worktree"].is_dir(), "the session's cwd must survive finish"
    assert mocks["state_file"].exists(), ".implement_state.json must survive"

    removal = report["steps"]["worktree_removal"]
    assert removal["deferred"] is True
    assert removal["reason"] == "driven-session"
    assert removal["worktree_path"] == str(tmp_path)
    assert report["steps"]["worktree_removed"] is False


def test_driven_finish_still_reaches_terminal_state(tmp_path, monkeypatch):
    """Deferral must not disturb the push/terminal-status machinery."""
    monkeypatch.setenv(implement.DRIVE_RECURSION_ENV, "1")

    with _mocked_phase_finish(tmp_path):
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert report["steps"]["push"]["success"] is True
    assert report["steps"]["status_readback"]["success"] is True
    assert report["steps"]["status_readback"]["status"] == "completed"


def test_deferred_finish_names_the_retained_worktree(tmp_path, monkeypatch):
    """The deferral is loud: the comment names the path and its remedy.

    A silent "we skipped cleanup" is worse than none — the retained tree must
    be discoverable from the work item and removable with a copy-pasteable
    command.
    """
    monkeypatch.setenv(implement.DRIVE_RECURSION_ENV, "1")

    with _mocked_phase_finish(tmp_path) as mocks:
        implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    completion = "\n".join(mocks["comments"])
    assert str(tmp_path) in completion
    assert "git worktree remove" in completion
    assert "git worktree prune" in completion


# ---------------------------------------------------------------------------
# AC2 — default removal is unchanged
# ---------------------------------------------------------------------------


def test_default_finish_still_removes_the_worktree(tmp_path):
    """Without any switch, finish cleans up and removes exactly as before."""
    with _mocked_phase_finish(tmp_path) as mocks:
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert mocks["cleanup"].call_count == 1
    assert mocks["removal"].call_count == 1
    assert str(mocks["worktree"]) in str(mocks["removal"].call_args.args[0])
    assert not mocks["state_file"].exists(), "the state file goes with the tree"

    removal = report["steps"]["worktree_removal"]
    assert removal["deferred"] is False
    assert report["steps"]["worktree_removed"] is True


# ---------------------------------------------------------------------------
# AC3 — explicit opt-in switch, with stable reasons
# ---------------------------------------------------------------------------


def test_keep_worktree_flag_defers_removal(tmp_path):
    """``finish --keep-worktree`` defers removal in a plain session."""
    with _mocked_phase_finish(tmp_path) as mocks:
        report = implement.phase_finish(
            ITEM_ID, json_output=True, no_refactor=True, keep_worktree=True
        )

    assert mocks["cleanup"].call_count == 0
    assert mocks["removal"].call_count == 0
    assert mocks["worktree"].is_dir()
    assert mocks["state_file"].exists()
    assert report["steps"]["worktree_removal"] == {
        "deferred": True,
        "reason": "keep-worktree-flag",
        "worktree_path": str(tmp_path),
    }
    assert report["steps"]["worktree_removed"] is False


def test_keep_worktree_env_defers_removal(tmp_path, monkeypatch):
    """``IMPLEMENT_KEEP_WORKTREE=1`` defers removal in a plain session."""
    monkeypatch.setenv(implement.KEEP_WORKTREE_ENV, "1")

    with _mocked_phase_finish(tmp_path) as mocks:
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert mocks["cleanup"].call_count == 0
    assert mocks["removal"].call_count == 0
    assert report["steps"]["worktree_removal"]["reason"] == "keep-worktree-env"


def test_flag_and_env_together_are_idempotent(tmp_path, monkeypatch):
    """Both opt-ins at once defer once — no double bookkeeping, no removal."""
    monkeypatch.setenv(implement.KEEP_WORKTREE_ENV, "1")

    with _mocked_phase_finish(tmp_path) as mocks:
        report = implement.phase_finish(
            ITEM_ID, json_output=True, no_refactor=True, keep_worktree=True
        )

    assert mocks["removal"].call_count == 0
    assert report["steps"]["worktree_removal"]["deferred"] is True


def test_driven_reason_wins_over_the_explicit_hatch(tmp_path, monkeypatch):
    """``IMPLEMENT_DRIVE_ACTIVE`` is reported as the primary reason.

    The reason is a stable token consumed by tests and the drive report; the
    driven marker explains the deferral even when an operator also passed
    ``--keep-worktree``.
    """
    monkeypatch.setenv(implement.DRIVE_RECURSION_ENV, "1")
    monkeypatch.setenv(implement.KEEP_WORKTREE_ENV, "1")

    with _mocked_phase_finish(tmp_path):
        report = implement.phase_finish(
            ITEM_ID, json_output=True, no_refactor=True, keep_worktree=True
        )

    assert report["steps"]["worktree_removal"]["reason"] == "driven-session"


@pytest.mark.parametrize("value", ["0", "", "false"])
def test_non_one_values_do_not_defer(tmp_path, monkeypatch, value):
    """Only the ``"1"`` convention defers (same semantics as the drive guard)."""
    monkeypatch.setenv(implement.KEEP_WORKTREE_ENV, value)

    with _mocked_phase_finish(tmp_path) as mocks:
        report = implement.phase_finish(ITEM_ID, json_output=True, no_refactor=True)

    assert mocks["removal"].call_count == 1
    assert report["steps"]["worktree_removal"]["deferred"] is False


# ---------------------------------------------------------------------------
# AC4 — the signal handler honours the deferral
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _signal_globals(worktree: Path, work_item_id: str = ITEM_ID):
    """Install the signal-handler globals and patch their side effects."""
    patcher = patch.object(implement, "_restore_repo_state")
    lifecycle = MagicMock(
        show=MagicMock(return_value={"workItem": {"status": "in_progress"}}),
        update_status=MagicMock(),
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(patcher)
        stack.enter_context(patch.object(implement, "StatusLifecycle", lifecycle))
        cleanup = stack.enter_context(
            patch.object(implement, "cleanup_worktree_processes")
        )
        removal = stack.enter_context(
            patch.object(implement, "_remove_worktree")
        )
        stack.enter_context(
            patch.object(implement, "_release_child_lock")
        )
        patchers = [
            patch.object(implement, "_worktree_path_global", str(worktree)),
            patch.object(implement, "_work_item_id_global", work_item_id),
            patch.object(implement, "_repo_root_global", "/tmp/repo-root"),
            patch.object(implement, "_child_lock_global", None),
        ]
        for p in patchers:
            stack.enter_context(p)
        yield {
            "cleanup": cleanup,
            "removal": removal,
            "lifecycle": lifecycle,
        }


def test_signal_cleanup_defers_under_driven_marker(tmp_path, monkeypatch):
    """SIGTERM during a driven finish releases the item but keeps the tree."""
    monkeypatch.setenv(implement.DRIVE_RECURSION_ENV, "1")

    with _signal_globals(tmp_path) as mocks:
        implement._handle_signal_cleanup(signal.SIGTERM)

    assert mocks["cleanup"].call_count == 0
    assert mocks["removal"].call_count == 0
    assert mocks["lifecycle"].update_status.call_args.args[1] == "open"


def test_signal_cleanup_defers_under_keep_worktree(tmp_path, monkeypatch):
    """SIGTERM with ``--keep-worktree`` pressed keeps the tree too."""
    monkeypatch.setattr(implement, "_keep_worktree_global", True, raising=False)

    with _signal_globals(tmp_path) as mocks:
        implement._handle_signal_cleanup(signal.SIGINT)

    assert mocks["cleanup"].call_count == 0
    assert mocks["removal"].call_count == 0
    assert mocks["lifecycle"].update_status.call_args.args[1] == "open"


def test_signal_cleanup_still_cleans_up_without_deferral(tmp_path):
    """The plain (non-deferred) interrupt path is unchanged."""
    with _signal_globals(tmp_path) as mocks:
        implement._handle_signal_cleanup(signal.SIGTERM)

    assert mocks["cleanup"].call_count == 1
    assert mocks["removal"].call_count == 1
    assert mocks["lifecycle"].update_status.call_args.args[1] == "open"


def test_signal_cleanup_restores_keep_worktree_global(tmp_path, monkeypatch):
    """The module flag defaults to False so an ordinary session is unaffected."""
    monkeypatch.setattr(implement, "_keep_worktree_global", False, raising=False)

    with _signal_globals(tmp_path) as mocks:
        implement._handle_signal_cleanup(signal.SIGTERM)

    assert mocks["removal"].call_count == 1


# ---------------------------------------------------------------------------
# AC12 — abort keeps removing the worktree unconditionally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("drive_active", "keep_worktree", "keep_env"),
    [
        ("1", False, False),
        (None, True, False),
        ("1", True, True),
    ],
)
def test_abort_still_removes_the_worktree(
    tmp_path, monkeypatch, drive_active, keep_worktree, keep_env
):
    """An aborted item is abandoned, not resumed — abort never defers."""
    if drive_active:
        monkeypatch.setenv(implement.DRIVE_RECURSION_ENV, drive_active)
    if keep_env:
        monkeypatch.setenv(implement.KEEP_WORKTREE_ENV, "1")
    if keep_worktree:
        monkeypatch.setattr(implement, "_keep_worktree_global", True, raising=False)

    state = implement.ImplementState(
        work_item_id=ITEM_ID,
        worktree_path=str(tmp_path),
        repo_root="/tmp/repo-root",
        parent_branch="dev",
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=str(tmp_path))
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", MagicMock())
        )
        stack.enter_context(patch.object(implement, "wl_add_comment", return_value=True))
        stack.enter_context(patch.object(implement, "_mark_pane_aborted"))
        stack.enter_context(patch.object(implement, "_restore_repo_state"))
        cleanup = stack.enter_context(
            patch.object(implement, "cleanup_worktree_processes")
        )
        removal = stack.enter_context(patch.object(implement, "_remove_worktree"))

        report = implement.phase_abort(ITEM_ID, json_output=True)

    assert report["success"] is True
    assert cleanup.call_count == 1, "abort always cleans up"
    assert removal.call_count == 1, "abort always removes the worktree"
