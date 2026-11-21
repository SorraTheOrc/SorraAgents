
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
  (or waits a bounded, configured time and then times out actionably) and a
  lock file left by a crashed run never blocks a later ``finish``. The
  adjacent ``start`` parent-branch refresh — which moves the same shared ref —
  takes the same lock and skips the refresh (fail-open) when it is held.
- **Deferred sync + ancestry** (SA-0MVPZG7A0000N66U; re-contracted by
  WL-0MWFKDH78002T2CR AC1/AC2): local ``dev`` is **never moved while a push
  is in flight** — the fast-forward is deferred until after the push reports
  success, so a failed push leaves local ``dev`` provably unchanged
  (``no_advance_to_undo``), and a ``finish`` whose commit did not reach
  ``origin/dev`` fails loudly instead of reporting success.

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
import time
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

    def __init__(self, work_item_id, target_stage=None, assignee=None, **kwargs):
        self.work_item_id = work_item_id
        self.target_stage = target_stage
        self.assignee = assignee

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


def _push_fails(repo_root, branch, commit_hash=None, timeout=None, **kwargs) -> bool:
    """A transient failure: nothing reached ``origin``.

    Accepts (and ignores) ``evidence_out`` — ``phase_finish`` threads it into
    every ``git_push_to_dev`` call (SA-0MVUTXJM8006PQY4).
    """
    return False


def _push_reports_failure_but_lands(implement_mod, counter: dict):
    """Push for real, then report failure (the ambiguous false negative)."""
    real_push = implement_mod.git_push_to_dev

    def _push(repo_root, branch, commit_hash=None, timeout=None, **kwargs) -> bool:
        ok = real_push(repo_root, branch, commit_hash, timeout, **kwargs)
        counter["calls"] = counter.get("calls", 0) + 1
        if counter["calls"] == 1:
            return False
        return ok

    return _push


