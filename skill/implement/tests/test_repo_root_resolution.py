"""Regression tests: ``repo_root`` always means the MAIN checkout.

``phase_start`` used to recompute ``repo_root`` from ``Path.cwd()`` immediately
before writing ``.implement_state.json``. On the normal driven-child pattern
— ``start`` resumed from inside an existing worktree — ``cwd`` *is* the
worktree, so the state file recorded the worktree as the repository root.
Every downstream consumer of that value was then wrong: ``phase_finish`` could
not fast-forward ``dev`` in the main checkout, and its worktree-removal gate
refused with ``REFUSE to remove the repository root``, stranding a successfully
pushed implementation with the work item reset to ``open``
(MS-0MVM7NJF70037YU4).

These tests build a **real** temporary git repository plus a linked worktree
and never mock ``_get_repo_root`` — mocking it is exactly why the defect
escaped the existing ``test_phase_start_claim.py`` coverage. Assertions are on
observable behaviour only: the persisted state file, the value handed to
``_store_signal_globals``, the ``repo_root`` handed to ``_remove_worktree``,
and the checkout restored by ``phase_abort``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
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

LOG_NAME = "implement.scripts.implement"

WORK_ITEM = "SA-TESTR00T01"
TITLE = "Repo root resolution regression"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Implement Tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "Implement Tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}

#: ``_remove_worktree`` calls recorded by the finish harness.
_gate_calls: list[tuple[str, str | None]] = []


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real git command in *cwd* with a hermetic identity."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def _worktree_path(main: Path) -> Path:
    """The canonical worktree path ``phase_start`` computes for this item."""
    slug = implement.slug_from_title(TITLE)
    return main / implement.DEFAULT_WORKTREE_DIR / f"wl-{WORK_ITEM}-{slug}"


@pytest.fixture()
def main_repo(tmp_path: Path) -> Path:
    """A real git repo on ``dev`` with a clean working tree."""
    main = tmp_path / "main-checkout"
    main.mkdir()
    _git(main, "init", "--quiet")
    _git(main, "checkout", "--quiet", "-b", "dev")
    # Worklog bookkeeping is gitignored in real repos; mirror that so the
    # main checkout stays genuinely clean for the safety gates.
    (main / ".gitignore").write_text(".worklog/\n")
    (main / "README.md").write_text("main checkout\n")
    _git(main, "add", ".gitignore", "README.md")
    _git(main, "commit", "--quiet", "-m", "init")
    return main


@pytest.fixture()
def repo_with_worktree(main_repo: Path) -> tuple[Path, Path]:
    """The main repo plus a linked worktree for the test item."""
    worktree = _worktree_path(main_repo)
    worktree.parent.mkdir(parents=True)
    _git(
        main_repo,
        "worktree",
        "add",
        "--quiet",
        "-b",
        f"wl-{WORK_ITEM}-{implement.slug_from_title(TITLE)}",
        str(worktree),
        "dev",
    )
    return main_repo, worktree


@contextlib.contextmanager
def _mocked_phase_start(worktree_path: str | None = None):
    """Patch ``phase_start``'s external boundaries.

    ``_get_repo_root``, ``write_state`` and ``_store_signal_globals`` are left
    real on purpose: the subject of these tests is the value they receive.
    """
    lifecycle = MagicMock()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active", return_value=False)
        )
        stack.enter_context(
            patch.object(
                implement,
                "wl_show",
                return_value={
                    "id": WORK_ITEM,
                    "title": TITLE,
                    "description": "## Key Files\n\n- `README.md`\n",
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
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "_register_signal_handlers"))
        stack.enter_context(
            patch.object(implement, "wl_add_comment", return_value=True)
        )
        stack.enter_context(patch.object(implement, "StatusLifecycle", lifecycle))
        if worktree_path is not None:
            # A resume passes an absolute path; the real helper returns a
            # cwd-relative one, which would resolve inside the worktree.
            stack.enter_context(
                patch.object(
                    implement, "worktree_path_for", return_value=worktree_path
                )
            )
        yield lifecycle


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
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


def _gate_replica(worktree_path: str, repo_root: str | None = None) -> bool:
    """Mirror ``_remove_worktree``'s safety gate 1 for ``phase_finish``.

    The real gate refuses when the target resolves to ``repo_root``. Replicating
    it as a side effect keeps the finish path honest (a wrong ``repo_root``
    raises ``RuntimeError`` exactly as it does live) without removing a real
    worktree during the test.
    """
    _gate_calls.append((worktree_path, repo_root))
    if repo_root and Path(worktree_path).resolve() == Path(repo_root).resolve():
        raise RuntimeError(
            f"REFUSE to remove the repository root: {worktree_path}"
        )
    return True


@contextlib.contextmanager
def _mocked_phase_finish(worktree_path: Path):
    """Patch ``phase_finish``'s external boundaries (git work, build, push)."""
    terminal_store = {"workItem": {"status": "completed", "stage": "in_review"}}
    _gate_calls.clear()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                implement, "_discover_worktree", return_value=str(worktree_path)
            )
        )
        stack.enter_context(
            patch.object(
                implement, "_worktree_placement_violation", return_value=None
            )
        )
        stack.enter_context(
            patch.object(implement, "run_build", return_value=_build_result())
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
        stack.enter_context(
            patch.object(
                implement,
                "cleanup_worktree_processes",
                return_value=_cleanup_result(),
            )
        )
        stack.enter_context(
            patch.object(implement, "_remove_worktree", side_effect=_gate_replica)
        )
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(
            patch.object(implement, "wl_add_comment", return_value=True)
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", MagicMock())
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle,
                "show",
                MagicMock(return_value=terminal_store),
            )
        )
        yield


