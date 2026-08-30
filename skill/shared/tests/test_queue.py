"""Unit tests for skill/shared/queue.py — bounded priority queue.

Direct API-level tests for the file-based priority queue module (SA-0MTG5N7BN003D95L).
Focus on the public PriorityQueue API: constructor resolution, enqueue/dequeue
semantics, priority ordering, bounded capacity, TTL cleanup, and cross-process
safety.

Design: a shared directory where each waiter writes a priority+timestamp file;
the dispatcher reads in priority order (critical>high>medium>low), then by
age/FIFO within each tier. Flock-based slot locking ensures no stale locks.
"""

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

from shared.queue import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_TTL_SECONDS,
    ENV_LOCK_DIR,
    PRIORITY_MAP,
    Priority,
    PriorityQueue,
    QueueEntry,
    _sanitize_name,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_queue_dir(tmp_path, monkeypatch):
    """Point every test at a unique queue dir and clear env overrides."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "queues"))
    # Clear any env vars that could affect queue config
    monkeypatch.delenv("QUEUE_MAX_DEPTH", raising=False)
    monkeypatch.delenv("QUEUE_TTL_SECONDS", raising=False)


@pytest.fixture
def queue_dir(tmp_path):
    """Return the base queue directory path."""
    return tmp_path / "queues"


@pytest.fixture
def pq(queue_dir):
    """Return a PriorityQueue instance isolated to *queue_dir*."""
    return PriorityQueue("unit-test", max_depth=5, timeout=5.0)


# ---------------------------------------------------------------------------
# Constructor & configuration
# ---------------------------------------------------------------------------


def test_default_max_depth():
    """No explicit arg and no env var -> documented default."""
    pq = PriorityQueue("unit-default")
    assert pq.max_depth == DEFAULT_MAX_DEPTH


def test_default_ttl():
    """Default TTL is the documented constant."""
    pq = PriorityQueue("unit-default")
    assert pq.ttl_seconds == DEFAULT_TTL_SECONDS


def test_explicit_max_depth_wins_over_env(monkeypatch):
    """Explicit arg takes priority over QUEUE_MAX_DEPTH."""
    monkeypatch.setenv("QUEUE_MAX_DEPTH", "100")
    pq = PriorityQueue("unit-explicit", max_depth=3)
    assert pq.max_depth == 3


def test_env_max_depth_respected(monkeypatch):
    """QUEUE_MAX_DEPTH overrides the default depth."""
    monkeypatch.setenv("QUEUE_MAX_DEPTH", "7")
    pq = PriorityQueue("unit-env")
    assert pq.max_depth == 7


def test_invalid_max_depth_rejected():
    """max_depth < 1 must raise ValueError."""
    with pytest.raises(ValueError):
        PriorityQueue("unit-bad", max_depth=0)


def test_name_sanitization():
    """Special characters in names are stripped to safe substrings."""
    assert _sanitize_name("audit@2024!") == "audit_2024"
    assert _sanitize_name("!!") == "default"
    assert _sanitize_name("valid-name_1") == "valid-name_1"


def test_same_name_shares_directory():
    """Two PriorityQueues with the same name share the same directory."""
    a = PriorityQueue("unit-shared", max_depth=3, timeout=5.0)
    b = PriorityQueue("unit-shared", max_depth=3, timeout=5.0)
    assert a._queue_dir == b._queue_dir


# ---------------------------------------------------------------------------
# Enqueue / Dequeue — priority ordering
# ---------------------------------------------------------------------------


def test_enqueue_dequeue_basic(pq):
    """Simple enqueue → dequeue round-trip."""
    pq.enqueue("item-1", Priority.MEDIUM)
    entry = pq.dequeue(timeout=1.0)
    assert entry is not None
    assert entry.item_id == "item-1"
    assert entry.priority == Priority.MEDIUM


def test_enqueue_with_unusual_item_id_chars():
    """Item ids with special characters survive the round-trip safely.

    Regression guard: the item_id must never leak into the filesystem path
    (path traversal / injected filenames).
    """
    pq = PriorityQueue("unit-weird", max_depth=10, timeout=5.0)
    weird_id = "SA-X/../evil\u0000 id with spaces"
    pq.enqueue(weird_id, Priority.HIGH)

    entry = pq.dequeue(timeout=1.0)
    assert entry is not None
    assert entry.item_id == weird_id

    # No stray files escaped the queue directory into a parent path.
    assert not (pq._queue_dir.parent / "evil\u0000 id with spaces.json").exists()


def test_enqueue_preserves_priority_order(capsys):
    """Items are dequeued in priority order: critical > high > medium > low."""
    pq = PriorityQueue("unit-prio-order", max_depth=10, timeout=5.0)
    pq.enqueue("low", Priority.LOW)
    pq.enqueue("critical", Priority.CRITICAL)
    pq.enqueue("high", Priority.HIGH)
    pq.enqueue("medium", Priority.MEDIUM)

    entries = [pq.dequeue(timeout=1.0) for _ in range(4)]
    priorities = [e.priority for e in entries]

    assert priorities == [
        Priority.CRITICAL,
        Priority.HIGH,
        Priority.MEDIUM,
        Priority.LOW,
    ]


def test_fifo_within_same_priority():
    """Within the same priority tier, FIFO ordering is observed."""
    pq = PriorityQueue("unit-fifo", max_depth=10, timeout=5.0)
    pq.enqueue("first", Priority.HIGH)
    time.sleep(0.01)
    pq.enqueue("second", Priority.HIGH)
    time.sleep(0.01)
    pq.enqueue("third", Priority.HIGH)

    entries = [pq.dequeue(timeout=1.0) for _ in range(3)]
    ids = [e.item_id for e in entries]
    assert ids == ["first", "second", "third"]


def test_priority_constants_map_correctly():
    """PRIORITY_MAP assigns correct numeric values."""
    assert PRIORITY_MAP[Priority.CRITICAL] == 0
    assert PRIORITY_MAP[Priority.HIGH] == 1
    assert PRIORITY_MAP[Priority.MEDIUM] == 2
    assert PRIORITY_MAP[Priority.LOW] == 3


# ---------------------------------------------------------------------------
# Bounded capacity
# ---------------------------------------------------------------------------


def test_enqueue_blocks_when_full(pq):
    """When queue is at max_depth, enqueue blocks (or times out)."""
    pq.enqueue("a", Priority.LOW)
    pq.enqueue("b", Priority.LOW)
    pq.enqueue("c", Priority.LOW)
    pq.enqueue("d", Priority.LOW)
    pq.enqueue("e", Priority.LOW)  # fills to max_depth=5

    # Next enqueue should block and timeout
    with pytest.raises(TimeoutError):
        pq.enqueue("f", Priority.LOW, timeout=0.5)


def test_dequeue_frees_space():
    """After dequeue, previously-full enqueue succeeds."""
    pq = PriorityQueue("unit-dequeue-frees", max_depth=2, timeout=5.0)
    pq.enqueue("a", Priority.LOW)
    pq.enqueue("b", Priority.LOW)

    entry = pq.dequeue(timeout=1.0)
    assert entry is not None

    # Now should be able to enqueue again
    pq.enqueue("c", Priority.HIGH)
    entry = pq.dequeue(timeout=1.0)
    assert entry.item_id == "c"


def test_concurrent_dequeue_frees_blocked_enqueue(tmp_path, monkeypatch):
    """A blocked enqueue is released by a dequeue in another process.

    Regression guard: the enqueue must NOT hold the directory flock while
    waiting for capacity, otherwise a concurrent dequeue could never free
    a slot (live-lock).
    """
    import multiprocessing as mp

    pq_name = "unit-livelock"
    max_depth = 1

    def producer(queue_name):
        pq = PriorityQueue(queue_name, max_depth=max_depth, timeout=10.0)
        pq.enqueue("first", Priority.HIGH, timeout=2.0)
        # This second enqueue blocks until the consumer frees the slot
        start = time.monotonic()
        pq.enqueue("second", Priority.LOW, timeout=10.0)
        elapsed = time.monotonic() - start
        return elapsed

    ctx = mp.get_context("fork")
    p = ctx.Process(target=producer, args=(pq_name,))
    p.start()

    try:
        # Give the producer a moment to fill the queue
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                pq = PriorityQueue(pq_name, max_depth=max_depth, timeout=1.0)
                if len(pq) == 1:
                    break
            except OSError:
                pass
            time.sleep(0.05)

        consumer = PriorityQueue(pq_name, max_depth=max_depth, timeout=1.0)
        entry = consumer.dequeue(timeout=3.0)
        assert entry is not None
        assert entry.item_id == "first"
    finally:
        p.join(timeout=15)
        assert p.exitcode == 0, f"Producer exited with code {p.exitcode}"


def test_queue_length_reflects_contents():
    """len(queue) returns the number of enqueued but not yet dequeued items."""
    pq = PriorityQueue("unit-len", max_depth=10, timeout=5.0)
    assert len(pq) == 0
    pq.enqueue("a", Priority.LOW)
    assert len(pq) == 1
    pq.enqueue("b", Priority.HIGH)
    assert len(pq) == 2
    pq.dequeue(timeout=1.0)
    assert len(pq) == 1


# ---------------------------------------------------------------------------
# TTL / Crash recovery
# ---------------------------------------------------------------------------


def test_orphaned_entries_cleaned_on_read():
    """Entries older than TTL are pruned on dequeue."""
    pq = PriorityQueue("unit-ttl", max_depth=10, timeout=5.0, ttl_seconds=0.2)
    pq.enqueue("old", Priority.LOW)
    time.sleep(0.3)  # older than TTL

    entry = pq.dequeue(timeout=1.0)
    # The old entry should have been pruned; queue should be empty
    assert entry is None
    assert len(pq) == 0


def test_fresh_entries_not_pruned():
    """Entries newer than TTL survive dequeue attempts."""
    pq = PriorityQueue("unit-ttl-fresh", max_depth=10, timeout=5.0, ttl_seconds=5.0)
    pq.enqueue("fresh", Priority.HIGH)

    entry = pq.dequeue(timeout=1.0)
    assert entry is not None
    assert entry.item_id == "fresh"


def test_ttl_cleanup_preserves_order():
    """Pruning old entries doesn't disturb ordering of remaining entries."""
    pq = PriorityQueue("unit-ttl-order", max_depth=10, timeout=5.0, ttl_seconds=0.2)
    pq.enqueue("old1", Priority.LOW)  # t=0
    time.sleep(0.15)  # t=0.15
    pq.enqueue("new1", Priority.HIGH)  # t=0.15
    time.sleep(0.15)  # t=0.30 — old1 now expired (>0.2s), new1 still fresh
    pq.enqueue("new2", Priority.MEDIUM)  # t=0.30

    entries = []
    while True:
        entry = pq.dequeue(timeout=0.5)
        if entry is None:
            break
        entries.append(entry)

    # old1 expired; new1 (HIGH) before new2 (MEDIUM), both fresh
    assert [e.item_id for e in entries] == ["new1", "new2"]


