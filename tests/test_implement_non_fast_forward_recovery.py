"""Regression tests: ``implement.py finish`` recovers non-fast-forward pushes.

Root cause (WL-0MV533YSW004D1E1, discovered-from:WL-0MV28X3BT0079JM3):
When ``dev`` advanced concurrently during ``phase_finish``, the
``PushNonFastForwardError`` handler called ``run_cmd(["git", "rebase",
"origin/dev"], cwd=cwd)`` but ``cwd`` was never defined in ``phase_finish``.
The handler raised ``NameError`` *after* the worktree had already been
removed, stranding the committed work on a local branch while the item was
reset to ``open``.

The recovery must therefore:

- run the rebase inside the worktree (``branch`` is checked out there), which
  means the push must happen *before* the worktree is removed;
- fetch ``origin/dev`` first so the rebase targets the concurrently-advanced
  tip rather than the stale remote-tracking ref;
- retry the push and report the rewritten commit hash so the completion
  comment names the commit that actually landed on ``dev``.

Coverage:

- AC1 — the handler rebases in the correct directory and raises no NameError.
- AC2 — a non-fast-forward push where ``dev`` advanced concurrently is
  recovered end-to-end against a hermetic git sandbox.
- AC3 — the committed work is not stranded: the rebased commit reaches
  ``origin/dev``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


@pytest.fixture(scope="module")
def implement_mod():
    """Load implement.py as a module for direct helper testing."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_non_ff", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "implement_under_test_non_ff"
    sys.modules["implement_under_test_non_ff"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Hermetic git sandbox helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command, raising on failure."""
    proc = subprocess.run(
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


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """Create a bare ``remote.git`` with a ``dev`` branch and a working clone.

    The working clone (returned under ``work``) is checked out on ``dev`` and
    tracks ``origin/dev``.  A linked worktree on the feature branch is added
    under ``wt`` so the recovery rebase has somewhere to run.
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

    # A linked worktree on the feature branch, exactly where phase_finish
    # keeps the worktree before the (reordered) push.
    wt = tmp_path / "wt"
    _git(
        ["worktree", "add", "--track", "-b", "wl-test-branch", str(wt), "dev"],
        work,
    )
    _git(["config", "user.email", "test@test.com"], wt)
    _git(["config", "user.name", "Test"], wt)
    (wt / "feature.txt").write_text("feature content\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-m", "Add feature"], wt)
    feature_hash = _rev(wt, "HEAD")

    return {
        "remote": remote,
        "work": work,
        "wt": wt,
        "tmp": tmp_path,
        "feature_hash": feature_hash,
    }


def _advance_remote_dev(sandbox: dict, filename: str, content: str) -> str:
    """Advance ``origin/dev`` from a separate clone (concurrent push)."""
    other = sandbox["tmp"] / f"other-{filename.replace('.', '_')}"
    _git(["clone", "--quiet", str(sandbox["remote"]), str(other)], sandbox["tmp"])
    _git(["config", "user.email", "test@test.com"], other)
    _git(["config", "user.name", "Test"], other)
    _git(["checkout", "dev"], other)
    (other / filename).write_text(content)
    _git(["add", "-A"], other)
    _git(["commit", "-m", f"Add {filename}"], other)
    new_hash = _rev(other, "HEAD")
    _git(["push", "origin", "dev"], other)
    return new_hash


# ---------------------------------------------------------------------------
# AC1 — rebase runs in the worktree, no NameError
# ---------------------------------------------------------------------------


@pytest.fixture
def silent_comments(monkeypatch, implement_mod):
    """Neutralise worklog comments (they shell out to ``wl``)."""
    monkeypatch.setattr(implement_mod, "wl_add_comment", lambda *a, **k: True)


class TestRecoveryHelper:
    """AC1: ``_recover_non_fast_forward_push`` is the extracted handler."""

    def test_helper_exists(self, implement_mod):
        assert hasattr(implement_mod, "_recover_non_fast_forward_push")

    def test_rebase_runs_in_worktree_not_repo_root(
        self, implement_mod, monkeypatch, silent_comments
    ):
        """AC1: the rebase (and fetch) must run in the worktree."""
        captured: list[tuple[list[str], str | None]] = []

        def fake_run_cmd(cmd, **kwargs):
            captured.append((list(cmd), kwargs.get("cwd")))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(implement_mod, "run_cmd", fake_run_cmd)
        monkeypatch.setattr(
            implement_mod, "git_get_commit_hash", lambda cwd: "newhash123"
        )
        pushes: list[tuple] = []
        monkeypatch.setattr(
            implement_mod,
            "git_push_to_dev",
            lambda *a, **k: pushes.append(a) or True,
        )

        exc = implement_mod.PushNonFastForwardError(
            "oldhash", "wl-branch", "rejected (non-fast-forward)", "retry",
        )
        result = implement_mod._recover_non_fast_forward_push(
            worktree_path="/worktrees/wt",
            repo_root="/main/checkout",
            branch="wl-branch",
            commit_hash="oldhash",
            exc=exc,
            work_item_id="SA-TEST",
        )

        assert result == "newhash123"
        rebase = [c for c in captured if c[0][:2] == ["git", "rebase"]]
        assert rebase, f"expected a git rebase invocation; got {captured}"
        assert rebase[0][1] == "/worktrees/wt", (
            f"rebase must run in the worktree; got cwd={rebase[0][1]!r}"
        )
        fetch = [c for c in captured if c[0][:2] == ["git", "fetch"]]
        assert fetch, f"expected a git fetch before the rebase; got {captured}"
        assert fetch[0][1] == "/worktrees/wt", (
            f"fetch must run in the worktree; got cwd={fetch[0][1]!r}"
        )
        assert pushes == [("/main/checkout", "wl-branch", "newhash123")]

    def test_rebase_failure_preserves_state(
        self, implement_mod, monkeypatch
    ):
        """A conflicted rebase preserves commit/branch for manual recovery."""
        comments: list[str] = []
        monkeypatch.setattr(
            implement_mod, "run_cmd",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ["git", "fetch"]
            else subprocess.CompletedProcess(cmd, 1, "", "conflict"),
        )
        monkeypatch.setattr(
            implement_mod, "wl_add_comment", lambda wid, msg: comments.append(msg)
        )

        exc = implement_mod.PushNonFastForwardError(
            "oldhash", "wl-branch", "rejected (non-fast-forward)", "retry",
        )
        with pytest.raises(RuntimeError, match="rebase"):
            implement_mod._recover_non_fast_forward_push(
                worktree_path="/worktrees/wt",
                repo_root="/main/checkout",
                branch="wl-branch",
                commit_hash="oldhash",
                exc=exc,
                work_item_id="SA-TEST",
            )
        assert comments, "a recovery comment must be posted on rebase failure"
        assert "oldhash" in comments[0]
        assert "wl-branch" in comments[0]
        assert "/worktrees/wt" in comments[0]