@contextlib.contextmanager
def _mocked_phase_abort():
    """Patch ``phase_abort``'s external boundaries; observe the restore target."""
    restore = MagicMock()
    remove = MagicMock(return_value=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(implement, "StatusLifecycle", MagicMock()))
        stack.enter_context(patch.object(implement, "_mark_pane_aborted"))
        stack.enter_context(
            patch.object(implement, "wl_add_comment", return_value=True)
        )
        stack.enter_context(
            patch.object(
                implement,
                "cleanup_worktree_processes",
                MagicMock(return_value=_cleanup_result()),
            )
        )
        stack.enter_context(patch.object(implement, "_remove_worktree", remove))
        stack.enter_context(patch.object(implement, "_restore_repo_state", restore))
        yield {"restore": restore, "remove": remove}


def test_resume_from_worktree_records_main_checkout_as_repo_root(
    repo_with_worktree, monkeypatch
):
    """Resuming ``start`` inside the worktree persists the main checkout root.

    The same value reaches ``_store_signal_globals``, so the signal handler's
    ``_restore_repo_state`` also runs in the main checkout (AC1).
    """
    main, worktree = repo_with_worktree
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(implement, "_repo_root_global", None)

    with _mocked_phase_start(worktree_path=str(worktree)):
        report = implement.phase_start(WORK_ITEM, json_output=True)

    assert report["success"] is True, report
    assert report["resumed"] is True

    state = json.loads((worktree / implement.STATE_FILE_NAME).read_text())
    assert Path(state["repo_root"]).resolve() == main.resolve()
    assert Path(state["repo_root"]).resolve() != worktree.resolve()

    assert implement._repo_root_global is not None
    assert Path(implement._repo_root_global).resolve() == main.resolve()


def test_fresh_start_from_main_checkout_records_main_checkout_as_repo_root(
    main_repo, monkeypatch
):
    """A fresh ``start`` from the main checkout keeps the same invariant (AC1)."""
    main = main_repo
    worktree = _worktree_path(main)
    monkeypatch.chdir(main)
    monkeypatch.setattr(implement, "_repo_root_global", None)

    with _mocked_phase_start():
        report = implement.phase_start(WORK_ITEM, json_output=True)

    assert report["success"] is True, report
    assert report["resumed"] is False
    assert worktree.exists()

    state = json.loads((worktree / implement.STATE_FILE_NAME).read_text())
    assert Path(state["repo_root"]).resolve() == main.resolve()
    assert Path(implement._repo_root_global).resolve() == main.resolve()


