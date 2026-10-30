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
        stack.enter_context(patch.object(implement, "run_tests", return_value=_tests_result()))
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
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
    """
    with _finish_harness(tmp_path) as mocks:
        repo = str(tmp_path / "main")
        fd, _ = implement.acquire_main_checkout_lock(repo, "WL-OTHER")
        assert fd is not None
        try:
            report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)
        finally:
            implement._release_main_checkout_lock(fd)

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
