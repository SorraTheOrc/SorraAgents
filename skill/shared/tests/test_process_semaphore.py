"""Unit tests for skill/shared/process_semaphore.py (SA-0MSAK2P3J0065POO).

Direct API-level tests complementing the cross-process guard suite
(skill/shared/tests/test_concurrency_guard.py). Focus on the public
Semaphore API: constructor resolution, acquire/release semantics, env-var
ceiling, timeout/fail-fast, and cleanup. Cross-process behaviour is covered
by the guard suite.
"""


import os
import random
import sys
import threading
import time
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
    nested_semaphore_dir,
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


# ---------------------------------------------------------------------------
# Arrival-ordered turnstile (SA-0MVSJX5HS002DJHI) — caller compatibility
# ---------------------------------------------------------------------------


def test_fail_fast_acquires_immediately_when_a_slot_is_free():
    """timeout=0 succeeds at once (audit_runner's semantics are preserved)."""
    sem = Semaphore("unit-failfast-free", max_workers=1, timeout=0)

    t0 = time.monotonic()
    assert sem.acquire() is True
    elapsed = time.monotonic() - t0
    sem.release()

    assert elapsed < 1.0, f"fail-fast acquire queued for {elapsed:.2f}s"
    assert sem.last_ticket is None, "fail-fast callers are exempt from the turnstile"


def test_fail_fast_does_not_queue_behind_an_outstanding_ticket():
    """A fail-fast caller must not block behind a queued waiter's ticket."""
    name = "unit-failfast-busy"
    holder = Semaphore(name, max_workers=1, timeout=5)
    holder.acquire()
    queued = Semaphore(name, max_workers=1, timeout=5)
    finished: list[bool] = []

    def _wait() -> None:
        try:
            queued.acquire()
            finished.append(True)
            queued.release()
        except TimeoutError:
            finished.append(False)

    thread = threading.Thread(target=_wait, daemon=True)
    try:
        thread.start()
        # Wait until the queued waiter holds a ticket (it is genuinely waiting).
        deadline = time.monotonic() + 5.0
        while queued.last_ticket is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queued.last_ticket is not None, "waiter never took a ticket"

        fast = Semaphore(name, max_workers=1, timeout=0)
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            fast.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, (
            f"fail-fast caller queued behind a ticket for {elapsed:.2f}s"
        )
    finally:
        holder.release()
        thread.join(timeout=10)

    # The queued waiter acquired once the holder released the slot (no deadlock).
    assert finished == [True]


