"""Regression tests: verified, reversible local-dev sync + ancestry assertion.

Feature: SA-0MVPZG7A0000N66U (parent SA-0MVNQURZM001TKJY AC1/AC2).

``phase_finish`` advances the **shared** main-checkout ``dev`` before pushing
(so the dev-push gate measures the new files) and then pushes the feature
branch to ``origin/dev``. Two failure modes used to be silent:

1. when the advance was impossible, a broad ``except Exception`` disabled it
   and the push proceeded from an unidentified checkout — and a *failed* push
   left local ``dev`` pinned at the unpushed commit, so the next finish (or
   the next child worktree) forked from a base ``origin/dev`` did not have;
2. ``finish`` trusted the push's exit status, so a commit that never reached
   ``origin/dev`` could still be reported as success.

Coverage (per-helper, hermetic bare-remote + clone sandbox):

- AC1 — :func:`_fast_forward_local_dev_for_push` only moves local ``dev`` by
  fast-forward, records the pre-advance tip, and refuses **loudly** (naming
  refs and hashes) when local ``dev`` has diverged or the commit is not based
  on the remote tip.
- AC1 — :func:`_restore_local_dev_after_failed_push` returns local ``dev`` to
  ``refs/remotes/origin/dev`` after a failed push, never clobbers a branch
  another actor moved, and keeps uncommitted work in the shared checkout.
- AC1 — the post-push :func:`_sync_parent_branch` target is the
  remote-tracking ref (remote-verified by construction).
- AC2 — :func:`_assert_commit_on_origin_dev` verifies a published commit and
  raises :class:`AncestryAssertionError` — naming the hash — for a commit
  that did not land, and for a commit that cannot be verified.

The live checkout's refs, worktrees and worklog are never touched; the
sandbox neutralises repository-overriding git environment variables.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from shared import git_sandbox as gs

_BRANCH = "wl-SA-0TESTVERIFIED"


@pytest.fixture(scope="module")
def implement_mod():
    """Load implement.py as a module for direct helper testing."""
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_local_dev_verified", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_local_dev_verified"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A bare ``origin``, a main checkout on ``dev`` and one feature worktree.

    The feature worktree is forked from ``dev`` before anything is pushed, so
    the finish commit is a direct child of the published base — the shape
    ``phase_finish`` Step 5 fast-forwards.
    """
    for var in gs.REPOSITORY_OVERRIDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)

    remote = gs.init_bare_remote(tmp_path / "remote.git")
    work = gs.init_repo(tmp_path / "work", default_branch="dev")
    (work / "README.md").write_text("# base\n", encoding="utf-8")
    base = gs.commit_all(work, "Initial commit")
    gs.add_remote(work, "origin", str(remote))
    gs.run_git(work, ["push", "-q", "-u", "origin", "dev"], check=True)

    wt = gs.add_worktree(work, tmp_path / "wt", _BRANCH, base="dev")
    (wt / "feature.txt").write_text("feature\n", encoding="utf-8")
    feature = gs.commit_all(wt, "Add feature")

    return {
        "tmp": tmp_path,
        "remote": remote,
        "work": work,
        "wt": wt,
        "base": base,
        "feature": feature,
    }


def _rev(cwd, ref: str) -> str:
    return gs.run_git(cwd, ["rev-parse", ref], check=True).stdout.strip()


def _local_dev(sandbox: dict) -> str:
    return _rev(sandbox["work"], "refs/heads/dev")


def _origin_dev(sandbox: dict) -> str:
    return _rev(sandbox["work"], "refs/remotes/origin/dev")


def _push_origin_dev(sandbox: dict) -> None:
    """Publish the feature commit to ``origin/dev`` through a real push."""
    gs.run_git(
        sandbox["wt"],
        ["push", "-q", "origin", f"{_BRANCH}:refs/heads/dev"],
        check=True,
    )


def _advance_origin_dev(sandbox: dict) -> str:
    """Push an unrelated sibling commit to ``origin/dev`` from the main clone."""
    work = sandbox["work"]
    (work / "sibling.txt").write_text("sibling\n", encoding="utf-8")
    sibling = gs.commit_all(work, "Sibling commit")
    gs.run_git(work, ["push", "-q", "origin", "dev"], check=True)
    return sibling


# ---------------------------------------------------------------------------
# AC1 — verified pre-push fast-forward
# ---------------------------------------------------------------------------


