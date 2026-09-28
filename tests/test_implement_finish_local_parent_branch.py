"""Regression tests: ``implement.py finish`` leaves local dev behind.

Root cause (SA-0MUGX1DIT000M7S1, discovered-from:AH-0MUD8QNRG006MCKB):
``phase_finish`` used to restore/pull the parent branch *before* pushing the
child branch to ``origin/dev``.  The push therefore landed after the pull,
leaving the main checkout's local ``dev`` one or more commits behind.  The
next ``phase_start`` forked the following child worktree from that stale
local ``dev`` and could not resolve files added by the previous sibling.

Coverage:

- AC1 — post-push local parent sync: after a successful push, the main
  checkout's local parent branch fast-forwards to the pushed tip.
- AC2 — safe-skip semantics: a dirty checkout, a diverged local parent
  branch, or a checkout on another branch never lose local commits and
  never fail.
- AC3 — children never start from a stale base: the parent branch is
  refreshed before ``git worktree add``, and ``phase_start`` wires the
  refresh before creating the worktree.

The tests use hermetic git sandboxes (bare "remote" + working clone under
``tmp_path``); the live SorraAgents checkout is never touched.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Paths / module loading
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


@pytest.fixture(scope="module")
def implement_mod():
    """Load implement.py as a module for direct helper testing."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_finish_local_parent", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_finish_local_parent"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Hermetic git sandbox helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command, raising on failure."""
    proc = subprocess.run(  # noqa: PLW1510
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}\n"
        f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    return proc


def _rev(cwd: Path, ref: str = "HEAD") -> str:
    """Return the full commit hash for *ref*."""
    return _git(["rev-parse", ref], cwd).stdout.strip()


