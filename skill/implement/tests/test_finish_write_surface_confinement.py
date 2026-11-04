"""Write-surface confinement evidence for the finish guard and cleanup paths.

Covers SA-0MVXVC0KB004XDH0 (parent SA-0MVWMUBP10041CSM AC4): every write
surface reachable from ``implement.py finish``'s main-checkout guard and from
the worktree-cleanup path is shown confined to ``repo_root`` or the own
worktree. Each test pins one claim of the audit's findings table with
observable behaviour, never with a source grep:

- **The guard is read-only.** ``_snapshot_main_checkout``,
  ``_verify_main_checkout``, ``_main_checkout_guard_violations`` and
  ``_main_checkout_actor_evidence`` read git status, state files and
  ``/proc``; a full snapshot → verify → evidence cycle on a real, dirty
  checkout must leave every working-tree byte (content, size, mtime) and the
  porcelain status surface unchanged.
- **Removal is confined to the named, registered worktree.**
  ``_remove_worktree``'s manual ``shutil.rmtree`` fallback used to run
  unconditionally, so any path outside the repository that lacked a ``.git/``
  directory was deleted outright. It now refuses anything git does not
  register as a worktree of ``repo_root`` (the deviation this item fixes),
  still removes a genuine registered worktree, and stays idempotent for a
  tree that is already gone.
- **The state-file surface is worktree-local.** ``write_state`` /
  ``remove_state`` touch ``<worktree>/.implement_state.json`` and nothing
  else.
- **Every main-checkout git operation runs in ``repo_root``.** The Step-5
  fast-forward, its failed-push reversal and the post-push parent-branch sync
  issue *all* their git commands with ``cwd=repo_root``; neither the own
  worktree nor a sibling directory is ever a cwd, so they cannot act on
  another checkout.
- **The guard's comment surface writes the worklog store, not a checkout.**
  ``wl_add_comment`` goes through the ``wl`` CLI and mutates no file in the
  checkout.

Everything runs in hermetic ``shared.git_sandbox`` sandboxes; the live
SorraAgents checkout is never touched.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement
from skill.shared import git_sandbox as gs

#: Directories excluded from the "no working-tree byte changed" digest: the
#: git database (``git status`` legitimately refreshes ``.git/index``) and the
#: worklog store (``wl`` bookkeeping, never a checkout file).
_DIGEST_EXCLUDES = (".git", ".worklog")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A hermetic git repository with one tracked file and a clean tree."""
    for var in gs.REPOSITORY_OVERRIDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    work = gs.init_repo(tmp_path / "work", default_branch="dev")
    (work / "README.md").write_text("# base\n", encoding="utf-8")
    gs.commit_all(work, "Initial commit")
    return work


@pytest.fixture
def repo_with_worktree(repo: Path, tmp_path: Path) -> dict[str, Path]:
    """The repo plus one registered worktree and one unrelated sibling dir."""
    worktree = gs.add_worktree(repo, tmp_path / "wl-SA-0TESTWT1", "wl-SA-0TESTWT1")
    (worktree / "work.txt").write_text("work\n", encoding="utf-8")
    gs.commit_all(worktree, "Worktree commit")
    victim = tmp_path / "not-a-worktree"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me\n", encoding="utf-8")
    return {"repo": repo, "worktree": worktree, "victim": victim}


