"""Unit tests for skill/shared/process_semaphore.py (SA-0MSAK2P3J0065POO).

Direct API-level tests complementing the cross-process guard suite
(skill/shared/tests/test_concurrency_guard.py). Focus on the public
Semaphore API: constructor resolution, acquire/release semantics, env-var
ceiling, timeout/fail-fast, and cleanup. Cross-process behaviour is covered
by the guard suite.
"""


import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT_FOR_TESTS = REPO_ROOT / "skill"
if str(_SKILLS_ROOT_FOR_TESTS) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT_FOR_TESTS))
import pytest
from shared.process_semaphore import (
    DEFAULT_MAX_WORKERS,
    ENV_LOCK_DIR,
    ENV_MAX_WORKERS,
    Semaphore,
    jittered_deadline,
)


@pytest.fixture(autouse=True)
def _isolate_lock_dir(tmp_path, monkeypatch):
    """Point every test at a unique lock dir and clear env overrides."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.delenv(ENV_MAX_WORKERS, raising=False)


def test_default_max_workers():
    """No explicit arg and no env var -> documented default."""
    sem = Semaphore("unit-default")
    assert sem.max_workers == DEFAULT_MAX_WORKERS
    assert sem.max_workers == 6


def test_explicit_max_workers_wins_over_env(monkeypatch):
    """Explicit arg takes priority over AUDIT_MAX_CONCURRENCY."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    sem = Semaphore("unit-explicit", max_workers=4)
    assert sem.max_workers == 4


def test_env_ceiling_respected(monkeypatch):
    """AUDIT_MAX_CONCURRENCY overrides the default ceiling."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "3")
    sem = Semaphore("unit-env")
    assert sem.max_workers == 3


def test_invalid_max_workers_rejected():
    """max_workers < 1 must raise ValueError."""
    with pytest.raises(ValueError):
        Semaphore("unit-bad", max_workers=0)


def test_acquire_and_release_roundtrip():
    """acquire() returns True and release() is idempotent."""
    sem = Semaphore("unit-roundtrip", max_workers=1, timeout=5)
    assert sem.acquire() is True
    assert sem._held_fd is not None
    sem.release()
    assert sem._held_fd is None
    sem.release()  # second release is a no-op


def test_reentrant_acquire_is_noop():
    """acquire() while already held returns True without taking another slot."""
    sem = Semaphore("unit-reentrant", max_workers=1, timeout=5)
    with sem:
        assert sem.acquire() is True
    # One release from the context manager suffices.
    assert sem._held_fd is None


def test_context_manager_releases_on_success():
    """`with` block releases the slot on normal exit."""
    sem = Semaphore("unit-ctx", max_workers=1, timeout=5)
    with sem:
        assert sem._held_fd is not None
    assert sem._held_fd is None


def test_context_manager_releases_on_exception():
    """`with` block releases the slot even when an exception propagates."""
    sem = Semaphore("unit-ctx-exc", max_workers=1, timeout=5)
    with pytest.raises(RuntimeError), sem:
        raise RuntimeError("boom")
    assert sem._held_fd is None


def test_same_name_shares_slot_pool():
    """Two Semaphores with the same name share the same slot directory."""
    a = Semaphore("unit-shared", max_workers=2, timeout=5)
    b = Semaphore("unit-shared", max_workers=2, timeout=5)
    assert a._slot_dir == b._slot_dir


# ---------------------------------------------------------------------------
# Staggered-wait jitter (SA-0MVEDOF7A004TSWD) — per-waiter desynchronisation
# ---------------------------------------------------------------------------


def test_jitter_default_off_keeps_historical_acquire():
    """jitter defaults to 0.0 so audit/other consumers are byte-identical."""
    sem = Semaphore("unit-jitter-default")
    assert sem.jitter == 0.0


def test_jitter_rejects_invalid_fractions():
    """jitter must be in [0, 1): >= 1 or negative is a programming error."""
    with pytest.raises(ValueError):
        Semaphore("unit-jitter-neg", jitter=-0.1)
    with pytest.raises(ValueError):
        Semaphore("unit-jitter-one", jitter=1.0)


def test_jittered_deadline_within_bound_never_exceeds():
    """Jittered deadline lies in [start + T*(1-j), start + T] — never beyond."""
    start, timeout = 1000.0, 600.0
    rng = random.Random(0)
    for _ in range(200):
        d = jittered_deadline(start, timeout, 0.1, rng)
        assert d <= start + timeout, f"deadline exceeded bound: {d}"
        assert d >= start + timeout * 0.9, f"deadline too early: {d}"


def test_jitter_zero_deadline_is_exact():
    """jitter=0.0 returns start + T unchanged (historical behaviour)."""
    assert jittered_deadline(10.0, 600.0, 0.0, random.Random(1)) == 610.0


def test_jittered_deadline_staggers_across_waiters():
    """Different waiters (different RNG streams) get different deadlines."""
    start, timeout = 0.0, 100.0
    d1 = jittered_deadline(start, timeout, 0.5, random.Random(1))
    d2 = jittered_deadline(start, timeout, 0.5, random.Random(2))
    assert d1 != d2, "waiters share the same deadline — herd not broken"


def test_jittered_deadline_never_negative_budget():
    """Even with jitter approaching 1 the deadline stays >= start."""
    start, timeout = 5.0, 10.0
    rng = random.Random(3)
    for _ in range(100):
        d = jittered_deadline(start, timeout, 0.9, rng)
        assert d >= start


def test_jittered_acquire_acquires_once_slot_frees():
    """Jitter must not starve a waiter: slot release -> bounded wait acquires."""
    holder = Semaphore("unit-jitter-free", max_workers=1, timeout=5)
    holder.acquire()
    try:
        waiter = Semaphore(
            "unit-jitter-free", max_workers=1, timeout=5, jitter=0.5,
            rng=random.Random(4),
        )
        # Bounded wait expires while the slot is held (no deadlock).
        with pytest.raises(TimeoutError):
            waiter.acquire(timeout=0.2)
    finally:
        holder.release()
    # Slot now free: the jittered waiter acquires immediately.
    waiter = Semaphore(
        "unit-jitter-free", max_workers=1, timeout=5, jitter=0.5,
        rng=random.Random(5),
    )
    assert waiter.acquire() is True
    waiter.release()