def _push_reports_success_but_does_nothing(
    repo_root, branch, commit_hash=None, timeout=None, **kwargs
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
    false negative tracked by SA-0MUOOXH17004ZEFX). Under the AC2 contract of
    WL-0MWFKDH78002T2CR the failed push leaves local ``dev`` **provably
    unchanged** (``no_advance_to_undo`` — the advance is deferred until after
    a successful push, so there is no write-then-restore window for a
    concurrent ``start`` to fork from), and A's commit is never dropped from
    ``origin/dev``. Local ``dev`` converges to the published tip through the
    framework's sync helper — the same ``_sync_parent_branch`` a later
    successful finish's post-push step and a later ``start`` refresh use —
    after which A stays an ancestor of both local ``dev`` and
    ``origin/dev`` and local ``dev`` never diverges.
    """
    work = sandbox["work"]
    wt_a, wt_b = sandbox["wt_a"], sandbox["wt_b"]

    pre_finish_dev = _local_dev(sandbox)

    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
    report_a = _run_finish(
        implement_mod,
        sandbox,
        wt_a,
        _ITEM_A,
        push=_push_reports_failure_but_lands(implement_mod, {}),
    )
    commit_a = _rev(wt_a, "HEAD")

    # A is published but finish reported failure, and the failed push
    # provably left local dev untouched (AC2 — no write-then-restore window).
    assert report_a["success"] is False, report_a
    assert _is_ancestor(work, commit_a, "refs/remotes/origin/dev"), (
        "scenario precondition: the ambiguous push must have landed"
    )
    restore = report_a["steps"]["local_dev_restore"]
    assert restore["reason"] == "no_advance_to_undo", restore
    assert _local_dev(sandbox) == pre_finish_dev, (
        "AC2: a failed push must leave local dev at its pre-finish tip "
        f"(now={_local_dev(sandbox)[:7]}, "
        f"pre_finish={pre_finish_dev[:7]})"
    )

    # The sibling finish, forked from the older base, never drops A.
    (wt_b / "feature_b.txt").write_text("b\n", encoding="utf-8")
    _run_finish(implement_mod, sandbox, wt_b, _ITEM_B)

    assert _is_ancestor(work, commit_a, "refs/remotes/origin/dev"), (
        "the published first commit must never be dropped from origin/dev"
    )

    # Convergence comes from the framework's sync (a later successful
    # finish's post-push step / a later start refresh), never from a
    # failed-push write: after the sync A stays reachable from local dev and
    # local dev equals origin/dev.
    sync = implement_mod._sync_parent_branch(str(work), "dev")
    assert sync["method"] == "synced", sync
    final_dev = _local_dev(sandbox)
    final_origin = _origin_dev(sandbox)
    assert _is_ancestor(work, commit_a, "refs/heads/dev"), (
        "the first commit must stay reachable from local dev"
    )
    assert final_dev == final_origin, (
        "local dev must never diverge from origin/dev after the sync "
        f"(local={final_dev[:7]}, origin={final_origin[:7]}, sync={sync!r})"
    )
    assert _BRANCH_A in _branches_containing(sandbox, commit_a), (
        "the first commit must remain reachable for recovery"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — a genuinely failed push is rolled back, not left in dev
# ---------------------------------------------------------------------------


def test_failed_push_leaves_local_dev_unchanged_and_keeps_commit_on_branch(
    implement_mod, sandbox
):
    """A failed push must not move local ``dev``; the commit stays on its branch.

    Contract (parent AC1/AC2, WL-0MWFKDH78002T2CR): the fast-forward is
    deferred until after a successful push, so on a failed push local ``dev``
    is **provably unchanged** from its pre-finish value — the restore reports
    ``no_advance_to_undo`` and there is no write-then-restore window for a
    concurrent ``start`` to fork from (the orphan observed on 2026-10-25 and
    the graft observed on 2026-11-13). The unpushed commit stays reachable
    from its feature branch. The sibling finish (from the same base) still
    runs; because nothing landed it fast-forwards cleanly and the shared refs
    converge.
    """
    work = sandbox["work"]
    wt_a, wt_b = sandbox["wt_a"], sandbox["wt_b"]

    pre_finish_dev = _local_dev(sandbox)

    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
    report_a = _run_finish(
        implement_mod, sandbox, wt_a, _ITEM_A, push=_push_fails
    )
    commit_a = _rev(work, "refs/heads/" + _BRANCH_A)
    after_failed_push_dev = _local_dev(sandbox)

    # Invariants that hold with or without the verified-sync feature.
    assert report_a["success"] is False, report_a
    assert "push to dev failed" in report_a["message"], report_a["message"]
    assert Path(wt_a).exists(), "a failed push must retain the worktree"
    assert _BRANCH_A in _branches_containing(sandbox, commit_a), (
        "the unpushed commit must stay reachable from its feature branch"
    )
    # AC2: a failed push leaves local dev provably unchanged — nothing to undo.
    assert after_failed_push_dev == pre_finish_dev, (
        "AC2: local dev must be provably unchanged from its pre-finish value "
        f"(now={after_failed_push_dev[:7]}, "
        f"pre_finish={pre_finish_dev[:7]})"
    )
    restore = report_a["steps"]["local_dev_restore"]
    assert restore["reason"] == "no_advance_to_undo", restore

    # The second finish, forked from the same base: nothing landed, so it
    # fast-forwards cleanly and converges the shared refs.
    (wt_b / "feature_b.txt").write_text("b\n", encoding="utf-8")
    report_b = _run_finish(implement_mod, sandbox, wt_b, _ITEM_B)

    final_dev = _local_dev(sandbox)
    final_origin = _origin_dev(sandbox)
    assert report_b["success"] is True, report_b.get("message")
    assert final_dev == final_origin, (
        "the sibling finish must leave local dev == origin/dev "
        f"(local={final_dev[:7]}, origin={final_origin[:7]})"
    )
    # A's commit never landed: it must not be claimed as published.
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


# ---------------------------------------------------------------------------
# Scenario 5 — the verified-sync / ancestry report contract
# ---------------------------------------------------------------------------


def test_successful_finish_records_verified_ancestry_and_local_dev_advance(
    implement_mod, sandbox
):
    """A landed finish records the post-push advance and the ancestry verdict.

    ``finish`` may only claim success when its own commit is verifiably on
    ``origin/dev``; the post-push fast-forward (never a pre-push one) and the
    post-push ancestry verdict are observable in the JSON report so an
    operator can see *why* it succeeded (WL-0MWFKDH78002T2CR AC1).
    """
    work = sandbox["work"]
    wt_a = sandbox["wt_a"]

    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
    report = _run_finish(implement_mod, sandbox, wt_a, _ITEM_A)
    commit_a = _rev(work, "refs/heads/" + _BRANCH_A)

    assert report["success"] is True, report.get("message")
    advance = report["steps"]["local_dev_fast_forward"]
    assert advance["advanced"] is True, advance
    assert advance["pre_advance_tip"] == sandbox["base"], advance
    assert advance["advanced_to"] == commit_a, advance

    verdict = report["steps"]["ancestry_assertion"]
    assert verdict["verified"] is True, verdict
    assert verdict["resolved_commit"] == commit_a, verdict
    assert verdict["target"] == "refs/remotes/origin/dev", verdict
    assert _is_ancestor(work, commit_a, "refs/remotes/origin/dev")


def test_failed_push_report_names_the_local_dev_restore(
    implement_mod, sandbox
):
    """A failed push records that there was nothing to undo (AC2)."""
    work = sandbox["work"]
    wt_a = sandbox["wt_a"]
    (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")

    report = _run_finish(
        implement_mod, sandbox, wt_a, _ITEM_A, push=_push_fails
    )
    commit_a = _rev(work, "refs/heads/" + _BRANCH_A)

    assert report["success"] is False, report
    assert report["steps"]["push"]["success"] is False
    assert report["steps"]["push"]["reason"] == "push_failed"
    restore = report["steps"]["local_dev_restore"]
    assert restore["reason"] == "no_advance_to_undo", restore
    assert _local_dev(sandbox) == _origin_dev(sandbox) == sandbox["base"], (
        "a failed push must leave local dev provably unchanged (AC2)"
    )
    # The unpushed commit is retained on its feature branch for recovery.
    assert _rev(wt_a, "HEAD") == commit_a


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


def test_finish_lock_wait_is_bounded_and_times_out_actionably(
    implement_mod, sandbox, monkeypatch
):
    """A configured wait is bounded and its expiry is actionable.

    Contract (parent AC3, SA-0MVPZG3IQ009IJMO): the wait is always bounded by
    a deadline — never an indefinite hang — and the expiry names the lock, the
    holder and how to proceed, leaving the shared refs and the uncommitted
    work untouched. With no timeout configured ``finish`` fails fast instead
    (covered above); here ``IMPLEMENT_FINISH_LOCK_TIMEOUT`` opts into waiting.
    """
    work = sandbox["work"]
    wt_a = sandbox["wt_a"]
    lock_path = implement_mod._main_checkout_lock_path(str(work))
    monkeypatch.setenv("IMPLEMENT_FINISH_LOCK_TIMEOUT", "0.3")

    holding_fd, holder = implement_mod.acquire_main_checkout_lock(
        str(work), _ITEM_A
    )
    assert holding_fd is not None, holder
    try:
        dev_before = _local_dev(sandbox)
        origin_before = _origin_dev(sandbox)

        (wt_a / "feature_a.txt").write_text("a\n", encoding="utf-8")
        started = time.monotonic()
        report = _run_finish(implement_mod, sandbox, wt_a, _ITEM_A)
        elapsed = time.monotonic() - started

        assert report["success"] is False, report
        assert elapsed < 60.0, (
            f"the lock wait must be bounded by the configured timeout; "
            f"took {elapsed}s"
        )
        message = report["message"]
        assert "Timed out" in message, message
        assert str(lock_path) in message, message
        assert _ITEM_A in message, message
        assert "concurrent finish" in message, message
        assert "re-run" in message.lower(), message

        lock_step = report["steps"]["main_checkout_lock"]
        assert lock_step["acquired"] is False
        assert lock_step["timeout_seconds"] == pytest.approx(0.3)

        # Nothing moved: no commit, no ref update, work still uncommitted.
        assert _local_dev(sandbox) == dev_before
        assert _origin_dev(sandbox) == origin_before
        assert (wt_a / "feature_a.txt").exists()
    finally:
        implement_mod._release_main_checkout_lock(holding_fd)


# ---------------------------------------------------------------------------
# Scenario 5 — the adjacent parent-branch refresh window (phase_start)
# ---------------------------------------------------------------------------


def _advance_origin_dev(sandbox: dict, filename: str) -> str:
    """Push a new commit to ``origin/dev`` from a second clone.

    Leaves the sandbox's main checkout with a local ``dev`` that is *behind*
    ``origin/dev`` — the state a ``start`` refresh is meant to correct.
    """
    other = gs.run_git(sandbox["tmp"], ["clone", "-q", str(sandbox["remote"]),
                                        str(sandbox["tmp"] / "other")])
    assert other.returncode == 0, other.stderr
    other_repo = sandbox["tmp"] / "other"
    gs.run_git(other_repo, ["checkout", "-q", "dev"], check=True)
    (other_repo / filename).write_text("upstream\n", encoding="utf-8")
    commit = gs.commit_all(other_repo, f"upstream {filename}")
    gs.run_git(other_repo, ["push", "-q", "origin", "dev"], check=True)
    return commit


def test_start_refresh_waits_for_the_finish_lock_then_syncs(
    implement_mod, sandbox, monkeypatch
):
    """The start-window refresh takes the lock and syncs once it is free.

    The parent-branch refresh in ``phase_start`` fast-forwards the same shared
    local ``dev`` ref a concurrent ``finish`` moves, so it must hold the same
    per-repository lock (parent AC3's "adjacent window" clause). With the lock
    free it syncs local ``dev`` to ``origin/dev`` as before.
    """
    work = sandbox["work"]
    upstream = _advance_origin_dev(sandbox, "upstream.txt")

    result = implement_mod._sync_parent_branch_locked(
        str(work), "dev", _ITEM_A, timeout=5.0
    )

    assert result["success"] is True, result
    assert result["method"] == "synced", result
    assert result["lock"]["acquired"] is True
    assert _rev(work, "refs/heads/dev") == upstream
    assert _local_dev(sandbox) == _origin_dev(sandbox)


def test_start_refresh_skips_when_the_finish_lock_is_held(
    implement_mod, sandbox
):
    """A held finish lock never blocks or fails the start refresh (fail-open).

    ``start`` must stay usable while a finish runs: the refresh waits only up
    to its bound, then skips with a warning naming the lock and leaves the
    shared ref alone (the local branch is still a valid base).
    """
    work = sandbox["work"]
    _advance_origin_dev(sandbox, "upstream.txt")
    lock_path = implement_mod._main_checkout_lock_path(str(work))
    dev_before = _local_dev(sandbox)

    holding_fd, holder = implement_mod.acquire_main_checkout_lock(
        str(work), _ITEM_B
    )
    assert holding_fd is not None, holder
    try:
        result = implement_mod._sync_parent_branch_locked(
            str(work), "dev", _ITEM_A, timeout=0.2
        )
    finally:
        implement_mod._release_main_checkout_lock(holding_fd)

    assert result["success"] is True, result
    assert result["method"] == "skipped", result
    assert result["reason"] == "lock_held", result
    assert result["lock"]["acquired"] is False
    assert str(lock_path) in result["warning"], result["warning"]
    assert _ITEM_B in result["warning"], result["warning"]
    assert _local_dev(sandbox) == dev_before, (
        "a skipped refresh must not move the shared parent ref"
    )
