"""Regression tests: ``finish`` never moves local ``dev`` while a push is in
flight, and moves it only after the push reports success.

WL-0MWFKDH78002T2CR AC1/AC2. ``phase_finish`` used to fast-forward the main
checkout's local ``dev`` to the about-to-be-pushed commit **before** the push
and restore it on failure — a write-then-restore window during which a
concurrent ``start`` resolved ``dev`` to the unpushed tip and grafted another
item's commit onto its branch.

* **AC1** — the advance is deferred until after ``git push`` reports success
  (and the post-push ancestry assertion and read-only verification pass); the
  single post-push fast-forward is the *only* local-``dev`` mutation.
* **AC2** — on a failed/refused push, local ``dev`` is provably unchanged from
  its pre-finish value: the restore reports ``no_advance_to_undo`` because
  nothing was ever advanced.

The phase-level tests drive ``phase_finish`` against real ``git`` repositories
with the local-``dev`` sync machinery running for real (only build, gate,
commit, the push boundary itself and the work-item store are stubbed).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

from skill.implement.scripts import implement
from skill.implement.tests.test_implement_push_from_worktree import (
    BRANCH,
    _build_result,
    _tests_result,
)

WORK_ITEM = "WL-TESTDEVR"


def _rev(cwd: Path, ref: str = "HEAD") -> str:
    return gs.run_git(cwd, ["rev-parse", ref], check=True).stdout.strip()


def _names_commit(candidate: str, commit: str) -> bool:
    return bool(candidate) and bool(commit) and (
        candidate.startswith(commit) or commit.startswith(candidate)
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """A scratch ``origin``, a ``dev`` main checkout and a feature worktree.

    Same shape as ``test_implement_push_from_worktree.sandbox``: ``dev``
    (main) at :data`base`, feature worktree on :data:`branch` at :data:`feature`.
    """
    remote = gs.init_bare_remote(tmp_path / "origin.git")
    main = gs.init_repo(tmp_path / "main", default_branch="dev")
    (main / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (main / "tests").mkdir()
    (main / "tests" / "test_seed.py").write_text(
        "def test_seed():\n    assert True\n", encoding="utf-8"
    )
    base = gs.commit_all(main, "base commit")
    gs.add_remote(main, "origin", str(remote))
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)

    wt = gs.add_worktree(main, tmp_path / BRANCH, BRANCH)
    (wt / "feature.txt").write_text("feature\n", encoding="utf-8")
    feature = gs.commit_all(wt, "optional feature")
    gs.run_git(remote, ["symbolic-ref", "HEAD", "refs/heads/dev"], check=True)

    return {
        "tmp": tmp_path,
        "remote": remote,
        "main": main,
        "wt": wt,
        "base": base,
        "feature": feature,
        "feature_short": gs.run_git(
            wt, ["rev-parse", "--short", "HEAD"], check=True
        ).stdout.strip(),
    }


@contextlib.contextmanager
def _real_dev_sync_finish(sandbox: dict, pushes: list[tuple]):
    """Drive ``phase_finish`` with the local-``dev`` sync machinery real.

    Build, gate, commit, the push boundary, worktree removal and the work-item
    store are stubbed (they have dedicated coverage). The pieces under test —
    ``_fast_forward_local_dev_for_push``, ``_restore_local_dev_after_failed_push``,
    ``_assert_commit_on_origin_dev`` and ``_sync_parent_branch`` — run against
    the sandbox's real git repositories, so the AC1/AC2 write-then-restore
    window would be directly observable: a pre-push test could assert ``dev``
    moved while the push was pending, and a failed-push test could assert it
    was rewritten then restored. Both must report no movement at all.
    """
    main = sandbox["main"]
    wt = sandbox["wt"]
    state = implement.ImplementState(
        work_item_id=WORK_ITEM,
        worktree_path=str(wt),
        repo_root=str(main),
        parent_branch="dev",
    )

    def recording_push(cwd, branch, commit_hash=None, **kwargs):
        pushes.append((cwd, branch, commit_hash))
        evidence_out = kwargs.get("evidence_out")
        if evidence_out is not None:
            evidence_out.update(
                {
                    "root": str(cwd),
                    "head": sandbox["feature"],
                    "pushed": sandbox["feature"],
                    "full_suite": "cached",
                }
            )
        return True

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=str(wt))
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(patch.object(implement, "run_build", return_value=_build_result()))
        stack.enter_context(
            patch.object(
                implement,
                "run_gate_suite",
                return_value=(
                    _tests_result(),
                    {
                        "tree": "abc1234",
                        "scope": "changed",
                        "cached": False,
                        "index_unchanged": True,
                    },
                ),
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "stage_tested_tree",
                return_value={"success": True, "tree": "abc1234", "expected": "abc1234"},
            )
        )
        stack.enter_context(patch.object(implement, "run_tests", return_value=_tests_result()))
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value=sandbox["feature_short"])
        )
        stack.enter_context(patch.object(implement, "git_push_to_dev", recording_push))
        stack.enter_context(
            patch.object(
                implement,
                "cleanup_worktree_processes",
                return_value={"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""},
            )
        )
        stack.enter_context(patch.object(implement, "remove_state"))
        stack.enter_context(patch.object(implement, "_remove_worktree", return_value=True))
        stack.enter_context(patch.object(implement, "wl_add_comment", return_value=True))
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(
                implement,
                "query_cached",
                return_value={
                    "stdout": "1 passed",
                    "stderr": "",
                    "exit_code": 0,
                    "completed_at": 1.0,
                    "command": implement.PYTEST_CMD,
                    "git_state": "stub-git-state",
                    "fingerprint_kind": "worktree",
                    "scope": "full",
                    "test_type": "full",
                    "cached": True,
                },
            )
        )
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", MagicMock())
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle,
                "show",
                MagicMock(
                    return_value={
                        "workItem": {"status": "completed", "stage": "in_review"}
                    }
                ),
            )
        )
        yield


# ---------------------------------------------------------------------------
# AC2 — a failed push leaves local dev provably unchanged
# ---------------------------------------------------------------------------


def test_failed_push_leaves_local_dev_provably_unchanged(sandbox):
    """AC2: on a refused push, local ``dev`` never moved from its pre-finish tip.

    The pre-fix behaviour advanced ``dev`` to the unpushed commit and then
    restored it on failure — a write-then-restore window visible to a
    concurrent ``start``. The fixed finish never advances pre-push, so the
    restore reports ``no_advance_to_undo`` and local ``dev`` stays exactly at
    the (pushed) base commit.
    """
    main = sandbox["main"]
    remote = sandbox["remote"]
    pre_finish_dev = _rev(main, "dev")
    assert pre_finish_dev == _rev(remote, "dev"), "fixture sanity: dev == origin/dev"

    pushes: list[tuple] = []

    def gate_refusing_push(cwd, branch, commit_hash=None, **kwargs):
        raise implement.PushGateFailureError(
            "tests_failed", "gate refused", commit_hash or "x", branch
        )

    with (
        _real_dev_sync_finish(sandbox, pushes),
        patch.object(implement, "git_push_to_dev", side_effect=gate_refusing_push),
    ):
        report = implement.phase_finish(WORK_ITEM, json_output=True, no_refactor=True)

    assert report["success"] is False, report["message"]
    # The pre-push step is a deferral, never an advance.
    ff_step = report["steps"]["local_dev_fast_forward"]
    assert ff_step["method"] == "deferred", ff_step
    assert ff_step["advanced"] is False, ff_step
    # The failed-push restore finds nothing to undo — the AC2 proof.
    restore = report["steps"]["local_dev_restore"]
    assert restore["reason"] == "no_advance_to_undo", restore

    # Local dev is provably unchanged from its pre-finish value.
    assert _rev(main, "dev") == pre_finish_dev, (
        "a failed push must leave local dev at its pre-finish tip; "
        f"dev={_rev(main, 'dev')} pre_finish={pre_finish_dev}"
    )


def test_transport_failure_leaves_local_dev_provably_unchanged(sandbox):
    """AC2: the exhausted-transport path is another no-write proof point."""
    main = sandbox["main"]
    pre_finish_dev = _rev(main, "dev")
    pushes: list[tuple] = []

    def transport_failing_push(cwd, branch, commit_hash=None, **kwargs):
        raise implement.PushTransportExhaustedError(
            commit_hash or "x", branch, "Permission denied (publickey).", 3
        )

    with (
        _real_dev_sync_finish(sandbox, pushes),
        patch.object(implement, "git_push_to_dev", side_effect=transport_failing_push),
    ):
        report = implement.phase_finish(WORK_ITEM, json_output=True, no_refactor=True)

    assert report["success"] is False, report["message"]
    assert report["steps"]["local_dev_restore"]["reason"] == "no_advance_to_undo", (
        report["steps"]["local_dev_restore"]
    )
    assert _rev(main, "dev") == pre_finish_dev


# ---------------------------------------------------------------------------
# AC1 — the advance happens only after the push reports success
# ---------------------------------------------------------------------------


def test_successful_push_advances_local_dev_only_post_push(sandbox):
    """AC1: local ``dev`` reaches the pushed commit only after the push lands.

    The pre-push step records a *deferral* (no movement); after the push
    succeeds and the ancestry assertion verifies the commit on
    ``origin/dev``, the single post-push fast-forward advances ``dev`` to it.
    """
    main = sandbox["main"]
    remote = sandbox["remote"]
    pre_finish_dev = _rev(main, "dev")
    pushes: list[tuple] = []

    def really_pushing_push(cwd, branch, commit_hash=None, **kwargs):
        pushes.append((cwd, branch, commit_hash))
        # Simulate the real outcome: the feature branch lands on origin/dev.
        result = gs.run_git(
            Path(cwd),
            ["push", "-q", "origin", f"{branch}:refs/heads/dev"],
            check=False,
        )
        evidence_out = kwargs.get("evidence_out")
        if evidence_out is not None:
            evidence_out.update(
                {
                    "root": str(cwd),
                    "head": commit_hash,
                    "pushed": commit_hash,
                    "full_suite": "cached",
                }
            )
        return result.returncode == 0

    with (
        _real_dev_sync_finish(sandbox, pushes),
        patch.object(implement, "git_push_to_dev", side_effect=really_pushing_push),
    ):
        report = implement.phase_finish(WORK_ITEM, json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    # The pre-push step deferred (covered in isolation by the failed-push
    # tests above); after the successful push the *post-push* advance replaced
    # the marker, so the final record is the advance.
    ff_step = report["steps"]["local_dev_fast_forward"]
    assert ff_step["method"] == "advanced", ff_step
    assert _names_commit(ff_step.get("advanced_to") or "", sandbox["feature"]), ff_step
    assert _names_commit(_rev(main, "dev"), sandbox["feature"]), (
        f"local dev must track the pushed commit; dev={_rev(main, 'dev')}"
    )
    # And the post-push parent-branch sync keeps it at the pushed tip (it may
    # safe-skip — the main checkout is dirty from the advance itself — the
    # ref is what matters).
    sync = report["steps"]["sync_parent_branch"]
    assert sync["method"] in ("synced", "skipped"), sync
    assert _rev(main, "dev") == _rev(remote, "dev")
    assert pre_finish_dev != _rev(main, "dev"), "the advance must have happened"

    # The push ran from the worktree (the finish push contract, untouched).
    assert pushes and pushes[0][0] == str(sandbox["wt"]), pushes


# ---------------------------------------------------------------------------
# Recovery — the rebase-and-retry path never advances dev mid-push
# ---------------------------------------------------------------------------


def test_non_fast_forward_recovery_defers_the_advance(sandbox):
    """The NFF recovery records a deferral, never an advance, pre-retry-push.

    ``_recover_non_fast_forward_push`` used to fast-forward local ``dev`` to
    the rebased commit *before* the retry push. Under AC1 it must not touch
    local ``dev`` at all — the retry push may still fail, and the single
    post-push advance in ``phase_finish`` owns the movement.
    """
    main = sandbox["main"]
    pre_finish_dev = _rev(main, "dev")
    ff_retry: dict = {}

    # The rebase "succeeds" and rewrites HEAD to a replacement commit.
    run_ok = gs.run_git(main, ["rev-parse", "HEAD"], check=False)

    with (
        patch.object(implement, "run_cmd", return_value=run_ok),
        patch.object(implement, "git_get_commit_hash", return_value="rebased123"),
        patch.object(implement, "_assert_push_subject"),
        patch.object(implement, "git_push_to_dev", return_value=True),
        patch.object(implement, "wl_add_comment", return_value=True),
    ):
        new_commit = implement._recover_non_fast_forward_push(
            worktree_path=str(sandbox["wt"]),
            repo_root=str(main),
            branch=BRANCH,
            commit_hash=sandbox["feature_short"],
            exc=implement.PushNonFastForwardError(
                sandbox["feature_short"],
                BRANCH,
                "non-fast-forward",
                "git rebase origin/dev",
            ),
            work_item_id=WORK_ITEM,
            parent_branch="dev",
            ff_retry_out=ff_retry,
        )

    assert new_commit == "rebased123"
    # The retry record is a deferral — local dev was never advanced mid-push.
    assert ff_retry["method"] == "deferred", ff_retry
    assert ff_retry["advanced"] is False, ff_retry
    assert _rev(main, "dev") == pre_finish_dev, (
        "the recovery must not move local dev before the retry push succeeds"
    )