def _init_clone(dest: Path, remote: Path) -> None:
    """Clone the bare remote into *dest* and set a deterministic identity."""
    _git(["clone", "--quiet", str(remote), str(dest)], dest.parent)
    _git(["config", "user.email", "test@test.com"], dest)
    _git(["config", "user.name", "Test"], dest)


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """Create a bare ``remote.git`` with a ``dev`` branch and a working clone.

    Returns a dict with ``remote`` and ``work`` paths.  ``work`` is checked
    out on ``dev`` and tracks ``origin/dev``.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "--initial-branch=dev"], remote)

    work = tmp_path / "work"
    _git(["clone", "--quiet", str(remote), str(work)], tmp_path)
    _git(["config", "user.email", "test@test.com"], work)
    _git(["config", "user.name", "Test"], work)

    (work / "README.md").write_text("# base\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "Initial commit"], work)
    _git(["branch", "-M", "dev"], work)
    _git(["push", "-u", "origin", "dev"], work)

    return {"remote": remote, "work": work, "tmp": tmp_path}


def _push_sibling_commit(sandbox: dict, filename: str, content: str) -> str:
    """Push a new commit to ``origin/dev`` from a separate clone.

    Simulates the just-finished sibling's ``finish`` push.  Returns the new
    commit hash now on ``origin/dev``.
    """
    other = sandbox["tmp"] / f"other-{filename.replace('.', '_')}"
    _init_clone(other, sandbox["remote"])
    _git(["checkout", "dev"], other)
    (other / filename).write_text(content)
    _git(["add", "-A"], other)
    _git(["commit", "-m", f"Add {filename}"], other)
    new_hash = _rev(other, "HEAD")
    _git(["push", "origin", "dev"], other)
    return new_hash


def _current_branch(cwd: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()


# ---------------------------------------------------------------------------
# AC1 — post-push local parent sync
# ---------------------------------------------------------------------------


def test_sync_parent_branch_fast_forwards_local_dev_after_push(
    implement_mod, sandbox
):
    """AC1: local dev fast-forwards to the pushed tip when safe."""
    work = sandbox["work"]
    _git(["checkout", "dev"], work)
    before = _rev(work, "dev")

    pushed = _push_sibling_commit(sandbox, "sibling.txt", "sibling content\n")
    assert before != pushed, "sandbox precondition: remote must have advanced"

    result = implement_mod._sync_parent_branch(str(work), "dev")

    assert result["method"] == "synced", result
    assert result["success"] is True
    assert _rev(work, "dev") == pushed == _rev(work, "origin/dev")
    # The working tree is updated too: the sibling's file is now present.
    assert (work / "sibling.txt").read_text() == "sibling content\n"


def test_sync_parent_branch_is_idempotent_when_already_current(
    implement_mod, sandbox
):
    """AC1: syncing an already-up-to-date dev is a successful no-op."""
    work = sandbox["work"]
    _git(["checkout", "dev"], work)
    tip = _rev(work, "dev")

    result = implement_mod._sync_parent_branch(str(work), "dev")

    assert result["method"] == "synced", result
    assert _rev(work, "dev") == tip


# ---------------------------------------------------------------------------
# AC2 — safe-skip semantics
# ---------------------------------------------------------------------------


def test_sync_parent_branch_skips_dirty_checkout(implement_mod, sandbox):
    """AC2: a dirty main checkout is skipped, not force-updated, no failure."""
    work = sandbox["work"]
    _git(["checkout", "dev"], work)
    before = _rev(work, "dev")
    _push_sibling_commit(sandbox, "sibling.txt", "sibling content\n")

    # Dirty the working tree with an untracked file.
    (work / "untracked.txt").write_text("uncommitted\n")

    result = implement_mod._sync_parent_branch(str(work), "dev")

    assert result["method"] == "skipped", result
    assert result["success"] is True
    assert result["reason"] == "dirty_working_tree"
    assert _rev(work, "dev") == before, "dirty checkout must not be updated"
    assert (work / "untracked.txt").exists(), "uncommitted work must survive"


def test_sync_parent_branch_skips_diverged_local_dev(implement_mod, sandbox):
    """AC2: a diverged local dev keeps its local commits and is not failed."""
    work = sandbox["work"]
    _git(["checkout", "dev"], work)

    # Create a local commit that is not on the remote.
    (work / "local.txt").write_text("local work\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "local commit"], work)
    local_commit = _rev(work, "dev")

    # Advance the remote independently → local and remote have diverged.
    _push_sibling_commit(sandbox, "remote.txt", "remote work\n")

    result = implement_mod._sync_parent_branch(str(work), "dev")

    assert result["method"] == "skipped", result
    assert result["success"] is True
    assert result["reason"] == "diverged"
    assert _rev(work, "dev") == local_commit, "local commits must be intact"
    log = _git(["log", "--oneline", "dev"], work).stdout
    assert "local commit" in log


def test_sync_parent_branch_updates_ref_without_switching_branch(
    implement_mod, sandbox
):
    """AC2: when off the parent branch, update the ref without a checkout."""
    work = sandbox["work"]
    _git(["checkout", "-b", "feature-x"], work)
    (work / "feature.txt").write_text("feature work\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "feature commit"], work)

    pushed = _push_sibling_commit(sandbox, "sibling.txt", "sibling content\n")

    result = implement_mod._sync_parent_branch(str(work), "dev")

    assert result["method"] == "synced", result
    assert _rev(work, "dev") == pushed, "local dev ref must be fast-forwarded"
    # The checkout itself must be untouched.
    assert _current_branch(work) == "feature-x"
    assert (work / "feature.txt").exists()


def test_sync_parent_branch_ignores_stale_local_origin_branch(
    implement_mod, sandbox
):
    """AC1/AC2: a stale local branch named ``origin/dev`` must not shadow
    the real remote-tracking ref (git resolves the ambiguous name to the
    local branch with a warning).
    """
    work = sandbox["work"]
    _git(["checkout", "dev"], work)
    stale = _rev(work, "dev")

    # Simulate an accidental local branch literally named ``origin/dev``
    # (the exact ref ambiguity this repo has hit in practice).
    _git(["branch", "origin/dev", stale], work)

    pushed = _push_sibling_commit(sandbox, "sibling.txt", "sibling content\n")
    assert pushed != stale

    result = implement_mod._sync_parent_branch(str(work), "dev")

    assert result["method"] == "synced", result
    assert _rev(work, "dev") == pushed, (
        "local dev must fast-forward to the remote-tracking origin/dev tip"
    )
    assert _rev(work, "origin/dev") == stale, (
        "the unrelated stale local branch must be left untouched"
    )


def test_sync_parent_branch_degrades_gracefully_when_remote_unreachable(
    implement_mod, sandbox
):
    """AC2: an unreachable remote reports a skip, never a failure."""
    work = sandbox["work"]
    _git(["checkout", "dev"], work)
    before = _rev(work, "dev")

    # Make origin unreachable.
    _git(["remote", "set-url", "origin", str(sandbox["tmp"] / "missing.git")], work)

    result = implement_mod._sync_parent_branch(str(work), "dev")

    assert result["method"] == "skipped", result
    assert result["success"] is True
    assert result["reason"] == "fetch_failed"
    assert _rev(work, "dev") == before


# ---------------------------------------------------------------------------
# AC3 — children never start from a stale base
# ---------------------------------------------------------------------------


def test_child_worktree_contains_sibling_commit_after_refresh(
    implement_mod, sandbox, tmp_path, monkeypatch
):
    """AC3: after a refresh, a new worktree forks from the pushed sibling."""
    work = sandbox["work"]
    _git(["checkout", "dev"], work)

    pushed = _push_sibling_commit(sandbox, "sibling.txt", "sibling content\n")

    sync = implement_mod._sync_parent_branch(str(work), "dev")
    assert sync["method"] == "synced", sync

    # ``git_worktree_add`` (like production) resolves the repo from the
    # process cwd, so run it from inside the sandbox checkout.
    monkeypatch.chdir(work)
    child_wt = tmp_path / "child-worktree"
    assert implement_mod.git_worktree_add("wl-child", str(child_wt), "dev") is True

    assert _rev(child_wt, "HEAD") == pushed, (
        "child worktree must fork from the refreshed (pushed) base"
    )
    assert (child_wt / "sibling.txt").read_text() == "sibling content\n"


def test_phase_start_refreshes_parent_before_worktree_add(implement_mod, tmp_path):
    """AC3: phase_start wires the parent refresh before git_worktree_add."""
    calls: list[str] = []

    def _record_sync(*args, **kwargs):
        calls.append("sync")
        return {"method": "synced", "success": True}

    def _record_worktree_add(*args, **kwargs):
        calls.append("worktree_add")
        return True

    with (
        mock.patch.object(implement_mod, "is_code_freeze_active", return_value=False),
        mock.patch.object(
            implement_mod.StatusLifecycle, "update_status", return_value=None
        ),
        mock.patch.object(implement_mod, "git_status", return_value=""),
        mock.patch.object(implement_mod, "git_has_dirty_files", return_value=False),
        mock.patch.object(
            implement_mod,
            "check_orphaned_stashes",
            return_value={
                "has_orphaned": False,
                "total_stashes": 0,
                "orphaned_stashes": [],
                "warning": "",
            },
        ),
        mock.patch.object(
            implement_mod, "wl_show",
            return_value={"id": "SA-0000000001", "title": "Test item"},
        ),
        mock.patch.object(
            implement_mod, "_get_repo_root", return_value=str(tmp_path)
        ),
        mock.patch.object(
            implement_mod, "_sync_parent_branch", side_effect=_record_sync
        ),
        mock.patch.object(
            implement_mod, "git_worktree_add", side_effect=_record_worktree_add
        ),
        mock.patch.object(implement_mod, "_ensure_node_modules_symlink"),
        mock.patch.object(implement_mod, "_ensure_submodules"),
        mock.patch.object(implement_mod, "wl_add_comment", return_value=True),
        mock.patch.object(implement_mod, "_store_signal_globals"),
        mock.patch.object(implement_mod, "_register_signal_handlers"),
        mock.patch.object(implement_mod, "write_state"),
        mock.patch.object(
            implement_mod, "worktree_path_for",
            return_value=str(tmp_path / "wt"),
        ),
        mock.patch.object(implement_mod, "branch_name_for", return_value="wl-test"),
        mock.patch.object(implement_mod, "slug_from_title", return_value="test"),
    ):
        report = implement_mod.phase_start("SA-0000000001", json_output=True)

    assert report["success"] is True, report
    assert calls == ["sync", "worktree_add"], (
        f"phase_start must refresh the parent branch before adding the "
        f"worktree; got {calls}"
    )
    assert report["steps"]["sync_parent_branch"]["method"] == "synced"


def test_phase_finish_syncs_parent_branch_after_push(implement_mod, tmp_path):
    """AC1: phase_finish calls _sync_parent_branch *after* a successful push,
    passing the state's parent branch (regression: a missing local variable
    here previously raised NameError after the push, leaving the item open).
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    calls: list[str] = []
    sync_args: list[tuple] = []

    state = implement_mod.ImplementState(
        work_item_id="SA-0000000001",
        worktree_path=str(worktree),
        repo_root=str(repo_root),
        parent_branch="dev",
        started_at="2026-01-01T00:00:00Z",
    )

    class _FakeLifecycle:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @staticmethod
        def update_status(*a, **k):
            return None

    def _record_push(*a, **k):
        calls.append("push")
        return True

    def _record_sync(*a, **k):
        calls.append("sync")
        sync_args.append(a)
        return {"method": "synced", "success": True}

    with (
        mock.patch.object(
            implement_mod, "_discover_worktree", return_value=str(worktree)
        ),
        mock.patch.object(implement_mod, "read_state", return_value=state),
        mock.patch.object(
            implement_mod, "_worktree_placement_violation", return_value=None
        ),
        mock.patch.object(
            implement_mod,
            "run_build",
            return_value={"success": True, "exit_code": 0, "stderr": ""},
        ),
        mock.patch.object(
            implement_mod,
            "run_tests",
            return_value={
                "success": True,
                "failures": [],
                "skipped": False,
                "tooling": "pytest",
                "scope": "full",
            },
        ),
        mock.patch.object(implement_mod, "git_commit", return_value=True),
        mock.patch.object(
            implement_mod, "git_get_commit_hash", return_value="abc1234"
        ),
        mock.patch.object(
            implement_mod, "cleanup_worktree_processes", return_value={}
        ),
        mock.patch.object(implement_mod, "remove_state"),
        mock.patch.object(implement_mod, "_remove_worktree", return_value=True),
        mock.patch.object(
            implement_mod, "git_push_to_dev", side_effect=_record_push
        ),
        mock.patch.object(
            implement_mod, "_sync_parent_branch", side_effect=_record_sync
        ),
        mock.patch.object(implement_mod, "wl_add_comment", return_value=True),
        mock.patch.object(implement_mod, "StatusLifecycle", _FakeLifecycle),
    ):
        report = implement_mod.phase_finish(
            "SA-0000000001", json_output=True, no_refactor=True
        )

    assert report["success"] is True, report
    assert calls == ["push", "sync"], (
        f"phase_finish must push then sync the parent branch; got {calls}"
    )
    assert sync_args == [(str(repo_root), "dev")], (
        f"_sync_parent_branch must receive (repo_root, state.parent_branch); "
        f"got {sync_args}"
    )
    assert report["steps"]["sync_parent_branch"]["method"] == "synced"