class TestFastForwardLocalDev:
    def test_advances_local_dev_and_records_the_pre_advance_tip(
        self, implement_mod, sandbox
    ):
        """A safe advance fast-forwards local dev and stays reversible."""
        before = _local_dev(sandbox)

        result = implement_mod._fast_forward_local_dev_for_push(
            str(sandbox["work"]), sandbox["feature"]
        )

        assert result["method"] == "advanced", result
        assert result["advanced"] is True
        assert result["pre_advance_tip"] == before
        assert result["advanced_to"] == sandbox["feature"]
        assert _local_dev(sandbox) == sandbox["feature"]
        # The main checkout really carries the new files: that is what the
        # pre-push context-budget gate measures.
        assert (sandbox["work"] / "feature.txt").read_text() == "feature\n"
        # The advance's own churn is attributable to finish.
        assert "feature.txt" in result["own_churn"]

    def test_refuses_loudly_when_local_dev_has_moved(
        self, implement_mod, sandbox
    ):
        """A diverged local dev is never advanced; the refusal names hashes."""
        work = sandbox["work"]
        (work / "local-only.txt").write_text("local\n", encoding="utf-8")
        diverged_tip = gs.commit_all(work, "Local-only commit")

        result = implement_mod._fast_forward_local_dev_for_push(
            str(work), sandbox["feature"]
        )

        assert result["method"] == "refused", result
        assert result["reason"] == "local_dev_diverged", result
        assert result["success"] is True, "a refusal must not fail the finish"
        assert diverged_tip in result["warning"]
        assert result["remote_tip"] in result["warning"]
        assert _local_dev(sandbox) == diverged_tip, (
            "the shared checkout must be left untouched on refusal"
        )

    def test_refuses_loudly_when_the_commit_is_not_based_on_the_remote_tip(
        self, implement_mod, sandbox
    ):
        """A sibling that landed first leaves local dev at the remote tip."""
        sibling = _advance_origin_dev(sandbox)

        result = implement_mod._fast_forward_local_dev_for_push(
            str(sandbox["work"]), sandbox["feature"]
        )

        assert result["method"] == "refused", result
        assert result["reason"] == "commit_not_based_on_remote_tip", result
        assert sandbox["feature"] in result["warning"]
        assert sibling in result["warning"]
        assert _local_dev(sandbox) == sibling, (
            "local dev must sit on origin/dev, never on an unpushed commit"
        )

    def test_refuses_loudly_when_the_remote_is_unreachable(
        self, implement_mod, sandbox
    ):
        """An unverifiable remote tip means no advance (and no failure)."""
        work = sandbox["work"]
        before = _local_dev(sandbox)
        gs.add_remote(work, "origin", str(sandbox["tmp"] / "missing.git"))

        result = implement_mod._fast_forward_local_dev_for_push(
            str(work), sandbox["feature"]
        )

        assert result["method"] == "refused", result
        assert result["reason"] == "fetch_failed", result
        assert result["success"] is True
        assert sandbox["feature"] in result["warning"]
        assert _local_dev(sandbox) == before


# ---------------------------------------------------------------------------
# AC1 — reversible: restore after a failed push
# ---------------------------------------------------------------------------


class TestRestoreAfterFailedPush:
    def test_restores_local_dev_to_the_remote_tip(self, implement_mod, sandbox):
        """A failed push never leaves local dev on the unpushed commit."""
        fast_forward = implement_mod._fast_forward_local_dev_for_push(
            str(sandbox["work"]), sandbox["feature"]
        )
        assert _local_dev(sandbox) == sandbox["feature"]

        result = implement_mod._restore_local_dev_after_failed_push(
            str(sandbox["work"]), fast_forward
        )

        assert result["restored"] is True, result
        assert _local_dev(sandbox) == _origin_dev(sandbox) == sandbox["base"]
        # The commit itself is not lost: it stays on the feature branch.
        assert _rev(sandbox["work"], f"refs/heads/{_BRANCH}") == sandbox["feature"]
        assert not (sandbox["work"] / "feature.txt").exists()

    def test_keeps_uncommitted_work_in_the_shared_checkout(
        self, implement_mod, sandbox
    ):
        """Restoring must never discard the operator's uncommitted changes."""
        work = sandbox["work"]
        fast_forward = implement_mod._fast_forward_local_dev_for_push(
            str(work), sandbox["feature"]
        )
        (work / "README.md").write_text("# base\noperator edit\n", encoding="utf-8")
        (work / "notes.txt").write_text("scratch\n", encoding="utf-8")

        result = implement_mod._restore_local_dev_after_failed_push(
            str(work), fast_forward
        )

        assert result["restored"] is True, result
        assert _local_dev(sandbox) == sandbox["base"]
        assert (work / "README.md").read_text().endswith("operator edit\n")
        assert (work / "notes.txt").read_text() == "scratch\n"

    def test_never_clobbers_a_branch_another_actor_moved(
        self, implement_mod, sandbox
    ):
        """Only finish's own advance is undone, and the skip is loud."""
        work = sandbox["work"]
        fast_forward = implement_mod._fast_forward_local_dev_for_push(
            str(work), sandbox["feature"]
        )
        (work / "other.txt").write_text("other\n", encoding="utf-8")
        moved_tip = gs.commit_all(work, "Another actor's commit")

        result = implement_mod._restore_local_dev_after_failed_push(
            str(work), fast_forward
        )

        assert result["restored"] is False
        assert result["reason"] == "branch_moved_since_advance", result
        assert moved_tip in result["warning"]
        assert _local_dev(sandbox) == moved_tip, "the other actor's move stands"

    def test_conflicting_local_change_is_reported_not_clobbered(
        self, implement_mod, sandbox
    ):
        """A local edit overlapping the commit being undone aborts the restore."""
        work = sandbox["work"]
        fast_forward = implement_mod._fast_forward_local_dev_for_push(
            str(work), sandbox["feature"]
        )
        (work / "feature.txt").write_text("operator edit\n", encoding="utf-8")

        result = implement_mod._restore_local_dev_after_failed_push(
            str(work), fast_forward
        )

        assert result["restored"] is False
        assert result["reason"] == "reset_conflict", result
        assert "operator edit" in (work / "feature.txt").read_text()
        assert "git -C" in result["warning"], "the refusal must say how to recover"

    def test_no_advance_means_nothing_to_restore(self, implement_mod, sandbox):
        """A refused fast-forward leaves nothing to undo."""
        refused = {"method": "refused", "advanced": False, "success": True}

        result = implement_mod._restore_local_dev_after_failed_push(
            str(sandbox["work"]), refused
        )

        assert result["restored"] is False
        assert result["reason"] == "no_advance_to_undo"
        assert _local_dev(sandbox) == sandbox["base"]


