"""Tests for idempotent ``phase_start`` resume (SA-0MUWNHKQH0034DW9).

When ``phase_drive`` starts a child and then spawns a fresh session to
implement it, that session runs the standard leaf workflow — whose Step 5
calls ``implement.py start`` again. ``start`` must therefore **resume** the
pre-created worktree instead of failing on ``git worktree add``.

These tests patch the worklog/git plumbing and exercise the worktree
create-vs-resume branch in ``phase_start`` directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
PARENT = "SA-0MUWNHKQH0034DW9"


@pytest.fixture(scope="module")
def implement_mod():
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_start_resume", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_start_resume"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_fake_worktree(tmp_path: Path) -> Path:
    """A directory with ``.git`` as a FILE (i.e. a git worktree)."""
    wt = tmp_path / "wl-SA-x-slug"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
    return wt


def _patch_start(m, tmp_path, add_side_effect):
    """Patch every external dependency of ``phase_start``."""
    return [
        mock.patch.object(m, "is_code_freeze_active", return_value=False),
        mock.patch.object(m.StatusLifecycle, "update_status"),
        mock.patch.object(m, "git_status", return_value=""),
        mock.patch.object(m, "git_has_dirty_files", return_value=False),
        mock.patch.object(
            m, "check_orphaned_stashes",
            return_value={"has_orphaned": False, "total_stashes": 0,
                          "orphaned_stashes": []},
        ),
        mock.patch.object(
            m, "wl_show",
            return_value={"id": PARENT, "title": "Drive all epic children"},
        ),
        mock.patch.object(
            m, "_check_and_evaluate_risk_effort", return_value={"success": True},
        ),
        mock.patch.object(m, "_get_repo_root", return_value=str(tmp_path)),
        mock.patch.object(m, "_sync_parent_branch", return_value={"success": True}),
        mock.patch.object(m, "git_worktree_add", side_effect=add_side_effect),
        mock.patch.object(m, "_ensure_node_modules_symlink"),
        mock.patch.object(m, "_ensure_nested_node_modules_symlinks"),
        mock.patch.object(m, "_ensure_submodules"),
        mock.patch.object(m, "wl_add_comment", return_value=True),
        mock.patch.object(m, "write_state"),
        mock.patch.object(m, "_store_signal_globals"),
        mock.patch.object(m, "_register_signal_handlers"),
    ]


def test_start_resumes_existing_worktree(implement_mod, tmp_path):
    m = implement_mod
    wt = _make_fake_worktree(tmp_path)

    def must_not_add(*a, **k):
        raise AssertionError("git_worktree_add must not run when resuming")

    patches = _patch_start(m, tmp_path, must_not_add)
    for p in patches:
        p.start()
    try:
        report = m.phase_start(
            PARENT, json_output=True, worktree_path_override=str(wt),
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert report["success"] is True
    assert report["resumed"] is True
    assert Path(report["worktree_path"]) == wt.resolve()


def test_start_creates_worktree_when_absent(implement_mod, tmp_path):
    m = implement_mod
    wt = tmp_path / "wl-SA-x-new"  # does not exist yet
    calls: dict = {}

    def fake_add(branch, path, parent_branch="dev"):
        calls["branch"] = branch
        calls["path"] = path
        return True

    patches = _patch_start(m, tmp_path, fake_add)
    for p in patches:
        p.start()
    try:
        report = m.phase_start(
            PARENT, json_output=True, worktree_path_override=str(wt),
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert report["success"] is True
    assert report["resumed"] is False
    assert calls["path"] == str(wt)