def test_nested_namespace_never_queues_in_the_host_turnstile(tmp_path, monkeypatch):
    """A nested run isolated via PI_SEMAPHORE_DIR must not see host tickets."""
    name = "unit-nested"
    host = Semaphore(name, max_workers=1, timeout=5)
    host.acquire()
    host_waiters: list[bool] = []
    host_waiter = Semaphore(name, max_workers=1, timeout=10)

    def _wait() -> None:
        try:
            host_waiter.acquire()
            host_waiters.append(True)
            host_waiter.release()
        except TimeoutError:
            host_waiters.append(False)

    thread = threading.Thread(target=_wait, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 5.0
        while host_waiter.last_ticket is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert host_waiter.last_ticket is not None

        # The nested namespace has its own slots AND its own turnstile.
        monkeypatch.setenv(ENV_LOCK_DIR, str(nested_semaphore_dir(tmp_path / "nested")))
        nested = Semaphore(name, max_workers=1, timeout=2)
        t0 = time.monotonic()
        assert nested.acquire() is True
        nested.release()
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, (
            f"nested run queued in the host turnstile for {elapsed:.2f}s"
        )
        assert nested._slot_dir != host._slot_dir
        assert str(nested._slot_dir).startswith(str(tmp_path / "nested"))
    finally:
        host.release()
        thread.join(timeout=10)

    assert host_waiters == [True]


def test_turnstile_reclaims_a_lower_ticket_left_by_a_dead_waiter(tmp_path):
    """A stale (unlocked) lower ticket must not block a later waiter."""
    sem = Semaphore("unit-reclaim", max_workers=1, timeout=2)
    sem._ensure_slots()
    stale = sem._turnstile_dir / "0"
    stale.write_text("", encoding="utf-8")  # created, never locked: dead waiter

    t0 = time.monotonic()
    assert sem.acquire() is True
    sem.release()

    assert time.monotonic() - t0 < 1.0
    assert not stale.exists(), "an unlocked lower ticket must be reclaimed"


# ---------------------------------------------------------------------------
# Dynamic slot discovery (SA-0MVWKZ4QD006L2E0) — failing-first regressions
#
# A waiter that started when the namespace held only slots ``0..N-1`` must
# still acquire a slot created later by a higher-ceiling process. The discovery
# assertions below run against the dynamic-discovery implementation; the
# strict ``xfail`` markers that pinned them test-first have been removed by the
# implementation child. The cadence and own-ceiling assertions stay active too,
# so a reversion to the snapshot-ceiling probe fails loudly.
# ---------------------------------------------------------------------------


def _hold_slots(sem_name, max_workers, count):
    """Acquire *count* distinct slots of *sem_name*; return the holders."""
    holders = []
    for _ in range(count):
        sem = Semaphore(sem_name, max_workers=max_workers, timeout=5)
        sem.acquire()
        holders.append(sem)
    return holders


def _create_free_slots(sem, indices):
    """Create unheld slot files for *indices* in *sem*'s namespace."""
    sem._slot_dir.mkdir(parents=True, exist_ok=True)
    for index in indices:
        fd = os.open(sem._slot_path(index), os.O_CREAT | os.O_RDWR, 0o644)
        os.close(fd)


def _count_attempts(sem):
    """Wrap *sem*'s slot probe so the number of poll attempts is observable."""
    attempts: list[float] = []
    original = sem._try_acquire_slot

    def counting():
        attempts.append(time.monotonic())
        return original()

    sem._try_acquire_slot = counting  # instance override shadows the method
    return attempts


def _wait_for_attempts(attempts, minimum, timeout=10.0):
    """Block until *attempts* has at least *minimum* entries (or time out)."""
    deadline = time.monotonic() + timeout
    while len(attempts) < minimum and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(attempts) >= minimum, (
        f"waiter made only {len(attempts)} poll attempts, expected >= {minimum}"
    )


def test_waiter_acquires_slot_created_after_it_started_polling():
    """AC1: a max_workers=2 waiter acquires a slot created after it began.

    The waiter starts when only slots 0 and 1 exist (and are held). Slots 2..7
    are then created free; the waiter must acquire one of the new slots well
    inside its timeout, rather than probing only ``range(2)`` and waiting out
    the entire bound.
    """
    name = "unit-discovery-late"
    holders = _hold_slots(name, max_workers=2, count=2)
    waiter = Semaphore(name, max_workers=2, timeout=10)
    attempts = _count_attempts(waiter)
    acquired: list[bool] = []

    def _wait() -> None:
        try:
            waiter.acquire()
            acquired.append(True)
        except TimeoutError:
            acquired.append(False)

    thread = threading.Thread(target=_wait, daemon=True)
    try:
        thread.start()
        # Let it poll the two held slots at least twice before capacity grows.
        _wait_for_attempts(attempts, 2)
        _create_free_slots(waiter, range(2, 8))

        deadline = time.monotonic() + 5.0
        while not acquired and time.monotonic() < deadline:
            time.sleep(0.01)
        assert acquired == [True], (
            "waiter never probed slots created after it started"
        )
    finally:
        for holder in holders:
            holder.release()
        thread.join(timeout=10)
        waiter.release()


def test_waiter_acquires_slot_created_after_many_poll_cycles():
    """AC4: a slot created after at least three poll cycles is still observed."""
    name = "unit-discovery-many-polls"
    holders = _hold_slots(name, max_workers=2, count=2)
    waiter = Semaphore(name, max_workers=2, timeout=10)
    attempts = _count_attempts(waiter)
    acquired: list[bool] = []

    def _wait() -> None:
        try:
            waiter.acquire()
            acquired.append(True)
        except TimeoutError:
            acquired.append(False)

    thread = threading.Thread(target=_wait, daemon=True)
    try:
        thread.start()
        _wait_for_attempts(attempts, 3)
        _create_free_slots(waiter, range(2, 6))

        deadline = time.monotonic() + 5.0
        while not acquired and time.monotonic() < deadline:
            time.sleep(0.01)
        assert acquired == [True], (
            "waiter stopped observing slots after many poll cycles"
        )
    finally:
        for holder in holders:
            holder.release()
        thread.join(timeout=10)
        waiter.release()


def test_max_poll_interval_is_bounded():
    """AC2: the poll cadence is 50 ms base, at most 75 ms jittered.

    No backoff may grow without a documented cap; the maximum interval is
    pinned so a future semaphore change cannot silently introduce unbounded
    waiting.
    """
    from shared.process_semaphore import (
        _RETRY_DELAY_SECONDS,
        _jittered_retry_delay,
    )

    assert _RETRY_DELAY_SECONDS == 0.05
    rng = random.Random(0)
    for _ in range(500):
        delay = _jittered_retry_delay(rng)
        assert 0.5 * _RETRY_DELAY_SECONDS <= delay <= 1.5 * _RETRY_DELAY_SECONDS
        assert delay <= 0.075, f"poll interval exceeds the documented cap: {delay}"


def test_long_waiter_makes_many_poll_attempts():
    """AC2: a bounded waiter polls many times, not once."""
    name = "unit-poll-many"
    holder = Semaphore(name, max_workers=1, timeout=5)
    holder.acquire()
    waiter = Semaphore(name, max_workers=1, timeout=1.0)
    attempts = _count_attempts(waiter)
    try:
        with pytest.raises(TimeoutError):
            waiter.acquire()
    finally:
        holder.release()
    assert len(attempts) > 3, f"waiter made only {len(attempts)} poll attempts"


def test_waiter_creates_no_slots_beyond_its_own_ceiling():
    """AC3: a max_workers=2 waiter creates only slots 0 and 1.

    Discovery may use slots another process created, but a waiter must never
    create more slot files than its own ``max_workers`` — its ceiling is a
    floor it guarantees, not a host-wide limit it may exceed.
    """
    waiter = Semaphore("unit-own-ceiling", max_workers=2, timeout=1)
    waiter._ensure_slots()
    created = sorted(
        int(p.name) for p in waiter._slot_dir.iterdir() if p.name.isdigit()
    )
    assert created == [0, 1]


def test_fail_fast_timeout_zero_is_one_scan_and_one_attempt(monkeypatch):
    """AC3: ``timeout=0`` stays fail-fast — one scan plus one attempt, no sleep.

    The dynamic discovery scan must not add blocking work to the first
    attempt: ``audit_runner`` enters with ``timeout=0`` and relies on an
    immediate succeed-or-raise.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    sem = Semaphore("unit-failfast-once", max_workers=2, timeout=0)
    attempts = _count_attempts(sem)
    assert sem.acquire() is True
    sem.release()

    assert len(attempts) == 1, f"fail-fast made {len(attempts)} slot probes"
    assert sleeps == [], f"fail-fast slept: {sleeps}"
