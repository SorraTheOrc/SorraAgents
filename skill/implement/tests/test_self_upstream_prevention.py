"""Regression tests: the framework's write paths can never put ``remote=.``
on a long-lived branch (SA-0MVX35ABT000I1PD — child 4 of SA-0MVX1M6HH004YPX2).

The defect: ``branch.<name>.remote = "."`` on ``dev``/``main`` makes a plain
``git pull`` fetch from the same repository and silently change nothing. Two
framework write paths could record it (parent AC3):

1. ``git worktree add --track -b wl-* <path> <parent>`` — legitimately writes
   ``"."`` for the new ``wl-*`` branch (the worktree-ownership signal), and
   *must never* touch the parent branch's key;
2. a ``git push`` whose remote is the local path form (``.``) — which is what
   ``git push -u`` uses to record ``branch.<name>.remote = "."``.

Pinned here:

* a real worktree registration through implement.py's own
  :func:`git_worktree_add` leaves ``dev``/``main`` tracking at ``origin`` and
  gives the ``wl-*`` branch ``"."``;
* :func:`assert_parent_tracking_untouched` fails loudly (hard error, not a
  warning) exactly when the registration changed the parent's ``remote`` to
  ``"."`` — a pre-existing ``"."`` (someone else's defect) is detection's job,
  not prevention's, and must not block worktree creation;
* :func:`git_push_to_dev` resolves through the configured remote **name**
  (``"origin"``), and the push-remote guard refuses ``"."`` / path / URL
  forms outright, so no framework push can record a self-upstream.

All fixtures are deterministic, network-free scratch repositories (a bare
local ``origin``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

_REPO_ROOT_FOR_SHARED = REPO_ROOT / "skill"
if str(_REPO_ROOT_FOR_SHARED) not in sys.path:
    sys.path.append(str(_REPO_ROOT_FOR_SHARED))

from shared import git_sandbox as gs


def _repo_with_origin(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    """A ``dev`` checkout whose ``dev``/``main`` track a bare local origin."""
    repo = gs.init_repo(tmp_path / name, default_branch="dev")
    gs.run_git(repo, ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
    origin = gs.init_bare_remote(tmp_path / f"{name}-origin.git")
    gs.add_remote(repo, "origin", str(origin))
    gs.run_git(repo, ["push", "-q", "-u", "origin", "dev"], check=True)
    gs.run_git(repo, ["branch", "main"], check=True)
    gs.run_git(repo, ["push", "-q", "-u", "origin", "main"], check=True)
    return repo, origin


def _tracking(repo: Path, branch: str) -> tuple[str | None, str | None]:
    remote = gs.run_git(repo, ["config", "--get", f"branch.{branch}.remote"]).stdout
    merge = gs.run_git(repo, ["config", "--get", f"branch.{branch}.merge"]).stdout
    return (remote.strip() or None, merge.strip() or None)


# ---------------------------------------------------------------------------
# AC1a — worktree registration never corrupts the parent's tracking config
# ---------------------------------------------------------------------------


class TestWorktreeRegistrationDoesNotPoisonParent:
    """``git worktree add --track`` through implement.py's own function.

    :func:`implement.git_worktree_add` runs ``git`` in the caller's cwd
    (no repo argument), so each test chdirs into the scratch repo first.
    """

    def _add_worktree(self, repo: Path, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(repo)
        assert implement.git_worktree_add(
            "wl-SA-TEST-prevent", str(tmp_path / "wt"), "dev"
        ), "worktree registration must succeed"

    def test_wl_branch_gets_dot_remote(self, tmp_path, monkeypatch):
        """The new wl-* branch records remote = . (the ownership signal)."""
        repo, _origin = _repo_with_origin(tmp_path)
        self._add_worktree(repo, tmp_path, monkeypatch)
        assert _tracking(repo, "wl-SA-TEST-prevent") == (".", "refs/heads/dev")

    def test_parent_dev_tracking_is_untouched(self, tmp_path, monkeypatch):
        """dev keeps remote = origin after the registration."""
        repo, _origin = _repo_with_origin(tmp_path)
        self._add_worktree(repo, tmp_path, monkeypatch)
        assert _tracking(repo, "dev") == ("origin", "refs/heads/dev")

    def test_parent_main_tracking_is_untouched(self, tmp_path, monkeypatch):
        """main keeps remote = origin after the registration."""
        repo, _origin = _repo_with_origin(tmp_path)
        self._add_worktree(repo, tmp_path, monkeypatch)
        assert _tracking(repo, "main") == ("origin", "refs/heads/main")


# ---------------------------------------------------------------------------
# AC1b — the post-registration guard fails loudly on parent poisoning
# ---------------------------------------------------------------------------


class TestParentTrackingGuard:
    """``assert_parent_tracking_untouched`` distinguishes registration-caused
    poisoning from a pre-existing defect."""

    def test_unchanged_tracking_passes(self, tmp_path, monkeypatch):
        """A registration that left the parent alone passes without raising."""
        repo, _origin = _repo_with_origin(tmp_path)
        before = _tracking(repo, "dev")
        implement.assert_parent_tracking_untouched(str(repo), "dev", before)
        # and a follow-up real registration still passes the guard
        monkeypatch.chdir(repo)
        assert implement.git_worktree_add(
            "wl-SA-TEST-guard", str(tmp_path / "wt-guard"), "dev"
        )
        implement.assert_parent_tracking_untouched(str(repo), "dev", before)

    def test_poisoned_parent_raises_loudly(self, tmp_path):
        """remote becoming '.' is a hard failure naming the branch and shape."""
        repo, _origin = _repo_with_origin(tmp_path)
        before = _tracking(repo, "dev")  # ("origin", "refs/heads/dev")
        gs.run_git(repo, ["config", "branch.dev.remote", "."], check=True)

        with pytest.raises(RuntimeError) as exc_info:
            implement.assert_parent_tracking_untouched(str(repo), "dev", before)
        message = str(exc_info.value)
        assert "'dev'" in message
        assert "'.'" in message
        assert "origin" in message, "the remediation must name the repair"

    def test_pre_existing_defect_is_not_a_registration_failure(self, tmp_path, monkeypatch):
        """A parent already at '.' before the registration is not poisoned by
        it — that defect is detection's job (SA-0MVX359B7008R9F3), never a
        prevention guard's, and must not block worktree creation."""
        repo, _origin = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["config", "branch.dev.remote", "."], check=True)
        before = _tracking(repo, "dev")  # ("." , "refs/heads/dev")

        implement.assert_parent_tracking_untouched(str(repo), "dev", before)
        # a registration on top of the defect is equally safe to run
        monkeypatch.chdir(repo)
        assert implement.git_worktree_add(
            "wl-SA-TEST-preexisting", str(tmp_path / "wt-pre"), "dev"
        )
        implement.assert_parent_tracking_untouched(str(repo), "dev", before)


