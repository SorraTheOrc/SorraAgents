"""Integration test: audit slot acquisition honours its timeout under lock
contention (SA-0MUV4C6RZ006GN5Z AC6 / child SA-0MV0J0XXR008ZE6E).

The real-world consumer of the bounded ``PriorityQueue`` lock is
``audit_runner._acquire_audit_slot``.  These tests hold the audit queue's
``.lock`` file from a second process and assert that admission raises
``TimeoutError`` within ``AUDIT_QUEUE_TIMEOUT`` rather than blocking for the
hold duration, and that admission proceeds normally once the lock is free.

No pi model call is made — only the queue/semaphore admission path is
exercised.
"""

import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT_FOR_TESTS = REPO_ROOT / "skill"
if str(_SKILLS_ROOT_FOR_TESTS) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT_FOR_TESTS))

from audit.scripts import audit_runner
from shared.process_semaphore import ENV_LOCK_DIR
from shared.queue import PriorityQueue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_queue_and_semaphore(tmp_path, monkeypatch):
    """Isolate queue + semaphore lock dirs and set fail-fast host lock.

    ``AUDIT_LOCK_TIMEOUT=0`` and ``AUDIT_HOST_LOCK_TIMEOUT=0`` keep the
    semaphore from masking the queue-directory-lock behaviour under test.
    """
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.setenv("AUDIT_LOCK_TIMEOUT", "0")
    monkeypatch.setenv("AUDIT_HOST_LOCK_TIMEOUT", "0")
    monkeypatch.delenv("AUDIT_MAX_CONCURRENCY", raising=False)


def _audit_lock_path() -> str:
    """Absolute path of the audit queue's ``.lock`` file."""
    queue = PriorityQueue(audit_runner.AUDIT_QUEUE_NAME)
    return str(queue._queue_dir / ".lock")


def _lock_holder(lock_path: str, hold_seconds: float = 6.0) -> None:
    """Hold an exclusive flock on *lock_path* for *hold_seconds* seconds."""
    import fcntl

    Path(lock_path).touch(exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        time.sleep(hold_seconds)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_acquire_audit_slot_times_out_under_lock_contention(monkeypatch):
    """_acquire_audit_slot raises TimeoutError within AUDIT_QUEUE_TIMEOUT.

    A background process holds the audit queue's directory lock.  With a
    short ``AUDIT_QUEUE_TIMEOUT`` the admission path must raise within that
    bound, not block for the full 6-second hold (AC2).
    """
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "0.5")
    lock_path = _audit_lock_path()
    Path(lock_path).touch(exist_ok=True)

    ctx = mp.get_context("fork")
    holder = ctx.Process(target=_lock_holder, args=(lock_path, 6.0))
    holder.start()
    try:
        time.sleep(0.3)  # let the holder acquire the lock

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            audit_runner._acquire_audit_slot(
                "TEST-LOCK-CONTENTION",
                priority=audit_runner.Priority.MEDIUM,
                max_concurrency=1,
            )
        elapsed = time.monotonic() - start

        assert elapsed < 3.0, (
            f"_acquire_audit_slot blocked for {elapsed:.2f}s under lock "
            f"contention — expected within ~AUDIT_QUEUE_TIMEOUT (lock held "
            f"for 6s)"
        )
    finally:
        holder.join(timeout=15)
        assert holder.exitcode == 0, f"Holder exited with code {holder.exitcode}"


def test_acquire_audit_slot_proceeds_when_lock_released(monkeypatch):
    """Once the peer releases the lock, admission proceeds normally (AC3)."""
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "5")
    lock_path = _audit_lock_path()
    Path(lock_path).touch(exist_ok=True)

    ctx = mp.get_context("fork")
    # Hold only briefly, then release — admission should succeed after.
    holder = ctx.Process(target=_lock_holder, args=(lock_path, 0.5))
    holder.start()

    try:
        holder.join(timeout=15)
        assert holder.exitcode == 0, f"Holder exited with code {holder.exitcode}"

        start = time.monotonic()
        sem = audit_runner._acquire_audit_slot(
            "TEST-LOCK-RELEASED",
            priority=audit_runner.Priority.MEDIUM,
            max_concurrency=1,
        )
        elapsed = time.monotonic() - start
        try:
            assert sem is not None
            # The queue ticket must have been removed on successful admission.
            queue = PriorityQueue(audit_runner.AUDIT_QUEUE_NAME)
            remaining = [e.item_id for e in queue._list_entries()]
            assert not any("TEST-LOCK-RELEASED" in e for e in remaining), remaining
        finally:
            sem.release()

        assert elapsed < 5.0, f"admission took too long: {elapsed:.2f}s"
    finally:
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=15)