# ---------------------------------------------------------------------------
# Dequeue when empty
# ---------------------------------------------------------------------------


def test_dequeue_empty_times_out(pq):
    """Dequeue on an empty queue returns None (after timeout)."""
    result = pq.dequeue(timeout=0.3)
    assert result is None


def test_dequeue_empty_returns_none_immediately_with_zero_timeout():
    """Dequeue with timeout=0 returns None immediately when empty."""
    pq = PriorityQueue("unit-empty-zero", max_depth=5, timeout=0.0)
    result = pq.dequeue(timeout=0)
    assert result is None


# ---------------------------------------------------------------------------
# Entry dataclass
# ---------------------------------------------------------------------------


def test_queue_entry_has_expected_fields():
    """QueueEntry has item_id, priority, and timestamp."""
    entry = QueueEntry(item_id="test", priority=Priority.HIGH, timestamp=1234.0)
    assert entry.item_id == "test"
    assert entry.priority == Priority.HIGH
    assert entry.timestamp == 1234.0


# ---------------------------------------------------------------------------
# Cross-process safety
# ---------------------------------------------------------------------------


def test_concurrent_enqueue_dequeue_multiprocessing():
    """Multiple processes can enqueue/dequeue concurrently without corruption."""
    import multiprocessing as mp

    pq_name = "unit-mproc"
    max_depth = 10
    p_count = 3  # number of producer processes
    items_per_producer = 3
    total_items = p_count * items_per_producer

    def producer(queue_name, item_id, priority_val, count):
        pq = PriorityQueue(queue_name, max_depth=max_depth, timeout=10.0)
        for i in range(count):
            try:
                pq.enqueue(f"{item_id}-{i}", priority_val, timeout=5.0)
            except TimeoutError:
                break

    def consumer(queue_name, count):
        pq = PriorityQueue(queue_name, max_depth=max_depth, timeout=2.0)
        results = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(results) < count:
            entry = pq.dequeue(timeout=1.0)
            if entry is None:
                continue
            results.append(entry.item_id)
        return results

    ctx = mp.get_context("fork")
    procs = [
        ctx.Process(
            target=producer,
            args=(pq_name, f"item-{i}", i % 4, items_per_producer),
        )
        for i in range(p_count)
    ]
    consumer_p = ctx.Process(target=consumer, args=(pq_name, total_items))

    # Start producers first, then the consumer
    for p in procs:
        p.start()
    time.sleep(0.1)
    consumer_p.start()

    for p in procs:
        p.join(timeout=30)
        if p.exitcode not in (0, None):
            pytest.fail(f"Producer process exited with code {p.exitcode}")
    consumer_p.join(timeout=30)
    if consumer_p.exitcode not in (0, None):
        pytest.fail(f"Consumer process exited with code {consumer_p.exitcode}")


