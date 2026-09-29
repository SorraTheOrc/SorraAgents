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
  (F3 AC5; parent AC3);
- agent-worktree registration/removal churn (including the
  ``branch.<name>.*`` config written by ``git worktree add --track``) moves
  neither the fingerprint nor the diff (SA-0MUINEW6X0034C65).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs


def _seeded_repo(tmp_path: Path, name: str = "main") -> Path:
    """A hermetic repo with one commit, ready for ``git worktree add``."""
    repo = gs.init_repo(tmp_path / name, default_branch="dev")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    gs.commit_all(repo, "init")
    return repo


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
# SA-0MUINEW6X0034C65 — agent-worktree churn is invisible to the guard
# ---------------------------------------------------------------------------


class TestAgentWorktreeChurn:
    """Registering/removing agent worktrees must not look like a mutation.

    Regression for the per-test guard false positive
    (SA-0MUINEW6X0034C65): the fingerprint moved when the *excluded set's
    membership* changed, while the authoritative diff stayed empty, so the
    guard failed a clean test with ``(no differences)``.
    """

    def _repo_with_agent_dir(self, tmp_path: Path) -> Path:
        repo = gs.init_repo(tmp_path / "repo")
        (repo / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        gs.commit_all(repo, "init")
        return repo

    def test_registering_worktree_moves_neither_fingerprint_nor_diff(
        self, tmp_path
    ):
        """A new excluded ref (agent worktree) is churn, not a mutation."""
        repo = self._repo_with_agent_dir(tmp_path)
        before = gs.snapshot_repo_state(repo)
        gs.add_worktree(repo, repo / ".worklog" / "worktrees" / "wl-new", "wl-new")
        after = gs.snapshot_repo_state(repo)
        assert after["excluded_refs"] > before["excluded_refs"]
        assert gs.fingerprint(after) == gs.fingerprint(before)
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_tracked_worktree_branch_config_churn_is_excluded(self, tmp_path):
        """``git worktree add --track`` config churn is excluded too.

        ``implement.py`` registers worktrees with ``--track``, which writes
        ``branch.<name>.remote`` / ``branch.<name>.merge``. Those keys belong
        to the excluded agent branch and must not be reported.
        """
        repo = self._repo_with_agent_dir(tmp_path)
        before = gs.snapshot_repo_state(repo)
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "wl-tracked",
                str(repo / ".worklog" / "worktrees" / "wl-tracked"),
                "dev",
            ],
            check=True,
        )
        after = gs.snapshot_repo_state(repo)
        assert after["excluded_refs"] > before["excluded_refs"]
        assert "branch.wl-tracked.remote" in after["config"]  # git really wrote it
        assert gs.fingerprint(after) == gs.fingerprint(before)
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_removing_worktree_with_retained_branch_reports_nothing(self, tmp_path):
        """Removal shrinks the excluded set but keeps the branch: no diff."""
        repo = self._repo_with_agent_dir(tmp_path)
        agent_wt = repo / ".worklog" / "worktrees" / "wl-agent"
        gs.add_worktree(repo, agent_wt, "wl-agent")
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["worktree", "remove", str(agent_wt)], check=True)
        after = gs.snapshot_repo_state(repo)
        assert before["excluded_refs"] > after["excluded_refs"]
        assert "refs/heads/wl-agent" in after["refs"]  # branch retained
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_existing_branch_becomes_excluded_reports_nothing(self, tmp_path):
        """A pre-existing branch gaining a worktree is churn, not a mutation.

        This is the exact ``excluded_refs`` grows / empty-diff signature from
        the SA-0MUINEW6X0034C65 report: the branch already existed, so the
        cheap fingerprint moves even though the authoritative diff is empty.
        """
        repo = self._repo_with_agent_dir(tmp_path)
        gs.run_git(repo, ["branch", "wl-existing"], check=True)
        before = gs.snapshot_repo_state(repo)
        assert "refs/heads/wl-existing" not in before["excluded_refs"]
        gs.add_worktree(
            repo,
            repo / ".worklog" / "worktrees" / "wl-existing",
            "wl-existing",
            create_branch=False,
        )
        after = gs.snapshot_repo_state(repo)
        assert "refs/heads/wl-existing" in after["excluded_refs"]
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_non_excluded_branch_config_rewrite_is_still_reported(self, tmp_path):
        """Exclusion is scoped to agent branches: other branch config fires."""
        repo = self._repo_with_agent_dir(tmp_path)
        before = gs.snapshot_repo_state(repo)
        gs.run_git(
            repo, ["config", "--local", "branch.dev.remote", "intruder"], check=True
        )
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert diff["changed"]
        assert "branch.dev.remote" in diff["config"]["added"]


