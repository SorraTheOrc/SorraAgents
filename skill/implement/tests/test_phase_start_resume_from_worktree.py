"""Regression tests: ``phase_start`` resolves the default worktree path
against the main checkout root, never against ``cwd``.

When ``implement.py start <id>`` is run from **inside** the item's existing
worktree (a driven child resuming the standard leaf flow, or a human
resuming a pre-created tree) the resume path must work: the worktree is
detected, ``resumed: true`` is returned, no nested
``<worktree>/.worklog/worktrees/…`` path is created, and
``git_worktree_add`` is never called (SA-0MVXUGXS5006M3HF AC1/AC5).

Root cause: ``worktree_path_for()`` returns a path relative to the main
checkout (``.worklog/worktrees/wl-<id>-<slug>``) but ``phase_start`` resolved
it with ``Path(wt_path).resolve()`` — i.e. against **cwd**.  From inside the
worktree that produced ``<worktree>/.worklog/worktrees/…`` (non-existent), so
``_is_worktree()`` returned ``False`` and ``git worktree add`` was attempted
at the nested path and failed.

These tests deliberately **do not mock ``_get_repo_root``** so the real
main-checkout resolution is exercised (AC5); mocking it is exactly why the
defect escaped the earlier coverage (see ``test_repo_root_resolution.py``).
"""

from __future__ import annotations

import contextlib
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

ITEM_ID = "SA-RESUMEWT"
TITLE = "Test item"

LOG_NAME = "implement.scripts.implement"


def _empty_stash_result() -> dict:
    return {
        "total_stashes": 0,
        "orphaned_stashes": [],
        "matched_stashes": [],
        "has_orphaned": False,
        "warning": None,
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal, initialised git repository and return its root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Test repo\n")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _expected_rel_wt() -> str:
    """The repo-root-relative worktree path ``phase_start`` will compute."""
    return implement.worktree_path_for(ITEM_ID, implement.slug_from_title(TITLE))


def _make_worktree(repo: Path) -> Path:
    """Create the canonical worktree under *repo* and return its path."""
    rel = _expected_rel_wt()
    wt = repo / rel
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", implement.branch_name_for(
        ITEM_ID, implement.slug_from_title(TITLE)), str(wt), "HEAD")
    return wt