def _tree_digest(root: Path) -> dict[str, tuple[int, int, str]]:
    """Content, size and mtime of every working-tree file under *root*."""
    digest: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in _DIGEST_EXCLUDES:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        digest[str(rel)] = (
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return digest


def _porcelain(root: Path) -> str:
    return gs.run_git(root, ["status", "--porcelain=v1"]).stdout


# ---------------------------------------------------------------------------
# The guard's write surfaces are read-only
# ---------------------------------------------------------------------------


def test_guard_surfaces_leave_the_checkout_byte_identical(repo: Path, tmp_path: Path):
    """snapshot → verify → actor evidence mutate nothing in the checkout."""
    # A dirty checkout, so every read surface has something to report: a
    # modified tracked file, an untracked file and a deleted tracked file.
    (repo / "tracked-then-deleted.txt").write_text("gone\n", encoding="utf-8")
    gs.commit_all(repo, "Add a file that will be deleted")
    (repo / "README.md").write_text("# base\nlocal edit\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("scratch\n", encoding="utf-8")
    (repo / "tracked-then-deleted.txt").unlink()

    own_worktree = gs.add_worktree(
        repo, tmp_path / "wl-SA-0TESTWT1", "wl-SA-0TESTWT1"
    )
    digest_before = _tree_digest(repo)
    status_before = _porcelain(repo)

    baseline = implement._snapshot_main_checkout(str(repo))
    guard: dict = {"verifies": [], "verdict": "pass"}
    violations = implement._verify_main_checkout(
        guard,
        baseline,
        repo_root=str(repo),
        worktree_path=str(own_worktree),
        own_churn=[],
        expected_dirty=(),
        stage="pre_push",
    )
    evidence = implement._main_checkout_actor_evidence(
        str(repo), str(own_worktree)
    )

    # The surfaces really read the checkout: the pre-existing dirty files are
    # in the baseline (so they are not "violations"), and the verify recorded
    # its entry. A no-op implementation would fail these assertions.
    assert set(baseline["status"]) >= {
        "README.md",
        "untracked.txt",
        "tracked-then-deleted.txt",
    }
    assert guard["verifies"][0]["stage"] == "pre_push"
    assert violations == []
    assert isinstance(evidence, str) and evidence

    # ... and they wrote nothing: no file content, size or mtime changed, and
    # the porcelain surface is identical.
    assert _tree_digest(repo) == digest_before
    assert _porcelain(repo) == status_before


def test_guard_verify_reports_a_foreign_deletion_without_touching_it(
    repo: Path, tmp_path: Path
):
    """A foreign deletion is reported read-only — the guard never "fixes" it."""
    (repo / "packages").mkdir()
    (repo / "packages" / "shared.ts").write_text("export {}\n", encoding="utf-8")
    gs.commit_all(repo, "Add a file a foreign actor will delete")
    own_worktree = gs.add_worktree(
        repo, tmp_path / "wl-SA-0TESTWT1", "wl-SA-0TESTWT1"
    )

    baseline = implement._snapshot_main_checkout(str(repo))
    (repo / "packages" / "shared.ts").unlink()
    digest_after_delete = _tree_digest(repo)

    guard: dict = {"verifies": [], "verdict": "pass"}
    violations = implement._verify_main_checkout(
        guard,
        baseline,
        repo_root=str(repo),
        worktree_path=str(own_worktree),
        own_churn=[],
        expected_dirty=(),
        stage="pre_push",
    )

    assert violations == ["packages/shared.ts [ D]"]
    assert guard["verdict"] == "fail"
    # Read-only: the deletion is still there (never restored, never "fixed").
    assert not (repo / "packages" / "shared.ts").exists()
    assert _tree_digest(repo) == digest_after_delete


# ---------------------------------------------------------------------------
# Removal is confined to the named, registered worktree
# ---------------------------------------------------------------------------


def test_remove_worktree_refuses_a_directory_outside_the_repository(
    repo_with_worktree: dict[str, Path]
):
    """An unregistered sibling directory is never deleted (the fixed deviation)."""
    repo = repo_with_worktree["repo"]
    victim = repo_with_worktree["victim"]

    with pytest.raises(RuntimeError) as excinfo:
        implement._remove_worktree(str(victim), repo_root=str(repo))

    assert "REFUSE" in str(excinfo.value)
    assert str(victim) in str(excinfo.value)
    # The directory and its content survive untouched.
    assert victim.is_dir()
    assert (victim / "important.txt").read_text(encoding="utf-8") == "keep me\n"


def test_remove_worktree_refuses_when_ownership_cannot_be_proven(
    repo_with_worktree: dict[str, Path], tmp_path: Path
):
    """No readable worktree registry ⇒ fail closed, delete nothing."""
    victim = repo_with_worktree["victim"]
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    with pytest.raises(RuntimeError) as excinfo:
        implement._remove_worktree(str(victim), repo_root=str(not_a_repo))

    assert "REFUSE" in str(excinfo.value)
    assert (victim / "important.txt").exists()


def test_remove_worktree_removes_only_the_registered_worktree(
    repo_with_worktree: dict[str, Path]
):
    """The genuine target is removed; the repo and the sibling survive."""
    repo = repo_with_worktree["repo"]
    worktree = repo_with_worktree["worktree"]
    victim = repo_with_worktree["victim"]
    repo_digest = _tree_digest(repo)

    assert implement._remove_worktree(str(worktree), repo_root=str(repo)) is True

    assert not worktree.exists()
    assert (victim / "important.txt").read_text(encoding="utf-8") == "keep me\n"
    assert _tree_digest(repo) == repo_digest
    listed = gs.run_git(repo, ["worktree", "list", "--porcelain"]).stdout
    assert str(worktree) not in listed


def test_remove_worktree_is_idempotent_for_an_already_absent_tree(
    repo_with_worktree: dict[str, Path]
):
    """A tree that is already gone is a reported no-op, never an error."""
    repo = repo_with_worktree["repo"]
    worktree = repo_with_worktree["worktree"]
    shutil.rmtree(worktree)

    assert implement._remove_worktree(str(worktree), repo_root=str(repo)) is True
    assert not worktree.exists()


# ---------------------------------------------------------------------------
# The state-file surface is worktree-local
# ---------------------------------------------------------------------------


def test_state_file_surface_stays_inside_the_worktree(
    repo_with_worktree: dict[str, Path]
):
    """write_state / remove_state touch only <worktree>/.implement_state.json."""
    repo = repo_with_worktree["repo"]
    worktree = repo_with_worktree["worktree"]
    (worktree / "keep.txt").write_text("keep\n", encoding="utf-8")
    repo_digest = _tree_digest(repo)

    state = implement.ImplementState(
        work_item_id="SA-0TESTWT1",
        worktree_path=str(worktree),
        repo_root=str(repo),
        parent_branch="dev",
    )
    implement.write_state(state, str(worktree))
    state_path = worktree / implement.STATE_FILE_NAME
    assert state_path.is_file()
    assert not (repo / implement.STATE_FILE_NAME).exists()

    implement.remove_state(str(worktree))

    assert not state_path.exists()
    assert (worktree / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert _tree_digest(repo) == repo_digest


# ---------------------------------------------------------------------------
# Main-checkout git operations all run in repo_root
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Bare remote + main checkout on ``dev`` + one registered worktree."""
    for var in gs.REPOSITORY_OVERRIDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    remote = gs.init_bare_remote(tmp_path / "remote.git")
    repo = gs.init_repo(tmp_path / "work", default_branch="dev")
    (repo / "README.md").write_text("# base\n", encoding="utf-8")
    gs.commit_all(repo, "Initial commit")
    gs.add_remote(repo, "origin", str(remote))
    gs.run_git(repo, ["push", "-q", "-u", "origin", "dev"], check=True)
    worktree = gs.add_worktree(repo, tmp_path / "wl-SA-0TESTWT1", "wl-SA-0TESTWT1")
    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
    gs.commit_all(worktree, "Feature commit")
    return {"repo": repo, "worktree": worktree, "remote": remote}


def test_main_checkout_git_operations_only_run_in_repo_root(
    remote_sandbox: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """Fast-forward and parent-branch sync never use another checkout as cwd."""
    repo = remote_sandbox["repo"]
    worktree = remote_sandbox["worktree"]

    recorded_cwds: list[str | None] = []
    real_run_cmd = implement.run_cmd

    def _recording_run_cmd(cmd, cwd=None, **kwargs):
        recorded_cwds.append(cwd)
        return real_run_cmd(cmd, cwd=cwd, **kwargs)

    monkeypatch.setattr(implement, "run_cmd", _recording_run_cmd)

    commit = gs.run_git(worktree, ["rev-parse", "HEAD"]).stdout.strip()
    fast_forward = implement._fast_forward_local_dev_for_push(
        str(repo), commit, "dev"
    )
    assert fast_forward["method"] == "advanced", fast_forward
    assert recorded_cwds, "the fast-forward must issue git commands"
    assert set(recorded_cwds) == {str(repo)}

    # The reversal of that advance (the failed-push path) is equally confined.
    recorded_cwds.clear()
    restore = implement._restore_local_dev_after_failed_push(
        str(repo), fast_forward, "dev"
    )
    assert restore["restored"] is True, restore
    assert recorded_cwds
    assert set(recorded_cwds) == {str(repo)}

    # Publish, then sync the parent branch: every git command it issues must
    # likewise be confined to the main checkout.
    gs.run_git(repo, ["push", "-q", "origin", "dev"], check=True)
    recorded_cwds.clear()
    sync = implement._sync_parent_branch(str(repo), "dev")
    assert sync["method"] == "synced", sync
    assert recorded_cwds
    assert set(recorded_cwds) == {str(repo)}

    # Never the worktree this run is finishing, and never the bare remote.
    assert str(worktree) not in set(recorded_cwds)
    assert str(remote_sandbox["remote"]) not in set(recorded_cwds)


# ---------------------------------------------------------------------------
# The guard's comment surface writes the worklog store, not a checkout
# ---------------------------------------------------------------------------


def test_wl_add_comment_writes_the_worklog_store_not_the_checkout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """The guard's fail-loud comment goes through ``wl``; no file is touched."""
    digest_before = _tree_digest(repo)
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run_cmd(cmd, cwd=None, **kwargs):
        calls.append(list(cmd))
        return _Completed()

    monkeypatch.setattr(implement, "run_cmd", _fake_run_cmd)

    assert implement.wl_add_comment("SA-0TESTWT1", "guard abort") is True

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "wl"
    assert "comment" in argv and "add" in argv
    assert "SA-0TESTWT1" in argv and "guard abort" in argv
    # Nothing in the checkout changed: the write went to the worklog store.
    assert _tree_digest(repo) == digest_before