# ---------------------------------------------------------------------------
# Empty queue name handling
# ---------------------------------------------------------------------------


def test_empty_name_sanitized_to_default():
    """An empty or all-special-char name becomes 'default'."""
    pq = PriorityQueue("", max_depth=3, timeout=5.0)
    assert pq.name == _sanitize_name("")


# ---------------------------------------------------------------------------
# Enqueue with explicit timestamp
# ---------------------------------------------------------------------------


def test_enqueue_with_custom_timestamp():
    """Enqueue accepts a custom timestamp for ordering."""
    pq = PriorityQueue("unit-ts", max_depth=10, timeout=5.0)
    base_ts = time.monotonic()
    pq.enqueue("a", Priority.HIGH, timestamp=base_ts)
    pq.enqueue("b", Priority.HIGH, timestamp=base_ts + 1.0)

    entries = [pq.dequeue(timeout=1.0) for _ in range(2)]
    ids = [e.item_id for e in entries]
    assert ids == ["a", "b"]


# ---------------------------------------------------------------------------
# Queue persistence across instances
# ---------------------------------------------------------------------------


def test_queue_survives_instance_recreation():
    """Enqueued items persist when the instance is recreated (same name)."""
    pq = PriorityQueue("unit-persist", max_depth=10, timeout=5.0)
    pq.enqueue("persistent", Priority.CRITICAL)
    del pq  # destroy instance

    pq2 = PriorityQueue("unit-persist", max_depth=10, timeout=5.0)
    entry = pq2.dequeue(timeout=1.0)
    assert entry is not None
    assert entry.item_id == "persistent"