class TestMainCheckoutAnchoring:
    """Snapshots taken *from a linked worktree* must exclude agent worktrees.

    ``implement.py finish`` runs the suite from a linked worktree; anchoring the
    exclusion on the passed root left ``excluded_refs`` empty there and turned
    concurrent sibling churn into a bogus mutation (SA-0MUINEW6X0034C65).
    """

    def _repo_with_two_worktrees(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        repo = gs.init_repo(tmp_path / "repo")
        (repo / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        gs.commit_all(repo, "init")
        guarded = repo / ".worklog" / "worktrees" / "wl-a"
        sibling = repo / ".worklog" / "worktrees" / "wl-b"
        gs.add_worktree(repo, guarded, "wl-a")
        gs.add_worktree(repo, sibling, "wl-b")
        return repo, guarded, sibling

    def test_main_checkout_root_resolves_from_linked_worktree(self, tmp_path):
        repo, guarded, _sibling = self._repo_with_two_worktrees(tmp_path)
        assert gs.main_checkout_root(guarded) == repo.resolve()
        assert gs.main_checkout_root(repo) == repo.resolve()

    def test_snapshot_from_linked_worktree_excludes_agent_branches(self, tmp_path):
        _repo, guarded, sibling = self._repo_with_two_worktrees(tmp_path)
        (sibling / "sibling.txt").write_text("sibling", encoding="utf-8")
        gs.commit_all(sibling, "sibling work")
        snapshot = gs.snapshot_repo_state(guarded)
        assert snapshot["root"] == str(guarded.resolve())
        assert "refs/heads/wl-a" in snapshot["excluded_refs"]
        assert "refs/heads/wl-b" in snapshot["excluded_refs"]

    def test_linked_worktree_sibling_commit_reports_nothing(self, tmp_path):
        _repo, guarded, sibling = self._repo_with_two_worktrees(tmp_path)
        before = gs.snapshot_repo_state(guarded)
        (sibling / "sibling.txt").write_text("sibling", encoding="utf-8")
        gs.commit_all(sibling, "sibling work")
        after = gs.snapshot_repo_state(guarded)
        assert gs.fingerprint(after) == gs.fingerprint(before)
        assert not gs.diff_snapshots(before, after)["changed"]


# ---------------------------------------------------------------------------
# AC1 — leaked environment is neutralised
# ---------------------------------------------------------------------------


class TestScrubRepositoryOverrides:
    """F2 (SA-0MUIULHQC005MUM0): the shared repository-override scrub helper."""

    def test_removes_every_override_when_all_present(self):
        env = {name: "poison" for name in gs.REPOSITORY_OVERRIDE_ENV_VARS}
        env["PATH"] = "/usr/bin"
        cleaned = gs.scrub_repository_overrides(env)
        for name in gs.REPOSITORY_OVERRIDE_ENV_VARS:
            assert name not in cleaned
        assert cleaned["PATH"] == "/usr/bin"

    def test_removes_only_present_overrides_and_preserves_unrelated(self):
        env = {"GIT_DIR": "/tmp/x", "HOME": "/home/u", "PATH": "/bin"}
        cleaned = gs.scrub_repository_overrides(env)
        assert cleaned == {"HOME": "/home/u", "PATH": "/bin"}

    def test_empty_environments_are_safe(self):
        assert gs.scrub_repository_overrides({}) == {}

    def test_empty_string_value_is_still_removed(self):
        cleaned = gs.scrub_repository_overrides({"GIT_DIR": "", "A": "b"})
        assert cleaned == {"A": "b"}

    def test_input_mapping_is_never_mutated(self):
        env = {"GIT_DIR": "/tmp/x", "PATH": "/bin"}
        gs.scrub_repository_overrides(env)
        assert env == {"GIT_DIR": "/tmp/x", "PATH": "/bin"}

    def test_os_environ_is_never_mutated(self, monkeypatch):
        monkeypatch.setenv("GIT_DIR", "/tmp/leaked")
        gs.scrub_repository_overrides()
        assert os.environ["GIT_DIR"] == "/tmp/leaked"

    def test_sanitized_git_env_uses_the_scrub(self, monkeypatch):
        monkeypatch.setenv("GIT_DIR", "/tmp/leaked")
        assert "GIT_DIR" not in gs.sanitized_git_env()


class TestRepositoryOverrideVarsPresent:
    """F2: the detection helper used by diagnostics and the release gate."""

    def test_reports_present_names_in_canonical_order(self):
        env = {"GIT_DIR": "x", "GIT_CONFIG": "y", "PATH": "z"}
        assert gs.repository_override_vars_present(env) == ("GIT_DIR", "GIT_CONFIG")

    def test_empty_when_none_present(self):
        assert gs.repository_override_vars_present({"PATH": "z"}) == ()

    def test_worktree_managed_git_dir_is_not_reported(self, tmp_path):
        """A worktree ``GIT_DIR`` (set by git for pre-push hooks) is allowed.

        SA-0MUMR3QPM002VK7M: the release gate must run from a linked worktree
        without the documented opt-out override.
        """
        repo = _seeded_repo(tmp_path)
        gs.add_worktree(repo, tmp_path / "wt", "feature")
        worktree_git_dir = repo / ".git" / "worktrees" / "wt"
        assert worktree_git_dir.is_dir()

        env = {"GIT_DIR": str(worktree_git_dir), "PATH": "/bin"}
        assert gs.repository_override_vars_present(env, top=repo) == ()

    def test_non_worktree_git_dir_is_still_reported(self, tmp_path):
        """A genuine leaked ``GIT_DIR`` is still refused (AC2)."""
        repo = _seeded_repo(tmp_path)

        env = {"GIT_DIR": str(repo / ".git"), "PATH": "/bin"}
        assert gs.repository_override_vars_present(env, top=repo) == ("GIT_DIR",)


class TestIsWorktreeGitDir:
    """SA-0MUMR3QPM002VK7M: recognise git's worktree-managed ``GIT_DIR``."""

    def test_true_for_a_registered_worktree_git_dir(self, tmp_path):
        repo = _seeded_repo(tmp_path)
        gs.add_worktree(repo, tmp_path / "wt", "feature")
        worktree_git_dir = repo / ".git" / "worktrees" / "wt"

        assert gs.is_worktree_git_dir(worktree_git_dir, top=repo) is True

    def test_false_for_the_main_checkout_git_dir(self, tmp_path):
        repo = gs.init_repo(tmp_path / "main", default_branch="dev")
        assert gs.is_worktree_git_dir(repo / ".git", top=repo) is False

    def test_false_for_a_leaked_absolute_path(self, tmp_path):
        repo = gs.init_repo(tmp_path / "main", default_branch="dev")
        assert gs.is_worktree_git_dir("/tmp/leaked", top=repo) is False

    def test_false_when_no_anchor_resolves(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gs, "main_checkout_root", lambda _path: None)
        assert gs.is_worktree_git_dir("/tmp/leaked") is False

    def test_resolves_anchor_from_cwd_when_top_omitted(
        self, tmp_path, monkeypatch
    ):
        """The production path resolves the main checkout from cwd itself."""
        repo = _seeded_repo(tmp_path)
        gs.add_worktree(repo, tmp_path / "wt", "feature")
        worktree_git_dir = repo / ".git" / "worktrees" / "wt"
        monkeypatch.chdir(tmp_path / "wt")

        assert gs.is_worktree_git_dir(worktree_git_dir) is True


class TestImportSurface:
    """F2 AC3: the helper is importable from every production caller."""

    def test_importable_from_test_runner_and_production_modules(self):
        from test_runner import scrub_repository_overrides

        assert scrub_repository_overrides is gs.scrub_repository_overrides
        # Import the production callers to prove no import cycle.
        import test_cache  # noqa: F401
        from audit.scripts import audit_runner  # noqa: F401
        # ``run_tests`` lives under skill/test/scripts; the skill root must
        # precede the stdlib ``test`` package for the import to resolve.
        skill_root = str(REPO_ROOT / "skill")
        while skill_root in sys.path:
            sys.path.remove(skill_root)
        sys.path.insert(0, skill_root)
        import test.scripts.run_tests  # noqa: F401


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
