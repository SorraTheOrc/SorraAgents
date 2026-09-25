"""Contract tests for the hermetic git sandbox + snapshot helpers (F2, SA-0MUG30HV8004HV94).

Behaviour under test (``skill/shared/git_sandbox.py``):

- a path inside any git work tree is rejected, a plain ``tmp_path`` is accepted,
  and a factory-created repo resolves ``git rev-parse --show-toplevel`` under
  ``tmp_path`` (F3 AC1/AC2/AC4);
- the factories neutralise a leaked ``GIT_DIR`` — the reproduction mechanism
  behind the 2026-09-24 live-checkout incident (F3 AC1);
- ``snapshot_repo_state`` / ``diff_snapshots`` report an added ref, a deleted
  ref, a moved ref, a changed local-config key, a deleted tracked file and a
  newly added untracked file — and report nothing for an unchanged repo
  (F3 AC5; parent AC3).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

# ---------------------------------------------------------------------------
# AC1/AC2 — sandbox containment
# ---------------------------------------------------------------------------


class TestSandboxContainment:
    def test_path_inside_live_checkout_is_rejected(self):
        """A path inside the live work tree is refused (superset of the old guard)."""
        with pytest.raises(gs.GitSandboxError):
            gs.assert_outside_git_worktree(REPO_ROOT)
        with pytest.raises(gs.GitSandboxError):
            gs.assert_outside_git_worktree(REPO_ROOT / "skill")

    def test_plain_tmp_path_is_accepted(self, tmp_path):
        """A tmp_path outside any repository is a valid sandbox root."""
        gs.assert_outside_git_worktree(tmp_path)  # must not raise

    def test_factory_repo_toplevel_is_under_tmp_path(self, tmp_path):
        """A factory-created repo resolves its toplevel under tmp_path."""
        repo = gs.init_repo(tmp_path / "fixture-repo")
        top = gs.git_toplevel(repo)
        assert top is not None
        assert Path(top).resolve().is_relative_to(tmp_path.resolve())

    def test_factory_repo_has_no_real_origin(self, tmp_path):
        """A migrated fixture repo never points a remote at the real origin."""
        real_origin = gs.run_git(REPO_ROOT, ["remote", "get-url", "origin"]).stdout.strip()
        repo = gs.init_repo(tmp_path / "fixture-repo")
        remotes = gs.run_git(repo, ["remote", "-v"]).stdout
        assert real_origin
        assert real_origin not in remotes

    def test_bare_remote_and_worktree_are_under_tmp_path(self, tmp_path):
        """The remote/worktree factories stay inside the sandbox root."""
        repo = gs.init_repo(tmp_path / "repo")
        (repo / "f.txt").write_text("x", encoding="utf-8")
        gs.commit_all(repo, "init")
        remote = gs.init_bare_remote(tmp_path / "origin.git")
        gs.add_remote(repo, "origin", str(remote))
        wt = gs.add_worktree(repo, tmp_path / "wt", "feature")
        assert gs.git_toplevel(wt) == str(wt.resolve())

    def test_worktree_registered_under_agent_dir_is_excluded(self, tmp_path):
        """Refs checked out in .worklog/worktrees/ are excluded from the diff."""
        repo = gs.init_repo(tmp_path / "repo")
        (repo / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        gs.commit_all(repo, "init")
        agent_wt = repo / ".worklog" / "worktrees" / "wl-agent"
        gs.add_worktree(repo, agent_wt, "wl-agent")
        before = gs.snapshot_repo_state(repo)
        (agent_wt / "agent.txt").write_text("agent", encoding="utf-8")
        gs.commit_all(agent_wt, "agent work")  # moves the agent worktree branch
        after = gs.snapshot_repo_state(repo)
        assert "refs/heads/wl-agent" in after["excluded_refs"]
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_worktree_less_wl_branch_is_not_excluded(self, tmp_path):
        """A wl-* branch with no registered worktree IS a reportable mutation."""
        repo = gs.init_repo(tmp_path / "repo")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        gs.commit_all(repo, "init")
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["branch", "wl-incident-test"], check=True)
        after = gs.snapshot_repo_state(repo)
        diff = gs.diff_snapshots(before, after)
        assert diff["changed"]
        assert "refs/heads/wl-incident-test" in diff["refs"]["added"]


# ---------------------------------------------------------------------------
# AC1 — leaked environment is neutralised
# ---------------------------------------------------------------------------


class TestEnvironmentIsolation:
    def test_leaked_git_dir_cannot_redirect_a_factory(self, tmp_path, monkeypatch):
        """A leaked GIT_DIR must not make the factory mutate the named repo.

        This is the deterministic guard on the incident mechanism: without
        neutralisation, ``git init`` / ``git add`` / ``git commit`` in the
        fixture would operate on the leaked target.
        """
        victim = gs.init_repo(tmp_path / "victim")
        (victim / "real.txt").write_text("real", encoding="utf-8")
        gs.commit_all(victim, "victim base")
        before = gs.snapshot_repo_state(victim)

        monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
        try:
            sandbox = gs.init_repo(tmp_path / "sandbox")
            (sandbox / "fixture.txt").write_text("fixture", encoding="utf-8")
            gs.commit_all(sandbox, "fixture commit")
        finally:
            monkeypatch.delenv("GIT_DIR", raising=False)

        after = gs.snapshot_repo_state(victim)
        assert not gs.diff_snapshots(before, after)["changed"], (
            "a leaked GIT_DIR reached the victim repository"
        )
        assert gs.git_toplevel(sandbox) == str(sandbox)


# ---------------------------------------------------------------------------
# AC2 — snapshot / diff contract
# ---------------------------------------------------------------------------


class TestSnapshotContract:
    def _repo(self, tmp_path: Path) -> Path:
        repo = gs.init_repo(tmp_path / "repo")
        (repo / "tracked.txt").write_text("tracked", encoding="utf-8")
        gs.commit_all(repo, "init")
        return repo

    def test_unchanged_repo_reports_nothing(self, tmp_path):
        repo = self._repo(tmp_path)
        before = gs.snapshot_repo_state(repo)
        after = gs.snapshot_repo_state(repo)
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_added_ref_is_reported(self, tmp_path):
        repo = self._repo(tmp_path)
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["branch", "feature-x"], check=True)
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert "refs/heads/feature-x" in diff["refs"]["added"]

    def test_deleted_ref_is_reported(self, tmp_path):
        repo = self._repo(tmp_path)
        gs.run_git(repo, ["branch", "doomed"], check=True)
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["branch", "-D", "doomed"], check=True)
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert "refs/heads/doomed" in diff["refs"]["removed"]

    def test_moved_ref_is_reported(self, tmp_path):
        repo = self._repo(tmp_path)
        before = gs.snapshot_repo_state(repo)
        (repo / "tracked.txt").write_text("moved", encoding="utf-8")
        gs.commit_all(repo, "advance dev")
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert "refs/heads/dev" in diff["refs"]["moved"]

    def test_changed_config_key_is_reported(self, tmp_path):
        repo = self._repo(tmp_path)
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["config", "--local", "user.name", "intruder"], check=True)
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert "user.name" in diff["config"]["changed"]

    def test_deleted_tracked_file_is_reported(self, tmp_path):
        repo = self._repo(tmp_path)
        before = gs.snapshot_repo_state(repo)
        (repo / "tracked.txt").unlink()
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert diff["changed"]
        assert "tracked.txt" in gs.describe_diff(diff)

    def test_added_untracked_file_is_reported(self, tmp_path):
        repo = self._repo(tmp_path)
        before = gs.snapshot_repo_state(repo)
        (repo / "untracked.txt").write_text("new", encoding="utf-8")
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert "untracked.txt" in diff["status"]["added"]

    def test_worklog_data_refs_are_ignored(self, tmp_path):
        """Worklog tooling refs (refs/worklog/*) are excluded from the surface."""
        repo = self._repo(tmp_path)
        gs.run_git(
            repo,
            ["update-ref", "refs/worklog/remotes/origin/worklog/data", "HEAD"],
            check=True,
        )
        before = gs.snapshot_repo_state(repo)
        assert "refs/worklog/remotes/origin/worklog/data" not in before["refs"]
        gs.run_git(
            repo,
            ["update-ref", "-d", "refs/worklog/remotes/origin/worklog/data"],
            check=True,
        )
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert not diff["changed"]

    def test_non_git_root_is_a_noop(self, tmp_path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        before = gs.snapshot_repo_state(plain)
        assert before["git"] is False
        (plain / "file.txt").write_text("x", encoding="utf-8")
        after = gs.snapshot_repo_state(plain)
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_describe_diff_names_the_changes(self, tmp_path):
        repo = self._repo(tmp_path)
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["branch", "feature-x"], check=True)
        text = gs.describe_diff(gs.diff_snapshots(before, gs.snapshot_repo_state(repo)))
        assert "refs/heads/feature-x" in text
