"""Integration tests: audit runner + shared bounded priority queue.

Covers SA-0MTG5RYH8005RQNM (integrate the audit runner with the shared
bounded priority queue):

- AC1: audits wait on the priority queue instead of failing fast when the
  concurrency ceiling is saturated.
- AC2: priority ordering is enforced among queued/contending launches
  (critical/high before medium before low).
- AC3: bounded wait (not fail-fast) when the queue itself is at capacity.
- AC4: admission logging exposes queued_at, priority, queue_position,
  dequeued_at, wait_seconds.
- AC6: integration tests live here (plus ordering coverage in
  test_audit_runner_concurrency.py for the ceiling guards).
"""

import json
import re
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path (mirrors tests/conftest.py).
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner
from shared.process_semaphore import ENV_LOCK_DIR, ENV_MAX_WORKERS
from shared.queue import Priority, PriorityQueue


@pytest.fixture(autouse=True)
def _isolate_queue_and_semaphore(tmp_path, monkeypatch):
    """Isolate queue + semaphore lock dirs and clear env per test."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.delenv(ENV_MAX_WORKERS, raising=False)
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("AUDIT_QUEUE_TIMEOUT", raising=False)


class _MockProcess:
    """Fake Popen return value: communicates canned JSON then exits."""

    def __init__(self, stdout_text=None, delay=0.0):
        if stdout_text is None:
            inner = json.dumps({"verdict": "met", "evidence": "ok"})
            stdout_text = json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_end", "content": inner},
                }
            )
        self._text = stdout_text
        self._delay = delay
        self.returncode = 0

    def communicate(self, timeout=None):
        if self._delay:
            time.sleep(self._delay)
        return self._text, ""

    def kill(self):
        pass


def _mock_popen(delay=0.0):
    """Return a mock.patch context for subprocess.Popen."""
    return mock.patch.object(
        audit_runner.subprocess,
        "Popen",
        return_value=_MockProcess(delay=delay),
    )


# ---------------------------------------------------------------------------
# AC1: wait, don't fail fast — a queued launch proceeds when the slot frees
# ---------------------------------------------------------------------------


def test_queued_launch_proceeds_when_slot_frees(monkeypatch):
    """With the single slot held, a second launch waits (no fail-fast) and
    then completes with `met` once the slot is released."""
    from shared.process_semaphore import Semaphore

    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "30")

    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()

    def _free_after_delay():
        time.sleep(0.25)
        sem.release()

    releaser = threading.Thread(target=_free_after_delay)
    releaser.start()
    try:
        with _mock_popen():
            start = time.monotonic()
            result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
            elapsed = time.monotonic() - start
    finally:
        releaser.join(timeout=10)

    assert result.get("verdict") == "met", result
    assert result.get("_concurrency_timeout") is not True
    # It genuinely waited for the freed slot (not fail-fast, not instant).
    assert elapsed >= 0.15, f"launch did not queue-wait: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# AC2: priority ordering among contending launches
# ---------------------------------------------------------------------------


def test_priority_ordering_under_contention(monkeypatch, capsys):
    """Under saturation the next freed slot goes to the highest-priority
    waiter, not whoever polls first.

    Setup: one LOW-priority launch holds the single slot; then HIGH,
    MEDIUM and LOW launches contend. Admission order must be
    LOW(holder) -> HIGH -> MEDIUM -> LOW.
    """
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "30")

    monkeypatch.setattr(
        audit_runner.subprocess,
        "Popen",
        lambda *a, **k: _MockProcess(delay=0.5),
    )

    holder = threading.Thread(
        target=audit_runner._call_pi,
        args=("p",),
        kwargs={"model": "m", "pi_bin": "pi", "priority": Priority.LOW},
    )
    holder.start()
    time.sleep(0.2)  # let the holder enqueue + grasp the only slot

    results = {}

    def _contender(tag, prio):
        results[tag] = audit_runner._call_pi(
            "prompt", model="m", pi_bin="pi", priority=prio
        )

    threads = [
        threading.Thread(target=_contender, args=("hi", Priority.HIGH)),
        threading.Thread(target=_contender, args=("med", Priority.MEDIUM)),
        threading.Thread(target=_contender, args=("lo", Priority.LOW)),
    ]
    for t in threads:
        t.start()
    holder.join(timeout=30)
    for t in threads:
        t.join(timeout=30)

    # No fail-fast: every launch eventually acquired a slot.
    assert all(r.get("verdict") == "met" for r in results.values()), results

    lines = [
        l
        for l in capsys.readouterr().err.splitlines()
        if "Audit slot acquired" in l
    ]
    assert len(lines) == 4, f"expected 4 admission logs, got {len(lines)}"
    seq = [
        re.search(r"priority=(\w+)", l).group(1)
        for l in lines
    ]
    assert seq[0] == "low", f"slot holder must admit first: {seq}"
    assert seq[1:] == ["high", "medium", "low"], (
        f"priority ordering violated: {seq}"
    )


def test_same_priority_stays_fifo(monkeypatch, capsys):
    """Within one priority tier, earlier enqueues admit first (FIFO)."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "30")

    monkeypatch.setattr(
        audit_runner.subprocess,
        "Popen",
        lambda *a, **k: _MockProcess(delay=0.3),
    )

    results = []

    def _run(tag):
        results.append(
            audit_runner._call_pi(
                "prompt", model="m", pi_bin="pi", priority=Priority.MEDIUM
            )
        )

    t1 = threading.Thread(target=_run, args=("first",))
    t2 = threading.Thread(target=_run, args=("second",))
    t1.start()
    time.sleep(0.1)  # t1 enqueues first (same priority -> timestamp wins)
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert all(r.get("verdict") == "met" for r in results)

    lines = [
        l
        for l in capsys.readouterr().err.splitlines()
        if "Audit slot acquired" in l
    ]
    assert len(lines) == 2
    # The first enqueued thread must own the first ticket slot in the log.
    tickets = [l.split("ticket=")[-1] for l in lines]
    # Counter order == enqueue order; admission order must match it.
    counters = [int(t.split(":")[-1]) for t in tickets]
    assert counters == sorted(counters), f"FIFO violated: {counters}"