# ---------------------------------------------------------------------------
# AC2 / AC3 — end-to-end recovery against a real remote
# ---------------------------------------------------------------------------


class TestNonFastForwardRecoveryEndToEnd:
    def test_recovery_rebases_and_pushes_concurrent_advance(
        self, implement_mod, sandbox, monkeypatch, silent_comments
    ):
        """AC2/AC3: a concurrent ``dev`` advance is rebased and the work lands."""
        work = sandbox["work"]
        wt = sandbox["wt"]
        feature_hash = sandbox["feature_hash"]

        # A sibling pushes to dev after our feature commit was created.
        sibling_hash = _advance_remote_dev(sandbox, "sibling.txt", "sibling\n")

        # The first push genuinely fails non-fast-forward.
        with pytest.raises(implement_mod.PushNonFastForwardError) as exc_info:
            implement_mod.git_push_to_dev(str(work), "wl-test-branch", feature_hash)

        # Recover using the (real) helper: fetch, rebase, retry.
        result_hash = implement_mod._recover_non_fast_forward_push(
            worktree_path=str(wt),
            repo_root=str(work),
            branch="wl-test-branch",
            commit_hash=feature_hash,
            exc=exc_info.value,
            work_item_id="SA-TEST",
        )

        # AC3: the work is recovered, not stranded.
        assert result_hash != feature_hash, "rebase must rewrite the commit"
        remote_tip = _rev(sandbox["remote"], "dev")
        assert remote_tip.startswith(result_hash), (
            "the pushed dev tip must be the rebased feature commit"
        )
        assert _rev(wt, "HEAD") == remote_tip

        # AC2 (SA-0MVPZG7A0000N66U): the *rebased replacement hash* — not the
        # pre-rebase commit — is what finish asserts against the pushed
        # origin/dev tip, so the assertion is exercised with the rewritten
        # commit exactly as phase_finish does after recovery.
        ancestry = implement_mod._assert_commit_on_origin_dev(
            str(work), result_hash
        )
        assert ancestry["verified"] is True, ancestry
        assert ancestry["resolved_commit"] == remote_tip
        assert ancestry["target"] == "refs/remotes/origin/dev"

        # The rebased commit sits on top of the concurrent sibling advance.
        ancestor = _git(
            ["merge-base", "--is-ancestor", sibling_hash, remote_tip], sandbox["remote"]
        )
        assert ancestor.returncode == 0
        # Both files are present on origin/dev.
        assert (
            _git(["cat-file", "-p", "dev:feature.txt"], sandbox["remote"]).stdout
            == "feature content\n"
        )
        assert (
            _git(["cat-file", "-p", "dev:sibling.txt"], sandbox["remote"]).stdout
            == "sibling\n"
        )


# ---------------------------------------------------------------------------
# Structural guards — push must precede worktree removal
# ---------------------------------------------------------------------------


class TestFinishOrdering:
    SOURCE = _IMPLEMENT_PY.read_text()

    @staticmethod
    def _finish_body() -> str:
        start = TestFinishOrdering.SOURCE.index("def phase_finish(")
        end = TestFinishOrdering.SOURCE.index("def _mark_pane_aborted(", start)
        return TestFinishOrdering.SOURCE[start:end]

    def test_push_precedes_worktree_removal(self):
        """The push must run before the worktree is removed so the recovery
        rebase has a directory in which ``branch`` is checked out."""
        body = self._finish_body()
        push_idx = body.index("git_push_to_dev(repo_root, branch")
        remove_idx = body.index("_remove_worktree(")
        assert push_idx < remove_idx, (
            "phase_finish must push to dev before removing the worktree"
        )

    def test_handler_calls_recovery_helper(self):
        body = self._finish_body()
        assert "_recover_non_fast_forward_push(" in body, (
            "phase_finish must delegate non-fast-forward recovery to the helper"
        )

    def test_phase_finish_has_no_undefined_cwd(self):
        """Regression for the original ``NameError: name 'cwd' is not defined``."""
        body = self._finish_body()
        assert "cwd=cwd" not in body, (
            "phase_finish must not reference the undefined ``cwd`` variable"
        )
