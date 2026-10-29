
# <!-- REFACTOR-SA-0MVU68D5O007OSP5
# smell: ruff_specific
# severity: medium
# description: Mutable default value for class attribute
# -->
"""Hermetic regression tests: concurrent ``implement.py finish`` orphans.

Root cause (SA-0MVNQURZM001TKJY, incident 2026-10-25): ``phase_finish``
mutates the **shared** main-checkout ``dev`` ref (pre-push fast-forward) and
only then pushes the commit to ``origin/dev``. The ref mutation, the push and
the post-push sync are neither locked nor verified, so two sessions finishing
different children of one epic interleave:

1. finish A fast-forwards local ``dev`` to its commit A and its push fails →
   local ``dev`` is left pinned at the unpushed commit A;
2. finish B, forked from the *older* base, fast-forwards ``origin/dev`` and
   pushes B.

Result: local ``dev`` never moves (it "diverged" and the sync safe-skips), A
is reachable only from its feature branch, and both runs may still report
success — the commit is silently orphaned and the next child worktree forks
from a stale/diverged base.

This module is the test-first slice of that parent: it defines the observable
regression contract the sibling features must satisfy —

- **Lock** (SA-0MVPZG3IQ009IJMO): the shared ``dev`` window is single-owner; a
  concurrent ``finish`` fails fast with an actionable, holder-naming message
  and a lock file left by a crashed run never blocks a later ``finish``.
- **Verified sync + ancestry** (SA-0MVPZG7A0000N66U): a failed push restores
  local ``dev`` to ``refs/remotes/origin/dev``, and a ``finish`` whose commit
  did not reach ``origin/dev`` fails loudly instead of reporting success.

Assertions for behaviour that is not implemented yet are marked with an
imperative ``pytest.xfail`` naming the blocking work item. The marker fires
only when the defect is observed, so the development-time changed-scope gate
stays green while the feature is absent and the marker disappears by itself
once it lands (the sibling items then simply go green — no strict-xpass
churn, no manual marker removal).

Hermeticity: every scenario runs in a bare-remote + clone sandbox under
``tmp_path`` built with ``shared.git_sandbox`` (which scrubs the
repository-overriding git environment, so a leaked ``GIT_DIR`` cannot reach
the live checkout). Only the worklog-facing seams (``wl_add_comment``,
``StatusLifecycle``) and the host build/test tooling are mocked; everything
that moves a ref — ``git_commit``, ``git_push_to_dev``, ``_sync_parent_branch``,
``_remove_worktree`` — runs for real inside the sandbox. The live checkout's
refs, worktrees and worklog are never touched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from shared import git_sandbox as gs

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def implement_mod():
    """Load implement.py as a module for direct helper / phase testing."""
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_concurrent_orphan", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_concurrent_orphan"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Hermetic git sandbox
# ---------------------------------------------------------------------------

_BRANCH_A = "wl-SA-0TESTORPHAN1"
_BRANCH_B = "wl-SA-0TESTORPHAN2"
_ITEM_A = "SA-0TESTORPHAN1"
_ITEM_B = "SA-0TESTORPHAN2"


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A bare ``origin`` plus a main checkout and two sibling worktrees.

    The two worktrees are both forked from the same *older* base before either
    finishes — exactly the concurrent-session shape of the incident. The
    repository-overriding git environment is neutralised and the process cwd is
    moved into the sandbox, so any production git call that omits an explicit
    ``cwd`` still operates on the sandbox.
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

    wt_a = gs.add_worktree(work, tmp_path / _BRANCH_A, _BRANCH_A, base="dev")
    wt_b = gs.add_worktree(work, tmp_path / _BRANCH_B, _BRANCH_B, base="dev")

    return {
        "tmp": tmp_path,
        "remote": remote,
        "work": work,
        "base": base,
        "wt_a": wt_a,
        "wt_b": wt_b,
    }


# ---------------------------------------------------------------------------
# Git assertions
# ---------------------------------------------------------------------------


def _rev(cwd: Path, ref: str) -> str:
    return gs.run_git(cwd, ["rev-parse", ref], check=True).stdout.strip()


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    return (
        gs.run_git(cwd, ["merge-base", "--is-ancestor", ancestor, descendant])
        .returncode
        == 0
    )


def _local_dev(sandbox: dict) -> str:
    return _rev(sandbox["work"], "refs/heads/dev")


def _origin_dev(sandbox: dict) -> str:
    return _rev(sandbox["work"], "refs/remotes/origin/dev")


def _branches_containing(sandbox: dict, commit: str) -> set[str]:
    proc = gs.run_git(
        sandbox["work"],
        [
            "for-each-ref",
            "--contains",
            commit,
            "--format=%(refname:short)",
            "refs/heads",
        ],
        check=True,
    )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


# ---------------------------------------------------------------------------
# phase_finish harness (real git, mocked worklog / host tooling)
# ---------------------------------------------------------------------------


class _RecordingLifecycle:
    """Stand-in for ``StatusLifecycle`` that records every transition."""

    transitions: list[tuple[str, str, str | None]] = []

    def __init__(self, work_item_id, target_stage=None, clear_assignee_on_exit=False):
        self.work_item_id = work_item_id
        self.target_stage = target_stage

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.target_stage:
            self.transitions.append(
                (self.work_item_id, "completed", self.target_stage)
            )
        return False

    @classmethod
    def reset(cls) -> None:
        cls.transitions = []

    @classmethod
    def update_status(cls, work_item_id, status, **kwargs):
        cls.transitions.append((work_item_id, status, kwargs.get("stage")))

    @staticmethod
    def show(work_item_id):
        # The post-finish store read-back (OSL-0MVME1PI5005WU58) reads the
        # item back after the terminal write; the real StatusLifecycle always
        # exposes this staticmethod.
        return {"workItem": {"status": "completed", "stage": "in_review"}}


def _write_state(implement_mod, worktree: Path, work: Path, item: str) -> None:
    """Write the real ``.implement_state.json`` the finish phase reads."""
    state = implement_mod.ImplementState(
        work_item_id=item,
        worktree_path=str(worktree),
        repo_root=str(work),
        parent_branch="dev",
        started_at="2026-01-01T00:00:00Z",
    )
    (worktree / implement_mod.STATE_FILE_NAME).write_text(
        json.dumps(state.to_dict()), encoding="utf-8"
    )


def _run_finish(implement_mod, sandbox: dict, worktree: Path, item: str, push=None):
    """Drive the production ``phase_finish`` against the git sandbox.

    ``push`` optionally replaces ``git_push_to_dev`` (same signature) to
    simulate a push outcome. Everything else that moves a ref is real.
    """
    _write_state(implement_mod, worktree, sandbox["work"], item)
    _RecordingLifecycle.reset()

    patches = [
        mock.patch.object(
            implement_mod, "_discover_worktree", return_value=str(worktree)
        ),
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
                "scope": "changed",
            },
        ),
        mock.patch.object(
            implement_mod, "cleanup_worktree_processes", return_value={}
        ),
        mock.patch.object(implement_mod, "wl_add_comment", return_value=True),
        mock.patch.object(implement_mod, "StatusLifecycle", _RecordingLifecycle),
    ]
    if push is not None:
        patches.append(
            mock.patch.object(implement_mod, "git_push_to_dev", new=push)
        )

    for patcher in patches:
        patcher.start()
    try:
        return implement_mod.phase_finish(item, json_output=True, no_refactor=True)
    finally:
        for patcher in reversed(patches):
            patcher.stop()


def _push_fails(repo_root, branch, commit_hash=None, timeout=None) -> bool:
    """A transient failure: nothing reached ``origin``."""
    return False


def _push_reports_failure_but_lands(implement_mod, counter: dict):
    """Push for real, then report failure (the ambiguous false negative)."""
    real_push = implement_mod.git_push_to_dev

    def _push(repo_root, branch, commit_hash=None, timeout=None) -> bool:
        ok = real_push(repo_root, branch, commit_hash, timeout)
        counter["calls"] = counter.get("calls", 0) + 1
        if counter["calls"] == 1:
            return False
        return ok

    return _push


def _push_reports_success_but_does_nothing(
    repo_root, branch, commit_hash=None, timeout=None
) -> bool:
    """The mirror-image false positive: report success without publishing."""
    return True


# ---------------------------------------------------------------------------
# Scenario 1 — a landed-but-reported-failed push is never orphaned
# ---------------------------------------------------------------------------


def test_published_commit_survives_a_sibling_finish_from_the_older_base(
    implement_mod, sandbox
):
    """The incident shape, end to end: A lands, then B finishes from the base.

    A's push actually reached ``origin/dev`` but was *reported* failed (the
    false negative tracked by SA-0MUOOXH17004ZEFX). B, forked from the older
    base, must not drop A: A stays an ancestor of both local ``dev`` and
    ``refs/remotes/origin/dev``, and local ``dev`` never diverges from the
    remote.

    This is a hard regression guard (green before and after the fixes): it
    pins the invariant, not the implementation, so a refusal to fast-forward
    and a rebase-then-succeed outcome both satisfy it.
    """
    work = sandbox["work"]
    wt_a, wt_b = sandbox["wt_a"], sandbox["wt_b"]

    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
    report_a = _run_finish(
        implement_mod,
        sandbox,
        wt_a,
        _ITEM_A,
        push=_push_reports_failure_but_lands(implement_mod, {}),
    )
    commit_a = _rev(wt_a, "HEAD")

    # Preconditions of the incident: A is published but finish reported
    # failure, and the pre-push fast-forward already moved local dev.
    assert report_a["success"] is False, report_a
    assert _is_ancestor(work, commit_a, "refs/remotes/origin/dev"), (
        "scenario precondition: the ambiguous push must have landed"
    )

    (wt_b / "feature_b.txt").write_text("b\n", encoding="utf-8")
    report_b = _run_finish(implement_mod, sandbox, wt_b, _ITEM_B)

    final_dev = _local_dev(sandbox)
    final_origin = _origin_dev(sandbox)

    assert _is_ancestor(work, commit_a, "refs/remotes/origin/dev"), (
        "the published first commit must never be dropped from origin/dev"
    )
    assert _is_ancestor(work, commit_a, "refs/heads/dev"), (
        "the first commit must stay reachable from local dev"
    )
    assert final_dev == final_origin, (
        "local dev must never diverge from origin/dev after a finish "
        f"(local={final_dev[:7]}, origin={final_origin[:7]}, "
        f"report_b={report_b.get('message')!r})"
    )
    assert _BRANCH_A in _branches_containing(sandbox, commit_a), (
        "the first commit must remain reachable for recovery"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — a genuinely failed push is rolled back, not left in dev
# ---------------------------------------------------------------------------


def test_failed_push_restores_local_dev_and_keeps_commit_on_its_branch(
    implement_mod, sandbox
):
    """A failed push must not leave local ``dev`` on an unpushed commit.

    Contract (parent AC1, SA-0MVPZG7A0000N66U): after a push failure local
    ``dev`` points at ``refs/remotes/origin/dev`` (or a commit already on it)
    and the unpushed commit stays reachable from its feature branch. Without
    that restore, the next finish (forked from the older base) either skips
    its pre-push fast-forward or pins local ``dev`` off the remote entirely —
    the orphan observed on 2026-10-25.

    While the feature is absent the restore assertion is marked ``xfail`` with
    the defect it observes; the sibling finish still runs (so the whole
    two-finish scenario is exercised) and its outcome is asserted after the
    marker.
    """
    work = sandbox["work"]
    wt_a, wt_b = sandbox["wt_a"], sandbox["wt_b"]

    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
    report_a = _run_finish(
        implement_mod, sandbox, wt_a, _ITEM_A, push=_push_fails
    )
    commit_a = _rev(work, "refs/heads/" + _BRANCH_A)
    after_failed_push_dev = _local_dev(sandbox)
    after_failed_push_origin = _origin_dev(sandbox)

    # Invariants that hold with or without the verified-sync feature.
    assert report_a["success"] is False, report_a
    assert "push to dev failed" in report_a["message"], report_a["message"]
    assert Path(wt_a).exists(), "a failed push must retain the worktree"
    assert _BRANCH_A in _branches_containing(sandbox, commit_a), (
        "the unpushed commit must stay reachable from its feature branch"
    )

    # The second finish, forked from the *older* base (before A's commit).
    (wt_b / "feature_b.txt").write_text("b\n", encoding="utf-8")
    report_b = _run_finish(implement_mod, sandbox, wt_b, _ITEM_B)

    final_dev = _local_dev(sandbox)
    final_origin = _origin_dev(sandbox)

    if after_failed_push_dev != after_failed_push_origin:
        pytest.xfail(
            "verified/reversible local-dev sync not implemented "
            "(SA-0MVPZG7A0000N66U): the failed push left local dev at the "
            f"unpushed commit {after_failed_push_dev[:7]}, so the next finish "
            "from the older base skips its fast-forward and local dev ends at "
            f"{final_dev[:7]} while origin/dev is at {final_origin[:7]}"
        )

    assert after_failed_push_dev == after_failed_push_origin, (
        "local dev must be restored to refs/remotes/origin/dev after a "
        "failed push"
    )
    assert report_b["success"] is True, report_b.get("message")
    assert final_dev == final_origin, (
        "the sibling finish must leave local dev == origin/dev"
    )
    assert not _is_ancestor(work, commit_a, "refs/remotes/origin/dev"), (
        "the unpublished commit must never be claimed as published"
    )
    assert _BRANCH_A in _branches_containing(sandbox, commit_a), (
        "the unpublished commit must stay reachable from its feature branch"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — post-push ancestry assertion
# ---------------------------------------------------------------------------


def test_finish_fails_loudly_when_its_commit_did_not_reach_origin_dev(
    implement_mod, sandbox
):
    """A finish whose commit is not on ``origin/dev`` must not report success.

    Contract (parent AC2, SA-0MVPZG7A0000N66U): after the push, ``finish``
    asserts ``git merge-base --is-ancestor <commit> refs/remotes/origin/dev``
    and fails loudly, naming the hash, leaving the worktree and feature branch
    in place and the work item un-advanced.
    """
    work = sandbox["work"]
    wt_a = sandbox["wt_a"]

    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
    report = _run_finish(
        implement_mod,
        sandbox,
        wt_a,
        _ITEM_A,
        push=_push_reports_success_but_does_nothing,
    )

    branch_a = "refs/heads/" + _BRANCH_A
    commit_a = _rev(work, branch_a)
    hash_a = report["steps"]["commit"]["hash"]

    if report["success"] and not _is_ancestor(
        work, commit_a, "refs/remotes/origin/dev"
    ):
        pytest.xfail(
            "post-push ancestry assertion not implemented "
            "(SA-0MVPZG7A0000N66U): finish reported success for commit "
            f"{hash_a} which is not on refs/remotes/origin/dev"
        )

    assert report["success"] is False, report
    assert hash_a and hash_a in report["message"], (
        f"the refusal must name the offending commit hash {hash_a!r}; "
        f"message was {report['message']!r}"
    )
    assert Path(wt_a).exists(), "a refused finish must retain the worktree"
    assert _BRANCH_A in _branches_containing(sandbox, commit_a), (
        "a refused finish must retain the feature branch"
    )
    assert ("completed", "in_review") not in {
        (status, stage) for _wid, status, stage in _RecordingLifecycle.transitions
    }, "a refused finish must not advance the work item to in_review"


# ---------------------------------------------------------------------------
# Scenario 4 — the single-owner finish window (lock)
# ---------------------------------------------------------------------------


def test_leftover_lock_file_does_not_block_a_later_finish(
    implement_mod, sandbox
):
    """A lock file left by a crashed run must never block the next finish.

    The lock is an advisory ``flock`` (kernel-released on process death), so
    only the flock — never the file's existence or its metadata — decides
    ownership.
    """
    work = sandbox["work"]
    wt_a = sandbox["wt_a"]

    lock_path = implement_mod._main_checkout_lock_path(str(work))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "work_item_id": "SA-0TESTCRASHED",
                "pid": 999_999,  # no such process: the holder is dead
                "started_at": "2026-01-01T00:00:00+00:00",
                "repo_root": str(work),
                "host": "crashed-host",
            }
        ),
        encoding="utf-8",
    )

    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
    report = _run_finish(implement_mod, sandbox, wt_a, _ITEM_A)

    assert report["success"] is True, report.get("message")
    assert report["steps"]["main_checkout_lock"]["acquired"] is True, (
        "a stale lock file must not block the finish lock"
    )
    assert _local_dev(sandbox) == _origin_dev(sandbox)


def test_concurrent_finish_fails_fast_with_an_actionable_message(
    implement_mod, sandbox
):
    """Two overlapping finishes in one repository must not interleave.

    The second finish either waits for the lock or fails fast; this contract
    accepts fail-fast, but requires it to be *actionable*: it names the lock,
    the holder and how to proceed, returns without hanging, and leaves the
    shared refs and the uncommitted work untouched. Once the holder releases,
    the next finish proceeds.
    """
    work = sandbox["work"]
    wt_a = sandbox["wt_a"]
    lock_path = implement_mod._main_checkout_lock_path(str(work))

    holding_fd, holder = implement_mod.acquire_main_checkout_lock(
        str(work), _ITEM_A
    )
    assert holding_fd is not None, holder
    try:
        dev_before = _local_dev(sandbox)
        origin_before = _origin_dev(sandbox)

        (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
        report = _run_finish(implement_mod, sandbox, wt_a, _ITEM_A)

        assert report["success"] is False, report
        message = report["message"]
        assert "lock" in message.lower(), message
        assert str(lock_path) in message, (
            f"the refusal must name the lock file; got {message!r}"
        )
        assert _ITEM_A in message, (
            f"the refusal must name the holder; got {message!r}"
        )
        assert "re-run" in message.lower(), (
            f"the refusal must say how to proceed; got {message!r}"
        )

        # Nothing moved: no commit, no ref update, work still uncommitted.
        assert _local_dev(sandbox) == dev_before
        assert _origin_dev(sandbox) == origin_before
        assert (wt_a / "feature_a.txt").exists()
    finally:
        implement_mod._release_main_checkout_lock(holding_fd)

    # Released (file left behind): the next finish proceeds normally.
    report_retry = _run_finish(implement_mod, sandbox, wt_a, _ITEM_A)
    assert report_retry["success"] is True, report_retry.get("message")


def test_finish_releases_the_lock_after_a_failed_push(
    implement_mod, sandbox
):
    """A failed push must not leak the finish lock (no deadlocked checkout)."""
    work = sandbox["work"]
    wt_a = sandbox["wt_a"]

    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
    report = _run_finish(
        implement_mod, sandbox, wt_a, _ITEM_A, push=_push_fails
    )
    assert report["success"] is False, report

    fd, holder = implement_mod.acquire_main_checkout_lock(str(work), _ITEM_B)
    try:
        assert fd is not None, (
            f"finish leaked the main-checkout lock after a failed push: "
            f"{holder}"
        )
    finally:
        implement_mod._release_main_checkout_lock(fd)
