"""Regression test: timeout on lock contention (cross-process).

When a second process holds the PriorityQueue directory lock, operations with
a bounded timeout must return/raise within that timeout — not block
indefinitely (SA-0MUV4C6RZ006GN5Z AC5).

Design: a background process acquires the queue's `.lock` file via a separate
flock and holds it for several seconds; the foreground process exercises
``peek(timeout=0)``, ``enqueue(timeout=0.5)``, and ``dequeue(timeout=0)``
while the lock is held, asserting bounded response times.
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

from shared.queue import ENV_LOCK_DIR, Priority, PriorityQueue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_queue_dir(tmp_path, monkeypatch):
    """Point every test at a unique queue dir and clear env overrides."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "queues"))
    monkeypatch.delenv("QUEUE_MAX_DEPTH", raising=False)
    monkeypatch.delenv("QUEUE_TTL_SECONDS", raising=False)


@pytest.fixture
def pq(tmp_path):
    """Return a PriorityQueue isolated to *tmp_path*."""
    return PriorityQueue("contention-test", max_depth=5, timeout=5.0)


# ---------------------------------------------------------------------------
# Helper: background process that holds the directory lock
# ---------------------------------------------------------------------------


def _lock_holder(lock_path: str, hold_seconds: float = 6.0) -> None:
    """Hold an exclusive flock on *lock_path* for *hold_seconds* seconds."""
    import fcntl

    # Ensure the file exists (may not have been created yet)
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


def test_peek_timeout_zero_returns_immediately_under_lock_contention():
    """peek(timeout=0) must not block past the lock-hold duration (AC1-2).

    A background process holds the queue's .lock file for 6 s.
    The foreground process calls peek(timeout=0) and asserts it returns
    within ~0.5 s, not for the full 6-second hold.
    """
    pq = PriorityQueue("contention-peek", max_depth=5, timeout=5.0)
    lock_path = str(pq._queue_dir / ".lock")

    # Ensure the lock file exists before the holder tries to open it
    (Path(pq._queue_dir) / ".lock").touch(exist_ok=True)

    ctx = mp.get_context("fork")
    holder = ctx.Process(target=_lock_holder, args=(lock_path, 6.0))
    holder.start()

    try:
        # Give the holder time to acquire
        time.sleep(0.3)

        start = time.monotonic()
        result = pq.peek(timeout=0)
        elapsed = time.monotonic() - start

        assert result is None  # queue is empty
        assert elapsed < 2.0, (
            f"peek(timeout=0) blocked for {elapsed:.2f}s — "
            f"contract: ~0s (lock held for 6s by holder)"
        )
    finally:
        holder.join(timeout=15)
        assert holder.exitcode == 0, f"Holder exited with code {holder.exitcode}"


def test_enqueue_timeout_raises_within_bound_under_lock_contention():
    """enqueue(timeout=0.5) must raise TimeoutError within the bound (AC3).

    A background process holds the directory lock.  Foreground calls
    enqueue(timeout=0.5) which should raise TimeoutError within ~1 s,
    not wait for the 6-second hold.
    """
    pq = PriorityQueue("contention-enq", max_depth=5, timeout=5.0)
    lock_path = str(pq._queue_dir / ".lock")

    ctx = mp.get_context("fork")
    holder = ctx.Process(target=_lock_holder, args=(lock_path, 6.0))
    holder.start()

    try:
        time.sleep(0.3)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            pq.enqueue("blocked-item", Priority.LOW, timeout=0.5)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, (
            f"enqueue(timeout=0.5) took {elapsed:.2f}s to raise — "
            f"expected < 2s (lock held for 6s)"
        )
    finally:
        holder.join(timeout=15)
        assert holder.exitcode == 0, f"Holder exited with code {holder.exitcode}"


def test_dequeue_timeout_zero_returns_none_under_lock_contention():
    """dequeue(timeout=0) must return None immediately under lock contention (AC4)."""
    pq = PriorityQueue("contention-deq", max_depth=5, timeout=5.0)
    lock_path = str(pq._queue_dir / ".lock")

    ctx = mp.get_context("fork")
    holder = ctx.Process(target=_lock_holder, args=(lock_path, 6.0))
    holder.start()

    try:
        time.sleep(0.3)

        start = time.monotonic()
        result = pq.dequeue(timeout=0)
        elapsed = time.monotonic() - start

        assert result is None
        assert elapsed < 2.0, (
            f"dequeue(timeout=0) blocked for {elapsed:.2f}s — "
            f"contract: ~0s (lock held for 6s)"
        )
    finally:
        holder.join(timeout=15)
        assert holder.exitcode == 0, f"Holder exited with code {holder.exitcode}"