def test_finish_rejects_stale_state_repo_root_and_removes_worktree(
    repo_with_worktree, caplog
):
    """A state file naming the worktree as ``repo_root`` no longer strands it.

    ``phase_finish`` must treat that value as stale, derive the main checkout
    root from the worktree (logging both values at WARNING) and hand the
    correct root to ``_remove_worktree`` — never the worktree itself (AC2).
    """
    main, worktree = repo_with_worktree
    implement.write_state(
        implement.ImplementState(
            work_item_id=WORK_ITEM,
            worktree_path=str(worktree),
            repo_root=str(worktree),  # the stale value written by the defect
            parent_branch="dev",
        ),
        str(worktree),
    )

    with (
        caplog.at_level(logging.WARNING, logger=LOG_NAME),
        _mocked_phase_finish(worktree),
    ):
        report = implement.phase_finish(
            WORK_ITEM, json_output=True, no_refactor=True
        )

    assert report["success"] is True, report["message"]

    assert len(_gate_calls) == 1, _gate_calls
    removed_path, removed_root = _gate_calls[0]
    assert Path(removed_path).resolve() == worktree.resolve()
    assert removed_root is not None
    assert Path(removed_root).resolve() == main.resolve()
    assert Path(removed_root).resolve() != worktree.resolve()

    # The rejected value and the derived value are both diagnosable in logs.
    assert str(worktree) in caplog.text
    assert str(main) in caplog.text
    assert "stale" in caplog.text.lower()


def test_remove_worktree_still_refuses_the_main_checkout(repo_with_worktree):
    """The safety gate keeps refusing the true main checkout (AC5).

    The fix supplies the correct root; it must not relax the gate.
    """
    main, _worktree = repo_with_worktree

    with pytest.raises(RuntimeError, match="REFUSE to remove the repository root"):
        implement._remove_worktree(str(main), repo_root=str(main))


def test_abort_from_inside_worktree_restores_main_checkout(
    repo_with_worktree, monkeypatch, caplog
):
    """``phase_abort`` restores the MAIN checkout when run from a worktree (AC3).

    The stale state value (the worktree) must not be used for the
    ``git checkout`` / ``git pull`` restore.
    """
    main, worktree = repo_with_worktree
    implement.write_state(
        implement.ImplementState(
            work_item_id=WORK_ITEM,
            worktree_path=str(worktree),
            repo_root=str(worktree),  # stale value
            parent_branch="dev",
        ),
        str(worktree),
    )
    monkeypatch.chdir(worktree)

    with (
        caplog.at_level(logging.WARNING, logger=LOG_NAME),
        _mocked_phase_abort() as mocks,
    ):
        report = implement.phase_abort(WORK_ITEM, json_output=True)

    assert report["success"] is True, report
    mocks["restore"].assert_called_once()
    restored_root = mocks["restore"].call_args.args[0]
    assert Path(restored_root).resolve() == main.resolve()
    assert Path(restored_root).resolve() != worktree.resolve()
    assert "stale" in caplog.text.lower()


def test_abort_from_main_checkout_restores_main_checkout(
    repo_with_worktree, monkeypatch
):
    """Aborting from the main checkout behaves identically (AC3).

    Restoring the main checkout from the main checkout is the pre-existing
    behaviour and must not change.
    """
    main, _worktree = repo_with_worktree
    monkeypatch.chdir(main)

    with _mocked_phase_abort() as mocks:
        report = implement.phase_abort(WORK_ITEM, json_output=True)

    assert report["success"] is True, report
    mocks["restore"].assert_called_once()
    restored_root = mocks["restore"].call_args.args[0]
    assert Path(restored_root).resolve() == main.resolve()