# ---------------------------------------------------------------------------
# Structural / documentation guards
# ---------------------------------------------------------------------------


class TestFinishOrdering:
    SOURCE = _IMPLEMENT_PY.read_text()

    def test_push_precedes_sync_in_phase_finish(self):
        """The post-push sync must be called after the push, not before."""
        # Scope the search to phase_finish's body: phase_start also calls
        # _sync_parent_branch (before its worktree add).
        finish_start = self.SOURCE.index("def phase_finish(")
        finish_end = self.SOURCE.index("def phase_abort(", finish_start)
        body = self.SOURCE[finish_start:finish_end]
        push_idx = body.index("git_push_to_dev(repo_root, branch)")
        sync_idx = body.index("_sync_parent_branch(repo_root,")
        assert sync_idx > push_idx, (
            "local parent sync must run AFTER the push to dev"
        )

    def test_sync_helper_defined(self):
        assert "def _sync_parent_branch(" in self.SOURCE

    def test_sync_helper_uses_ff_only(self):
        # The checked-out branch path must use a fast-forward-only merge.
        assert '"--ff-only"' in self.SOURCE or "'--ff-only'" in self.SOURCE


class TestDocumentation:
    _SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"

    def test_skill_md_documents_post_push_sync(self):
        """AC6: SKILL.md documents the post-push local parent sync."""
        assert self._SKILL_MD.exists(), f"SKILL.md not found at {self._SKILL_MD}"
        content = self._SKILL_MD.read_text()
        assert "local parent branch" in content.lower() or (
            "post-push" in content.lower()
        ), "SKILL.md must document the post-push local parent sync"