@contextlib.contextmanager
def _patch_externals(*, git_worktree_add: MagicMock):
    """Patch ``phase_start``'s external boundaries **except** repo-root logic.

    ``_get_repo_root``, ``worktree_path_for``, ``slug_from_title`` and
    ``_is_worktree`` are deliberately left unpatched so the real
    main-checkout resolution runs (AC5).
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "is_code_freeze_active",
                         return_value=False))
        stack.enter_context(
            patch.object(implement, "git_status", return_value="## dev\n"))
        stack.enter_context(
            patch.object(implement, "git_has_dirty_files",
                         return_value=False))
        stack.enter_context(
            patch.object(implement, "check_orphaned_stashes",
                         return_value=_empty_stash_result()))
        stack.enter_context(
            patch.object(implement, "wl_show", return_value={
                "id": ITEM_ID,
                "title": TITLE,
                "risk": "Low",
                "effort": "S",
            }))
        stack.enter_context(
            patch.object(implement, "_check_and_evaluate_risk_effort",
                         return_value={"success": True,
                                       "estimates_provided": True,
                                       "risk": "Low", "effort": "S"}))
        stack.enter_context(
            patch.object(implement, "_sync_parent_branch_locked",
                         return_value={"method": "skipped", "success": True}))
        stack.enter_context(
            patch.object(implement, "warn_self_referential_upstream",
                         return_value={"status": "ok", "defect_count": 0}))
        stack.enter_context(
            patch.object(implement, "git_worktree_add",
                         new=git_worktree_add))
        stack.enter_context(
            patch.object(implement, "_prepare_node_modules", return_value={
                "link_created": False,
                "shared_resolution_ok": True,
                "shared_resolution": {
                    "applicable": False,
                    "ok": True,
                    "resolved_root": None,
                },
            }))
        stack.enter_context(
            patch.object(implement, "_ensure_nested_node_modules_symlinks",
                         return_value=0))
        stack.enter_context(
            patch.object(implement, "_ensure_submodules",
                         return_value=True))
        stack.enter_context(
            patch.object(implement, "_store_signal_globals"))
        stack.enter_context(
            patch.object(implement, "_register_signal_handlers"))
        stack.enter_context(
            patch.object(implement, "write_state"))
        stack.enter_context(
            patch.object(implement, "wl_add_comment"))
        stack.enter_context(
            patch.object(implement, "StatusLifecycle", MagicMock()))
        stack.enter_context(
            patch.object(implement, "_parse_expected_dirty",
                         return_value=set()))
        stack.enter_context(
            patch.object(implement, "load_implement_extension",
                         return_value=None))
        yield


def test_resume_from_inside_worktree(git_repo, monkeypatch):
    """AC1/AC5: invoked from inside the worktree, ``start`` resumes it.

    The default path must resolve against the main checkout root, so the
    registered worktree is detected (``resumed: true``), no nested tree is
    created, and ``git_worktree_add`` is never called.
    """
    wt_path = _make_worktree(git_repo)
    nested = wt_path / _expected_rel_wt()
    mock_add = MagicMock(return_value=True)

    monkeypatch.chdir(wt_path)
    with _patch_externals(git_worktree_add=mock_add):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is True, report
    assert report.get("resumed") is True, (
        f"expected resumed=True when invoked from inside the worktree, "
        f"got {report.get('resumed')}"
    )
    assert report["worktree_path"] == str(wt_path.resolve()), report
    mock_add.assert_not_called()
    assert not nested.exists(), (
        f"a nested worktree path was created at {nested} — the default path "
        f"was resolved against cwd instead of the main checkout root"
    )
    # The work item must have been claimed and left in_progress.
    assert report["success"] is True


def test_main_checkout_creates_canonical_worktree(git_repo, monkeypatch):
    """AC3: from the main checkout the worktree is created at the canonical path."""
    mock_add = MagicMock(return_value=True)

    monkeypatch.chdir(git_repo)
    with _patch_externals(git_worktree_add=mock_add):
        report = implement.phase_start(ITEM_ID, json_output=True)

    assert report["success"] is True, report
    assert report.get("resumed") is False, report
    expected = str((git_repo / _expected_rel_wt()).resolve())
    mock_add.assert_called_once()
    called_args = mock_add.call_args.args
    # git_worktree_add(branch, path, parent_branch)
    assert called_args[1] == expected, (
        f"git_worktree_add received {called_args[1]!r}, expected {expected!r}"
    )
    assert report["worktree_path"] == expected, report


def test_relative_override_resolves_against_cwd(git_repo, monkeypatch):
    """AC4: a relative ``--worktree-path`` keeps resolving against cwd."""
    subdir = git_repo / "nested"
    subdir.mkdir()
    mock_add = MagicMock(return_value=True)

    monkeypatch.chdir(subdir)
    with _patch_externals(git_worktree_add=mock_add):
        report = implement.phase_start(
            ITEM_ID, json_output=True,
            worktree_path_override="rel-wt",
        )

    assert report["success"] is True, report
    expected = str((subdir / "rel-wt").resolve())
    mock_add.assert_called_once()
    assert mock_add.call_args.args[1] == expected, (
        f"relative override resolved to {mock_add.call_args.args[1]!r}, "
        f"expected {expected!r} (cwd-relative, not repo-root-relative)"
    )
    assert report["worktree_path"] == expected, report


def test_absolute_override_used_as_given(git_repo, tmp_path, monkeypatch):
    """AC4: an absolute ``--worktree-path`` is used as given."""
    abs_wt = tmp_path / "absolute-wt"
    mock_add = MagicMock(return_value=True)

    monkeypatch.chdir(git_repo)
    with _patch_externals(git_worktree_add=mock_add):
        report = implement.phase_start(
            ITEM_ID, json_output=True,
            worktree_path_override=str(abs_wt),
        )

    assert report["success"] is True, report
    mock_add.assert_called_once()
    assert mock_add.call_args.args[1] == str(abs_wt), (
        f"absolute override changed to {mock_add.call_args.args[1]!r}, "
        f"expected {str(abs_wt)!r}"
    )
    assert report["worktree_path"] == str(abs_wt), report
