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
  neither the fingerprint nor the diff (SA-0MUINEW6X0034C65);
- ownership-based exclusion (SA-0MV8311B3005DNG8): a ref checked out in a
  registered worktree **other than the guard's own root** is invisible to the
  guard, while the guard root's own branch stays monitored. The
  new-behaviour tests are red-for-the-right-reason via ``xfail(strict=True)``
  (a tracer bullet, same convention as
  ``tests/test_repository_override_isolation.py``) until the implementation
  sibling SA-0MV87GC8O006RHS8 removes the markers.
- concurrency ref-namespace exclusion (WL-0MV5MYBKJ0033P8U): moving only
  ``refs/heads/dev``, only ``refs/remotes/origin/dev`` or any
  ``refs/remotes/origin/*`` ref (a sibling ``finish`` fast-forward, a
  concurrent fetch/push on a multi-agent host) is invisible to
  ``diff_snapshots`` and ``fingerprint``, while every other mutation (new /
  removed / moved non-excluded ref, local-config rewrite, working-tree
  change) is still reported. The new-behaviour tests are red-for-the-right-
  reason via ``xfail(strict=True)`` until the implementation sibling
  WL-0MV9820OB0041PPO removes the markers.
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

    def test_snapshot_from_linked_worktree_excludes_sibling_branches(self, tmp_path):
        """A snapshot armed *from a linked worktree* (the guard root) excludes
        sibling worktree branches but keeps its own root branch monitored.

        Ownership-based exclusion (SA-0MV8311B3005DNG8 AC1) replaced the
        ``.worklog/worktrees/`` directory anchor: ``wl-a`` is the guard root
        here, so it stays monitored, while sibling ``wl-b`` is another
        registered worktree and is excluded — the same SA-0MUILGH5E007X627
        sibling-churn invisibility, expressed via ownership.
        """
        _repo, guarded, sibling = self._repo_with_two_worktrees(tmp_path)
        (sibling / "sibling.txt").write_text("sibling", encoding="utf-8")
        gs.commit_all(sibling, "sibling work")
        snapshot = gs.snapshot_repo_state(guarded)
        assert snapshot["root"] == str(guarded.resolve())
        assert "refs/heads/wl-a" not in snapshot["excluded_refs"]
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
# SA-0MV8311B3005DNG8 — ownership-based exclusion (guard root vs other worktrees)
#
# Ownership-based exclusion: a ref checked out in a registered worktree **other
# than the guard's own root** is concurrent-infrastructure churn, invisible to
# ``fingerprint()`` / ``diff_snapshots()``. The guard root's own branch stays
# monitored, and a register→remove cycle wholly inside the window is invisible
# via the retained ``branch.<name>.remote=.`` worktree-track config.
# ---------------------------------------------------------------------------


def _repo_with_linked_guard_worktree(
    tmp_path: Path, *, guard_branch: str = "wl-guard"
) -> tuple[Path, Path]:
    """A main checkout on ``dev`` plus a linked guard worktree.

    Mirrors the shape ``implement.py`` creates: the main checkout owns
    ``dev``, and the linked worktree (the guard root) lives under the main
    checkout's ``.worklog/worktrees/``. Returns ``(main_checkout, guard_root)``.
    """
    repo = gs.init_repo(tmp_path / "main", default_branch="dev")
    (repo / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    gs.commit_all(repo, "init")
    guarded = repo / ".worklog" / "worktrees" / "wl-guard"
    gs.add_worktree(repo, guarded, guard_branch)
    return repo, guarded


class TestOwnershipBasedExclusion:
    """A ref owned by *another* registered worktree is churn, not a mutation.

    The guard root's own branch stays monitored; every ref checked out in a
    different registered worktree (the main checkout's integration branch,
    a sibling anywhere, a transient ``.worklog/tmp-worktree-*``) is excluded
    regardless of directory name. Red for the right reason pre-fix.
    """

    def test_linked_worktree_root_excludes_other_checkouts_not_its_own(
        self, tmp_path
    ):
        """AC1/AC2: armed at a linked worktree, the main checkout's ``dev`` is
        excluded, while the guard root's own branch stays monitored."""
        _repo, guarded = _repo_with_linked_guard_worktree(tmp_path)
        snapshot = gs.snapshot_repo_state(guarded)
        assert "refs/heads/dev" in snapshot["excluded_refs"]
        assert "refs/heads/wl-guard" not in snapshot["excluded_refs"]

    def test_sibling_worktree_branch_excluded_outside_agent_dir(self, tmp_path):
        """AC3: exclusion is ownership-based, not bound to a directory name.

        A sibling worktree registered *outside* ``.worklog/worktrees/`` must
        still be excluded, and its commit must not trip the diff."""
        repo, guarded = _repo_with_linked_guard_worktree(tmp_path)
        sibling = tmp_path / "not-under-worklog" / "sibling"
        gs.add_worktree(repo, sibling, "sibling-branch")
        before = gs.snapshot_repo_state(guarded)
        assert "refs/heads/sibling-branch" in before["excluded_refs"]
        (sibling / "sibling.txt").write_text("sibling\n", encoding="utf-8")
        gs.commit_all(sibling, "sibling work")
        after = gs.snapshot_repo_state(guarded)
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_tmp_worktree_under_worklog_is_excluded(self, tmp_path):
        """AC4: ``.worklog/tmp-worktree-*`` is churn even though it is not
        under the ``.worklog/worktrees/`` anchor."""
        repo, guarded = _repo_with_linked_guard_worktree(tmp_path)
        tmp_wt = repo / ".worklog" / "tmp-worktree-abc123"
        gs.add_worktree(repo, tmp_wt, "tmp-branch")
        before = gs.snapshot_repo_state(guarded)
        assert "refs/heads/tmp-branch" in before["excluded_refs"]
        (tmp_wt / "tmp.txt").write_text("tmp\n", encoding="utf-8")
        gs.commit_all(tmp_wt, "tmp work")
        after = gs.snapshot_repo_state(guarded)
        assert not gs.diff_snapshots(before, after)["changed"]

    def test_register_remove_cycle_wholly_inside_window_is_invisible(
        self, tmp_path
    ):
        """A register→remove cycle that starts and ends inside the window.

        A ``--track`` branch is created and removed entirely between *before*
        and *after*; the branch and its tracking config persist. Neither the
        diff nor the fingerprint may move."""
        repo, guarded = _repo_with_linked_guard_worktree(tmp_path)
        cycle_wt = repo / ".worklog" / "tmp-worktree-in-window"
        before = gs.snapshot_repo_state(guarded)
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "window-branch",
                str(cycle_wt),
                "dev",
            ],
            check=True,
        )
        gs.run_git(repo, ["worktree", "remove", "--force", str(cycle_wt)], check=True)
        after = gs.snapshot_repo_state(guarded)
        assert "refs/heads/window-branch" in after["refs"]  # branch retained
        assert not gs.diff_snapshots(before, after)["changed"]
        assert gs.fingerprint(after) == gs.fingerprint(before)


class TestOwnershipExclusionBoundaries:
    """Limits of the ownership rule that must never false-fail.

    These pin (a) the guard's own checkout and (b) a worktree registered and
    removed within the window. They are green against the unmodified module
    and must stay green after the ownership fix — an unexpected pass under a
    strict ``xfail`` would itself be a failure, so they are unmarked.
    """

    def test_main_checkout_root_keeps_its_own_branch_monitored(self, tmp_path):
        """AC2: armed at the main checkout, its own ``dev`` is not excluded."""
        repo, _guarded = _repo_with_linked_guard_worktree(tmp_path)
        snapshot = gs.snapshot_repo_state(repo)
        assert "refs/heads/dev" not in snapshot["excluded_refs"]

    def test_worktree_registered_then_removed_with_branch_retained(
        self, tmp_path
    ):
        """AC5: a ``--track`` worktree registered at *before* and removed by
        *after* (branch retained) moves neither the diff nor the fingerprint.

        The branch's ``branch.<name>.remote``/``.merge`` config survives the
        removal, so the ownership signal must as well."""
        repo, guarded = _repo_with_linked_guard_worktree(tmp_path)
        cycle_wt = repo / ".worklog" / "tmp-worktree-cycle"
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "cycle-branch",
                str(cycle_wt),
                "dev",
            ],
            check=True,
        )
        before = gs.snapshot_repo_state(guarded)
        gs.run_git(repo, ["worktree", "remove", "--force", str(cycle_wt)], check=True)
        after = gs.snapshot_repo_state(guarded)
        assert "refs/heads/cycle-branch" in after["refs"]  # branch retained
        assert not gs.diff_snapshots(before, after)["changed"]
        assert gs.fingerprint(after) == gs.fingerprint(before)


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
        """A moved **non-excluded** ref is still reported.

        Uses ``feature-x`` rather than ``dev``: ``refs/heads/dev`` is a
        concurrency-excluded namespace (WL-0MV5MYBKJ0033P8U) whose movement is
        invisible by design — see ``TestConcurrencyExcludedRefs``.
        """
        repo = self._repo(tmp_path)
        gs.run_git(repo, ["branch", "feature-x"], check=True)
        before = gs.snapshot_repo_state(repo)
        _advance_ref(repo, "refs/heads/feature-x")
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert "refs/heads/feature-x" in diff["refs"]["moved"]

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


# ---------------------------------------------------------------------------
# WL-0MV5MYBKJ0033P8U — concurrent dev / remote-tracking ref movement is
# invisible; every other mutation is still reported
# ---------------------------------------------------------------------------


def _advance_ref(repo: Path, ref: str) -> None:
    """Move *ref* to a fresh tree-identical commit without touching the worktree.

    A ``commit-tree`` child with the same tree means ONLY the ref moves — the
    working tree and index stay clean, so a snapshot diff attributes the change
    entirely to the ref (deterministic adversary/movement fixture).
    """
    parent = gs.run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
    tree = gs.run_git(repo, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
    fresh = gs.run_git(
        repo, ["commit-tree", tree, "-p", parent, "-m", "advance"], check=True
    ).stdout.strip()
    gs.run_git(repo, ["update-ref", ref, fresh], check=True)


def _repo_with_origin(tmp_path: Path, name: str = "repo") -> Path:
    """A dev-checkout repo with an origin remote and remote-tracking refs."""
    repo = gs.init_repo(tmp_path / name, default_branch="dev")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    gs.commit_all(repo, "init")
    origin = gs.init_bare_remote(tmp_path / "origin.git")
    gs.add_remote(repo, "origin", str(origin))
    gs.run_git(repo, ["push", "-u", "origin", "dev"], check=True)
    gs.run_git(repo, ["update-ref", "refs/remotes/origin/feature-x", "HEAD"], check=True)
    return repo


#: Red tracer bullet — see the module docstring. The implementation sibling
#: WL-0MV9820OB0041PPO removes this marker once ``refs/heads/dev`` and
#: ``refs/remotes/origin/*`` are excluded from the diff/fingerprint.
_concurrency_xfail = pytest.mark.xfail(
    strict=True,
    reason=(
        "concurrent dev/remote-tracking ref exclusion not yet implemented "
        "(WL-0MV9820OB0041PPO)"
    ),
)


class TestConcurrencyExcludedRefs:
    """Concurrent dev / origin ref movement is invisible; other mutation still fires.

    AC1/AC2 (parent WL-0MV5MYBKJ0033P8U): moving only ``refs/heads/dev``, only
    ``refs/remotes/origin/dev`` or only an arbitrary ``refs/remotes/origin/*``
    yields ``changed == false`` from both ``diff_snapshots`` and ``fingerprint``.
    AC3: new/removed/moved non-excluded refs, a local-config rewrite and a
    working-tree change each still report ``changed == true``.
    """

    @_concurrency_xfail
    def test_dev_ref_move_is_invisible(self, tmp_path):
        """AC1: only refs/heads/dev moved → no diff, equal fingerprint."""
        repo = gs.init_repo(tmp_path / "main", default_branch="dev")
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        gs.commit_all(repo, "init")
        before = gs.snapshot_repo_state(repo)
        _advance_ref(repo, "refs/heads/dev")
        after = gs.snapshot_repo_state(repo)
        assert after["refs"]["refs/heads/dev"] != before["refs"]["refs/heads/dev"]
        assert not gs.diff_snapshots(before, after)["changed"]
        assert gs.fingerprint(after) == gs.fingerprint(before)

    @_concurrency_xfail
    def test_origin_dev_ref_move_is_invisible(self, tmp_path):
        """AC2: only refs/remotes/origin/dev moved → no diff, equal fingerprint."""
        repo = _repo_with_origin(tmp_path)
        before = gs.snapshot_repo_state(repo)
        _advance_ref(repo, "refs/remotes/origin/dev")
        after = gs.snapshot_repo_state(repo)
        assert after["refs"]["refs/remotes/origin/dev"] != before["refs"]["refs/remotes/origin/dev"]
        assert not gs.diff_snapshots(before, after)["changed"]
        assert gs.fingerprint(after) == gs.fingerprint(before)

    @_concurrency_xfail
    def test_arbitrary_origin_ref_move_is_invisible(self, tmp_path):
        """AC2: only an arbitrary refs/remotes/origin/* ref moved → no diff."""
        repo = _repo_with_origin(tmp_path, "repo2")
        before = gs.snapshot_repo_state(repo)
        _advance_ref(repo, "refs/remotes/origin/feature-x")
        after = gs.snapshot_repo_state(repo)
        assert after["refs"]["refs/remotes/origin/feature-x"] != before["refs"]["refs/remotes/origin/feature-x"]
        assert not gs.diff_snapshots(before, after)["changed"]
        assert gs.fingerprint(after) == gs.fingerprint(before)

    def test_new_non_excluded_branch_still_reported(self, tmp_path):
        """AC3: a new non-excluded branch (fixture branch) still fails the diff."""
        repo = _repo_with_origin(tmp_path, "repo3")
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["branch", "feature-x"], check=True)
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert diff["changed"]
        assert "refs/heads/feature-x" in diff["refs"]["added"]

    def test_removed_non_excluded_ref_still_reported(self, tmp_path):
        """AC3: removing a non-excluded ref still fails the diff."""
        repo = _repo_with_origin(tmp_path, "repo4")
        gs.run_git(repo, ["branch", "doomed"], check=True)
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["branch", "-D", "doomed"], check=True)
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert diff["changed"]
        assert "refs/heads/doomed" in diff["refs"]["removed"]

    def test_local_config_rewrite_still_reported(self, tmp_path):
        """AC3: a non-excluded local-config rewrite still fails the diff."""
        repo = _repo_with_origin(tmp_path, "repo5")
        before = gs.snapshot_repo_state(repo)
        gs.run_git(repo, ["config", "--local", "user.name", "intruder"], check=True)
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert diff["changed"]
        assert "user.name" in diff["config"]["changed"]

    def test_working_tree_change_still_reported(self, tmp_path):
        """AC3: a working-tree change still fails the diff."""
        repo = _repo_with_origin(tmp_path, "repo6")
        before = gs.snapshot_repo_state(repo)
        (repo / "intruder.txt").write_text("x", encoding="utf-8")
        diff = gs.diff_snapshots(before, gs.snapshot_repo_state(repo))
        assert diff["changed"]
        assert "intruder.txt" in diff["status"]["added"]