# ---------------------------------------------------------------------------
# AC1c — framework pushes resolve through the remote name, never a path form
# ---------------------------------------------------------------------------


class TestPushRemoteNameOnly:
    """The push remote guard rejects every shape that could record a
    self-referential upstream."""

    @pytest.mark.parametrize("remote", ["origin", "upstream"])
    def test_plain_remote_names_accepted(self, remote):
        implement._assert_remote_name_only(remote)  # must not raise

    @pytest.mark.parametrize(
        "remote",
        [
            ".",
            "./dev.git",
            "/tmp/some/repo",
            "file:///tmp/some/repo",
            "git@github.com:org/repo.git",
        ],
    )
    def test_path_and_url_forms_refused(self, remote):
        with pytest.raises(RuntimeError) as exc_info:
            implement._assert_remote_name_only(remote)
        message = str(exc_info.value)
        assert "remote name" in message
        assert "SA-0MVX1M6HH004YPX2" in message, (
            "the refusal must cite the defect it prevents"
        )

    def test_push_remote_constant_is_the_configured_name(self):
        """The framework's push remote is pinned to the name 'origin' — never
        a '.' or path form (AC3's 'origin'-only push requirement)."""
        assert implement.PUSH_REMOTE_NAME == "origin"

    def test_git_push_to_dev_pushes_via_origin(self, tmp_path):
        """End-to-end: git_push_to_dev pushes the branch to the configured
        origin remote and the bare remote gains the ref."""
        repo, origin = _repo_with_origin(tmp_path)
        # drop the -u self-upstream, then push dev through the framework fn
        assert implement.git_push_to_dev(str(repo), "dev", timeout=120)
        pushed = gs.run_git(repo, ["ls-remote", str(origin), "refs/heads/dev"]).stdout
        assert pushed.strip(), "git_push_to_dev must land dev on origin"
        # and no framework path could have recorded a self-upstream
        assert _tracking(repo, "dev") == ("origin", "refs/heads/dev")