# ---------------------------------------------------------------------------
# AC3: bounded wait when the queue itself is at capacity
# ---------------------------------------------------------------------------


def test_queue_full_waits_bounded_then_reports_unmet(monkeypatch):
    """Filling the audit queue to max_depth makes a new launch wait (not
    fail-fast) on capacity; when the bound expires it returns the graceful
    unmet concurrency verdict (no crash)."""
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "0.3")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)


    # Fill the shared audit queue to capacity with inert tickets.
    queue = PriorityQueue(audit_runner.AUDIT_QUEUE_NAME)
    max_depth = queue.max_depth
    for i in range(max_depth):
        queue.enqueue(f"fill:{i}", Priority.MEDIUM, timeout=5)

    try:
        start = time.monotonic()
        with _mock_popen():
            result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
        elapsed = time.monotonic() - start
    finally:
        for i in range(max_depth):
            queue.remove(f"fill:{i}")

    assert result.get("verdict") == "unmet", result
    assert "concurr" in result.get("evidence", "").lower()
    assert result.get("_concurrency_timeout") is True
    # Bounded: waited for capacity (not fail-fast) but within the bound.
    assert elapsed >= 0.1, f"queue-full did not wait: {elapsed:.2f}s"
    assert elapsed < 5.0, f"queue-full wait exceeded bound: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# AC4: admission logging fields
# ---------------------------------------------------------------------------


def test_admission_log_fields_are_complete(monkeypatch, capsys):
    """The admission log exposes queued_at, priority, queue_position,
    dequeued_at and wait_seconds (AC4)."""
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)

    with _mock_popen():
        audit_runner._call_pi(
            "prompt", model="m", pi_bin="pi",
            issue_id="SA-TEST-AC4", priority=Priority.HIGH,
        )

    err = capsys.readouterr().err
    line = next(l for l in err.splitlines() if "Audit slot acquired" in l)
    assert re.search(r"queued_at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", line), line
    assert "priority=high" in line, line
    assert re.search(r"queue_position=\d+", line), line
    assert re.search(r"dequeued_at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", line), line
    assert re.search(r"wait_seconds=\d+\.\d{2}", line), line
    assert "ticket=audit:SA-TEST-AC4:" in line, line


def test_admission_log_queue_position_matches_ordering(monkeypatch, capsys):
    """queue_position reflects the launch's place among pending waiters.

    The holder is admitted with position 1 (queue empty at its enqueue);
    a HIGH contender enqueues while the holder is ACTIVE (position 1 of
    the pending waiters); a LOW contender enqueues behind it (position 2).
    Admission order is holder -> HIGH -> LOW, and each line records its
    own position at enqueue time.
    """
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "30")

    monkeypatch.setattr(
        audit_runner.subprocess,
        "Popen",
        lambda *a, **k: _MockProcess(delay=0.35),
    )

    holder = threading.Thread(
        target=audit_runner._call_pi,
        args=("p",),
        kwargs={"model": "m", "pi_bin": "pi", "priority": Priority.LOW},
    )
    holder.start()
    time.sleep(0.15)  # holder admitted + active with the only slot

    results = {}

    def _contend(tag, prio):
        results[tag] = audit_runner._call_pi(
            "prompt", model="m", pi_bin="pi", priority=prio
        )

    hi = threading.Thread(target=_contend, args=("hi", Priority.HIGH))
    lo = threading.Thread(target=_contend, args=("lo", Priority.LOW))
    hi.start()
    time.sleep(0.05)  # high enqueues first -> position 1 of waiters
    lo.start()
    hi.join(timeout=30)
    lo.join(timeout=30)
    holder.join(timeout=30)

    assert all(r.get("verdict") == "met" for r in results.values())

    lines = [
        l
        for l in capsys.readouterr().err.splitlines()
        if "Audit slot acquired" in l
    ]
    assert len(lines) == 3, lines
    # Admission order: holder (low), then high, then low.
    seq = [re.search(r"priority=(\w+)", l).group(1) for l in lines]
    assert seq == ["low", "high", "low"], f"admission order: {seq}"
    positions = [int(re.search(r"queue_position=(\d+)", l).group(1)) for l in lines]
    # Holder saw an empty queue (position 1); high queued first among the
    # waiters (1); low queued behind high (2).
    assert positions == [1, 1, 2], f"positions: {positions}"