# ---------------------------------------------------------------------------
# AC1 — post-push sync is remote-verified by construction
# ---------------------------------------------------------------------------


def test_post_push_sync_targets_the_remote_tracking_ref(implement_mod, sandbox):
    """The post-push local-dev update only fast-forwards to origin/dev."""
    _push_origin_dev(sandbox)

    result = implement_mod._sync_parent_branch(str(sandbox["work"]), "dev")

    assert result["method"] == "synced", result
    assert result["target"] == "refs/remotes/origin/dev", result
    assert _local_dev(sandbox) == _origin_dev(sandbox) == sandbox["feature"]


# ---------------------------------------------------------------------------
# AC2 — post-push ancestry assertion
# ---------------------------------------------------------------------------


class TestAncestryAssertion:
    def test_verifies_a_published_commit(self, implement_mod, sandbox):
        """A commit that reached origin/dev passes the assertion."""
        _push_origin_dev(sandbox)

        result = implement_mod._assert_commit_on_origin_dev(
            str(sandbox["work"]), sandbox["feature"]
        )

        assert result["verified"] is True, result
        assert result["resolved_commit"] == sandbox["feature"]
        assert result["origin_tip"] == sandbox["feature"]
        assert result["target"] == "refs/remotes/origin/dev"

    def test_accepts_the_short_hash_reported_by_finish(
        self, implement_mod, sandbox
    ):
        """The short hash ``git_get_commit_hash`` returns is resolved first."""
        _push_origin_dev(sandbox)

        result = implement_mod._assert_commit_on_origin_dev(
            str(sandbox["work"]), sandbox["feature"][:7]
        )

        assert result["verified"] is True, result
        assert result["resolved_commit"] == sandbox["feature"]

    def test_raises_naming_the_hash_when_the_commit_did_not_land(
        self, implement_mod, sandbox
    ):
        """An unpublished commit can never be reported as success."""
        with pytest.raises(implement_mod.AncestryAssertionError) as exc_info:
            implement_mod._assert_commit_on_origin_dev(
                str(sandbox["work"]), sandbox["feature"]
            )

        message = str(exc_info.value)
        assert sandbox["feature"] in message, message
        assert "refs/remotes/origin/dev" in message, message
        assert sandbox["feature"][:7] in message, message
        assert exc_info.value.resolved_commit == sandbox["feature"]
        assert exc_info.value.target_tip == sandbox["base"]

    def test_raises_when_the_commit_cannot_be_resolved(
        self, implement_mod, sandbox
    ):
        """An unverifiable commit is a refusal, never a pass."""
        with pytest.raises(implement_mod.AncestryAssertionError) as exc_info:
            implement_mod._assert_commit_on_origin_dev(
                str(sandbox["work"]), "0" * 40
            )

        assert "could not be resolved" in str(exc_info.value)

    def test_is_a_runtime_error_so_finish_resets_to_open(
        self, implement_mod
    ):
        """``AncestryAssertionError`` flows through finish's failure handler."""
        assert issubclass(implement_mod.AncestryAssertionError, RuntimeError)
