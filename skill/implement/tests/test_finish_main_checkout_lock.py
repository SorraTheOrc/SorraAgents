
# <!-- REFACTOR-SA-0MWFMO8V5005IHRI
# smell: simplification
# severity: medium
# description: Use a single `with` statement with multiple contexts instead of nested `with` statements
# -->
"""Main-checkout finish lock regression tests (WL-0MVPADZ1Z000FOSG AC5).

``implement.py finish`` operates on the shared main checkout (fast-forward,
push, worktree removal). Two finishes on the same main checkout concurrently —
the 2026-10-26 overlap — must be refused up front, with a named holder, via a
kernel-released advisory ``flock`` on
``<main-checkout>/.worklog/locks/main-checkout.lock``.

Covered here:
- pure lock helpers: acquire/release, per-checkout scope, holder metadata,
  second-holder refusal naming the first holder,
- kernel release on a SIGKILLed holder (no stale locks possible),
- ``phase_finish`` behaviour: refusal exits non-zero without touching the
  work item or commenting; a successful finish acquires and releases (a second
  finish can immediately re-acquire),
- the dev-push full-suite gate runs **outside** the lock (SA-0MWFJS3820032YB4):
  the lock is free while the suite runs, the verdict is keyed on the
  would-be-commit tree, a red verdict aborts before the lock, and two
  concurrent finishes overlap their suite runs instead of serialising them,
- ``scripts/hygiene_check.sh`` reports a held lock and tracked-file deletions
  in both human and ``--json`` output.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement
from skill.shared import git_sandbox

HYGIENE_SCRIPT = REPO_ROOT / "scripts" / "hygiene_check.sh"


# ---------------------------------------------------------------------------
# Lock helper behaviour
# ---------------------------------------------------------------------------


def test_lock_acquire_release_roundtrip(tmp_path):
    """Acquire, release, and re-acquire succeeds (no stale lock after exit)."""
    repo = str(tmp_path / "main")
    fd, holder = implement.acquire_main_checkout_lock(repo, "WL-HOLDER")
    assert fd is not None
    assert holder is None

    # Holder metadata written into the lock file.
    lock_path = implement._main_checkout_lock_path(repo)
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["work_item_id"] == "WL-HOLDER"
    assert data["pid"] == os.getpid()
    assert data["started_at"]

    implement._release_main_checkout_lock(fd)
    fd2, holder2 = implement.acquire_main_checkout_lock(repo, "WL-SECOND")
    assert fd2 is not None
    assert holder2 is None
    implement._release_main_checkout_lock(fd2)


def test_second_holder_refused_naming_first(tmp_path):
    """A held lock refuses a second holder, naming the first in the message."""
    repo = str(tmp_path / "main")
    fd, _ = implement.acquire_main_checkout_lock(repo, "WL-FIRST")
    assert fd is not None
    try:
        fd2, holder = implement.acquire_main_checkout_lock(repo, "WL-SECOND")
        assert fd2 is None
        assert "WL-FIRST" in holder
        assert str(os.getpid()) in holder or "pid" in holder
    finally:
        implement._release_main_checkout_lock(fd)
    # Released; a third holder can proceed.
    fd3, _ = implement.acquire_main_checkout_lock(repo, "WL-THIRD")
    assert fd3 is not None
    implement._release_main_checkout_lock(fd3)


def test_lock_is_per_checkout(tmp_path):
    """Locks in different repositories are independent (per-checkout scope)."""
    repo_a = str(tmp_path / "a")
    repo_b = str(tmp_path / "b")
    fd_a, _ = implement.acquire_main_checkout_lock(repo_a, "WL-A")
    fd_b, _ = implement.acquire_main_checkout_lock(repo_b, "WL-B")
    assert fd_a is not None and fd_b is not None
    implement._release_main_checkout_lock(fd_a)
    implement._release_main_checkout_lock(fd_b)


def test_lock_released_on_holder_sigkill(tmp_path):
    """A SIGKILLed holder releases the flock via the kernel (no stale lock).

    Proves the AC4 crash-release property: the advisory lock is an ``flock``
    on the lock file's open file description, which the kernel tears down
    when the holding process dies — no PID bookkeeping to trust.
    """
    repo = str(tmp_path / "main")
    ready = tmp_path / "ready"
    holder_pid = os.fork()
    if holder_pid == 0:
        # Child: acquire the lock, signal readiness, then idle until killed.
        fd, holder = implement.acquire_main_checkout_lock(repo, "WL-KILLED")
        if fd is None:
            os._exit(3)
        try:
            ready.write_text("ready", encoding="utf-8")
        except OSError:
            os._exit(4)
        try:
            while True:
                time.sleep(1)
        except BaseException:  # noqa: BLE001 - child teardown
            os._exit(0)

    try:
        # Wait for the child to hold the lock, then check contention.
        deadline = time.monotonic() + 15
        while not ready.exists():
            if time.monotonic() > deadline:
                raise AssertionError("holder child never signalled readiness")
            time.sleep(0.05)
        fd, holder = implement.acquire_main_checkout_lock(repo, "WL-CONTENDER")
        assert fd is None
        assert "WL-KILLED" in holder
        implement._release_main_checkout_lock(fd)  # no-op, fd is None

        # SIGKILL the holder — the kernel must release the flock.
        os.kill(holder_pid, signal.SIGKILL)
        os.waitpid(holder_pid, 0)

        fd2, holder2 = implement.acquire_main_checkout_lock(repo, "WL-RECLAIM")
        assert fd2 is not None, f"lock not released after SIGKILL: {holder2}"
        implement._release_main_checkout_lock(fd2)
    finally:
        try:
            os.kill(holder_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(holder_pid, 0)
        except ChildProcessError:
            pass


# ---------------------------------------------------------------------------
# phase_finish integration
# ---------------------------------------------------------------------------


#: Stable full commit hash the fake git resolves every revision to. The
#: verified local-dev fast-forward and the post-push ancestry assertion
#: (SA-0MVPZG7A0000N66U) resolve refs with ``git rev-parse --verify``; a
#: resolvable published commit keeps both paths real instead of tripping the
#: fail-closed assertion (covered separately by
#: ``tests/test_implement_finish_local_dev_verified_sync.py``).
_FAKE_COMMIT = "1" * 40


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    stdout = f"{_FAKE_COMMIT}\n" if len(cmd) > 1 and cmd[1] == "rev-parse" else ""
    if len(cmd) > 1 and cmd[1] == "merge-base":
        # A freshly-committed feature branch is not yet on origin/dev:
        # the pre-push already-landed check (SA-0MW22SEMV00230O5) must see
        # an unpushed commit and proceed to the push.
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")


def _build_result() -> dict:
    return {"success": True, "stdout": "no build", "stderr": "", "exit_code": 0,
            "skipped": True}


def _tests_result() -> dict:
    return {"success": True, "failures": [], "skipped": False, "tooling": "suite",
            "scope": "changed", "notices": []}


def _cleanup_result() -> dict:
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


@contextlib.contextmanager
def _finish_harness(tmp_path, repo_root: str | None = None):
    """Patch every external boundary of ``phase_finish`` (no real git)."""
    worktree = str(tmp_path / "wt")
    Path(worktree).mkdir(parents=True, exist_ok=True)
    state = implement.ImplementState(
        work_item_id="WL-TEST",
        worktree_path=worktree,
        repo_root=repo_root or str(tmp_path / "main"),
        parent_branch="dev",
    )
    update_status = MagicMock()
    store_show = {"workItem": {"status": "completed", "stage": "in_review"}}
    show = MagicMock(return_value=store_show)

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=worktree)
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(patch.object(implement, "run_build", return_value=_build_result()))
        # ``phase_finish`` runs its gate against the would-be-commit tree
        # (``run_gate_suite``, SA-0MVQHVGFR006W8LG) and stages the change set
        # in the real index through ``stage_tested_tree`` once green. Both act
        # on a real git repository; this harness drives ``phase_finish`` from a
        # bare temp directory, so both boundaries are stubbed green (the gate's
        # own behaviour is covered by ``test_finish_gate_*.py``).
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
                return_value={
                    "success": True,
                    "tree": "abc1234",
                    "expected": "abc1234",
                },
            )
        )
        stack.enter_context(patch.object(implement, "run_tests", return_value=_tests_result()))
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        # The post-push ancestry assertion (Step 6a) resolves against the
        # stubbed main checkout; model a published commit so the terminal
        # path is exercised (the assertion's own fail-closed behaviour has
        # dedicated coverage in test_implement_finish_local_dev_verified_sync.py,
        # SA-0MVPZG7A0000N66U).
        stack.enter_context(
            patch.object(
                implement,
                "_assert_commit_on_origin_dev",
                return_value={
                    "verified": True,
                    "resolved_commit": _FAKE_COMMIT,
                    "target": "refs/remotes/origin/dev",
                    "target_tip": _FAKE_COMMIT,
                },
            )
        )
        # The push subject is the synthetic hash ``abc1234`` in this harness, so
        # the worktree-holding guard (SA-0MVUTXJ4300301ZN) is stubbed like the
        # push itself; it has dedicated coverage in
        # ``skill/implement/tests/test_implement_push_from_worktree.py``.
        stack.enter_context(patch.object(implement, "_assert_push_subject"))
        stack.enter_context(patch.object(implement, "run_cmd", side_effect=_fake_run_cmd))
        stack.enter_context(patch.object(implement, "_record_finish_own_churn", return_value=[]))
        stack.enter_context(
            patch.object(
                implement, "cleanup_worktree_processes", return_value=_cleanup_result()
            )
        )
        stack.enter_context(patch.object(implement, "remove_state"))
        stack.enter_context(patch.object(implement, "_remove_worktree", return_value=True))
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        comment_mock = stack.enter_context(
            patch.object(implement, "wl_add_comment", return_value=True)
        )
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(patch.object(implement, "_snapshot_main_checkout",
                                          return_value={"taken_at": "T", "status": {}}))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", update_status)
        )
        stack.enter_context(patch.object(implement.StatusLifecycle, "show", show))
        yield {"update_status": update_status, "comment": comment_mock}


def test_finish_refuses_when_lock_held_by_other_finish(tmp_path):
    """A finish against a locked checkout exits non-zero, naming the holder.

    The refusal must NOT modify the work item (no status writes) and must not
    comment — AC3: "does not modify the checkout or the work item".

    Regression (SA-0MVY0G5EH000LIMB): when an outer ``implement.py finish``
    exports ``IMPLEMENT_FINISH_LOCK_TIMEOUT``, the test's own lock-hold +
    ``phase_finish`` call used to sleep for the full inherited duration before
    refusing. The ``_no_ambient_lock_timeout`` fixture neutralises the ambient
    var, and the elapsed-time assertion catches any regression.
    """
    start = time.monotonic()
    with _finish_harness(tmp_path) as mocks:
        repo = str(tmp_path / "main")
        fd, _ = implement.acquire_main_checkout_lock(repo, "WL-OTHER")
        assert fd is not None
        try:
            report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)
        finally:
            implement._release_main_checkout_lock(fd)

    elapsed = time.monotonic() - start
    # The fixture neutralises inherited timeouts; refusal is instant.
    assert elapsed < 5, f"Lock refusal took {elapsed:.1f}s — ambient timeout not neutralised?"

    assert report["success"] is False
    assert "WL-OTHER" in report["message"]
    assert "main-checkout.lock" in report["message"]
    # No work-item mutation and no comment on refusal.
    assert mocks["update_status"].call_args_list == []
    assert mocks["comment"].call_args_list == []
    lock_step = report["steps"]["main_checkout_lock"]
    assert lock_step["acquired"] is False
    assert "WL-OTHER" in lock_step["holder"]


def test_finish_success_acquires_and_releases_lock(tmp_path):
    """A successful finish records the lock step and releases on exit."""
    repo = str(tmp_path / "main")
    with _finish_harness(tmp_path, repo_root=repo):
        report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    lock_step = report["steps"]["main_checkout_lock"]
    assert lock_step["acquired"] is True

    # Released on exit: a fresh acquisition succeeds immediately.
    fd, holder = implement.acquire_main_checkout_lock(repo, "WL-AFTER")
    assert fd is not None, f"lock not released after successful finish: {holder}"
    implement._release_main_checkout_lock(fd)


# ---------------------------------------------------------------------------
# The full-suite gate runs OUTSIDE the lock (SA-0MWFJS3820032YB4)
# ---------------------------------------------------------------------------
#
# The dev-push full-suite gate used to be reached only through the push, i.e.
# while the exclusive main-checkout lock was held, so concurrent finishes
# serialised their ~5 min suite runs. ``finish`` now resolves the tree-keyed
# verdict for the would-be-commit tree *before* it takes the lock; the
# pre-push hook then serves that verdict instead of executing the suite. These
# tests pin the ordering (observed from inside the suite run, never from
# source greps) and the abort path (a red verdict never reaches the lock).


def _probe_lock_free_during_full_scope(repo: str, observed: list[tuple[str, bool]]):
    """A ``run_tests`` stand-in that observes the lock across a full run.

    A full-scope invocation tries a non-blocking acquisition of the real
    main-checkout lock. ``True`` means the lock was free while the suite ran;
    ``False`` means it was held — the regression this item fixes.
    """

    def _fake(cwd, scope="changed", base_ref=None, fingerprint=None):
        if scope != "full":
            return _tests_result()
        fd, _holder = implement.acquire_main_checkout_lock(repo, "WL-PROBE")
        observed.append(("full_suite", fd is not None))
        if fd is not None:
            implement._release_main_checkout_lock(fd)
        return {
            **_tests_result(),
            "scope": "full",
            "tooling": "suite",
            "git_fingerprint": fingerprint,
        }

    return _fake


def test_prelock_full_suite_runs_while_the_lock_is_free(tmp_path):
    """AC: the full suite is never executed while ``main-checkout.lock`` is held.

    The lock state is observed from *inside* the full-suite invocation (the
    suite's own non-blocking acquisition of the real lock file), which is the
    property the item exists to guarantee.
    """
    repo = str(tmp_path / "main")
    observed: list[tuple[str, bool]] = []
    with _finish_harness(tmp_path, repo_root=repo), patch.object(
        implement, "run_tests",
        side_effect=_probe_lock_free_during_full_scope(repo, observed),
    ):
        report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert observed == [("full_suite", True)], (
        "the dev-push full suite must run exactly once, with the main-checkout "
        f"lock free (observed {observed!r})"
    )
    prelock = report["steps"]["full_suite_prelock"]
    assert prelock["attempted"] is True
    assert prelock["success"] is True
    assert prelock["verdict"] == "green"
    assert prelock["scope"] == "full"
    assert prelock["lock_held"] is False


def test_prelock_verdict_is_bound_to_the_would_be_commit_tree(tmp_path):
    """The pre-lock verdict is keyed on the tree the commit will produce.

    The hook's ``--gate-commit`` run derives the pushed commit's tree; only a
    verdict keyed on that *same* tree is served. Anything else would either
    re-run the suite inside the lock or, worse, trust a verdict about another
    tree.
    """
    repo = str(tmp_path / "main")
    calls: list[dict] = []

    def _fake(cwd, scope="changed", base_ref=None, fingerprint=None):
        calls.append(
            {"cwd": cwd, "scope": scope, "fingerprint": fingerprint}
        )
        return {**_tests_result(), "scope": scope}

    with _finish_harness(tmp_path, repo_root=repo):
        with patch.object(implement, "run_tests", side_effect=_fake):
            report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    full_runs = [call for call in calls if call["scope"] == "full"]
    assert len(full_runs) == 1, full_runs
    assert full_runs[0]["fingerprint"] == "abc1234", (
        "the pre-lock run must be keyed on the changed-scope gate's "
        "would-be-commit tree"
    )
    assert full_runs[0]["cwd"] == str(tmp_path / "wt"), (
        "the verdict must land in the cache of the checkout the push runs from"
    )
    assert report["steps"]["full_suite_prelock"]["tree"] == "abc1234"
    assert report["steps"]["full_suite_prelock"]["fingerprint_kind"] == "tree"


def test_green_finish_lock_window_excludes_the_full_suite(tmp_path):
    """AC: a green finish's lock window does not include the suite duration.

    Ordering is recorded as events rather than inferred from timing: the suite
    starts and finishes, and only then is the lock acquired.
    """
    repo = str(tmp_path / "main")
    events: list[tuple[str, ...]] = []
    real_acquire = implement.acquire_main_checkout_lock

    def _fake_run_tests(cwd, scope="changed", base_ref=None, fingerprint=None):
        if scope != "full":
            return _tests_result()
        events.append(("suite", "start"))
        events.append(("suite", "end"))
        return {**_tests_result(), "scope": "full"}

    def _acquire(repo_root, work_item_id, timeout=0.0):
        outcome = real_acquire(repo_root, work_item_id, timeout=timeout)
        events.append(("lock", "acquired"))
        return outcome

    with _finish_harness(tmp_path, repo_root=repo), patch.object(
        implement, "run_tests", side_effect=_fake_run_tests
    ), patch.object(
        implement, "acquire_main_checkout_lock", side_effect=_acquire
    ):
        report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    assert events == [
        ("suite", "start"),
        ("suite", "end"),
        ("lock", "acquired"),
    ], f"the lock must be taken only after the suite finished (observed {events!r})"
    # The bounded-wait evidence the operator reads is recorded for the lock.
    assert report["steps"]["main_checkout_lock"]["waited_seconds"] >= 0.0
    assert report["steps"]["full_suite_prelock"]["lock_held"] is False


def test_red_prelock_verdict_aborts_before_taking_the_lock(tmp_path):
    """A red dev-push verdict aborts before the lock — nothing is published.

    The gate's verdict still gates the push; it is simply resolved earlier.
    Nothing may be committed, no lock may be taken and the item must be
    released to ``open``.
    """
    repo = str(tmp_path / "main")
    lock_calls: list[str] = []
    red = {
        "success": False,
        "failures": ["tests/test_example.py::test_broken"],
        "scope": "full",
        "tooling": "suite",
        "notices": [],
    }
    real_acquire = implement.acquire_main_checkout_lock

    def _fake_run_tests(cwd, scope="changed", base_ref=None, fingerprint=None):
        return red if scope == "full" else _tests_result()

    def _acquire(repo_root, work_item_id, timeout=0.0):
        lock_calls.append(work_item_id)
        return real_acquire(repo_root, work_item_id, timeout=timeout)

    with _finish_harness(tmp_path, repo_root=repo) as mocks, patch.object(
        implement, "run_tests", side_effect=_fake_run_tests
    ), patch.object(
        implement, "acquire_main_checkout_lock", side_effect=_acquire
    ), patch.object(implement, "git_commit") as commit:
        report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False
    prelock = report["steps"]["full_suite_prelock"]
    assert prelock["verdict"] == "red"
    assert prelock["reason"] == "full_suite_red"
    assert prelock["failure_kind"] == implement.GATE_FAILURE_TESTS
    assert "main_checkout_lock" not in report["steps"], (
        "a red dev-push verdict must abort before the lock is taken"
    )
    assert lock_calls == []
    commit.assert_not_called()
    assert "test_broken" in report["message"]
    # The item is released to open and the failure is on the record.
    assert any(
        call.args[:2] == ("WL-TEST", "open")
        for call in mocks["update_status"].call_args_list
    ), mocks["update_status"].call_args_list
    assert mocks["comment"].call_count == 1
    # And the finish-failure classifier treats it as a gate failure, never as
    # a retryable infrastructure refusal (drive must not spin on a red suite).
    classified = implement.classify_finish_failure(report)
    assert classified["kind"] == "gate", classified
    assert classified["step"] == "full_suite_prelock"


def test_prelock_skipped_without_a_would_be_commit_tree(tmp_path):
    """No tree to bind the verdict to → stand down, the hook still gates.

    Outside a git work tree there is no would-be-commit tree, so the verdict
    cannot be tree-keyed in advance. Finish then behaves exactly as before
    (the hook runs the suite), and the report says why.
    """
    repo = str(tmp_path / "main")
    ran: list[str] = []

    def _fake_run_tests(cwd, scope="changed", base_ref=None, fingerprint=None):
        ran.append(scope)
        return _tests_result()

    no_tree = {
        "available": False,
        "reason": "not_a_git_worktree",
        "notice": "no would-be-commit tree",
        "staged_paths": [],
    }
    with _finish_harness(tmp_path, repo_root=repo), patch.object(
        implement, "run_gate_suite", return_value=(_tests_result(), no_tree)
    ), patch.object(implement, "run_tests", side_effect=_fake_run_tests):
        report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    prelock = report["steps"]["full_suite_prelock"]
    assert prelock["attempted"] is False
    assert prelock["skipped_reason"] == "no_would_be_commit_tree"
    assert prelock["lock_held"] is False
    assert ran == [], "no full-scope run may happen without a tree to bind it to"


def test_prelock_skipped_when_the_dev_push_gate_is_bypassed(tmp_path, monkeypatch):
    """``TEST_SCOPE_SKIP=1`` skips the pre-resolution, never the hook's gate."""
    monkeypatch.setenv("TEST_SCOPE_SKIP", "1")
    repo = str(tmp_path / "main")
    ran: list[str] = []

    def _fake_run_tests(cwd, scope="changed", base_ref=None, fingerprint=None):
        ran.append(scope)
        return _tests_result()

    with _finish_harness(tmp_path, repo_root=repo):
        with patch.object(implement, "run_tests", side_effect=_fake_run_tests):
            report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    prelock = report["steps"]["full_suite_prelock"]
    assert prelock["attempted"] is False
    assert prelock["skipped_reason"] == "test_scope_skip"
    assert ran == []


def test_prelock_helper_skips_branches_without_a_full_suite_gate(monkeypatch):
    """Only ``dev``/``main`` pushes are gated by the full suite."""
    called: list[str] = []
    monkeypatch.setattr(
        implement,
        "run_tests",
        lambda *args, **kwargs: called.append("run") or _tests_result(),
    )

    verdict = implement.resolve_prelock_full_suite_verdict(
        "/nonexistent", {"tree": "a" * 40}, parent_branch="release"
    )

    assert verdict["attempted"] is False
    assert verdict["skipped_reason"] == "branch_not_gated"
    assert called == []


def test_concurrent_finishes_overlap_their_full_suite_runs(tmp_path, monkeypatch):
    """A concurrent finish no longer waits for another's full-suite run.

    Two finishes run concurrently against one main checkout. Each must be
    able to *enter* its full-suite run while the other is inside its own (the
    barrier) and must find the main-checkout lock free while running it. Under
    the previous ordering — the suite executed by the pre-push hook inside the
    exclusive lock — the second finish could never reach its suite until the
    first released: the barrier would break and this test would fail.

    A bounded lock wait is configured because the two finishes still contend
    for the short *mutating* window; that is the invariant this item keeps.
    """
    monkeypatch.setenv("IMPLEMENT_FINISH_LOCK_TIMEOUT", "30")
    repo = str(tmp_path / "main")
    #: Both finishes must be inside their suite at the same time.
    inside_suite = threading.Barrier(2, timeout=30)
    observed: list[tuple[str, bool]] = []
    observed_lock = threading.Lock()

    def _fake_run_tests(cwd, scope="changed", base_ref=None, fingerprint=None):
        if scope != "full":
            return _tests_result()
        fd, _holder = implement.acquire_main_checkout_lock(repo, "WL-PROBE")
        with observed_lock:
            observed.append(("full_suite", fd is not None))
        if fd is not None:
            implement._release_main_checkout_lock(fd)
        inside_suite.wait()
        return {**_tests_result(), "scope": "full"}

    reports: dict[str, dict] = {}

    def _finish(label: str) -> None:
        reports[label] = implement.phase_finish(
            "WL-TEST", json_output=True, no_refactor=True
        )

    with _finish_harness(tmp_path, repo_root=repo):
        with patch.object(implement, "run_tests", side_effect=_fake_run_tests):
            threads = [
                threading.Thread(target=_finish, args=(label,), daemon=True)
                for label in ("a", "b")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            assert not any(t.is_alive() for t in threads), (
                "a concurrent finish did not complete — the full-suite runs "
                "were serialised on the main-checkout lock"
            )

    assert set(reports) == {"a", "b"}
    for label, report in reports.items():
        assert report["success"] is True, (label, report["message"])
        assert report["steps"]["main_checkout_lock"]["acquired"] is True
        # The wait was for the short mutating window only, bounded by the
        # configured timeout — never the other finish's suite duration.
        assert report["steps"]["main_checkout_lock"]["waited_seconds"] < 30
    assert observed == [("full_suite", True), ("full_suite", True)], (
        "both finishes must observe a FREE main-checkout lock while their "
        f"full suite runs (observed {observed!r})"
    )


# ---------------------------------------------------------------------------
# hygiene_check.sh integration
# ---------------------------------------------------------------------------


def _run_hygiene(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HYGIENE_SCRIPT), "--repo-root", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_hygiene_reports_held_lock_and_deletions_json(tmp_path):
    """`--json` reports a held lock and tracked-file deletions (AC6)."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    deleted = repo / "packages" / "shared" / "src.ts"
    deleted.parent.mkdir(parents=True)
    deleted.write_text("x\n", encoding="utf-8")
    git_sandbox.commit_all(repo, "seed")
    deleted.unlink()  # tracked-file deletion in the main checkout

    fd, _ = implement.acquire_main_checkout_lock(str(repo), "WL-HYGIENE")
    assert fd is not None
    try:
        proc = _run_hygiene(repo, "--json")
    finally:
        implement._release_main_checkout_lock(fd)

    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    lock_check = next(l for l in lines if l.get("check") == "main_checkout_lock")
    assert lock_check["status"] == "warning"
    assert "WL-HYGIENE" in lock_check["holder"]
    deletion_check = next(l for l in lines if l.get("check") == "tracked_deletions")
    assert deletion_check["status"] == "warning"
    assert "packages/shared/src.ts" in deletion_check["deletions"]
    assert proc.returncode == 1


def test_hygiene_reports_held_lock_and_deletions_human(tmp_path):
    """Human output mentions the holder and the deleted file."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    deleted = repo / "src.ts"
    deleted.write_text("x\n", encoding="utf-8")
    git_sandbox.commit_all(repo, "seed")
    deleted.unlink()

    fd, _ = implement.acquire_main_checkout_lock(str(repo), "WL-HYGIENE-HUMAN")
    assert fd is not None
    try:
        proc = _run_hygiene(repo)
    finally:
        implement._release_main_checkout_lock(fd)

    assert "WL-HYGIENE-HUMAN" in proc.stdout
    assert "packages/shared/src.ts" in proc.stdout or "src.ts" in proc.stdout
    assert proc.returncode == 1


def test_hygiene_ok_when_lock_free_and_clean(tmp_path):
    """No lock, no deletions → both new checks report ok."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    (repo / "keep.txt").write_text("x\n", encoding="utf-8")
    git_sandbox.commit_all(repo, "seed")
    proc = _run_hygiene(repo, "--json")
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    lock_check = next(l for l in lines if l.get("check") == "main_checkout_lock")
    deletion_check = next(l for l in lines if l.get("check") == "tracked_deletions")
    assert lock_check["status"] == "ok"
    assert deletion_check["status"] == "ok"
    assert proc.returncode == 0
