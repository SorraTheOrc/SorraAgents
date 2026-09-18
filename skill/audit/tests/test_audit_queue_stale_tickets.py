"""Regression tests for stale audit-queue ticket hygiene.

Discovered during the WL-0MU6UL3XY001M3VT audit (2026-09-18): the audit
admission queue accumulated tickets from crashed/timed-out audits.  Ticket
files were only pruned by the 24h TTL, so a dead process's ticket stayed at
the head of the queue and every subsequent audit timed out with
"audit concurrency queue saturated", even though no audit was running.

Fixes under test:
  1. `_prune_dead_audit_tickets` removes tickets whose owning PID is gone
     (and only those), called at admission time and on the slow wait path.
  2. `_acquire_audit_slot` removes its OWN ticket before raising
     `TimeoutError`, so a timed-out audit never leaves a stale ticket.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner
from shared.process_semaphore import ENV_LOCK_DIR
from shared.queue import PriorityQueue


@pytest.fixture(autouse=True)
def _isolate_queue(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "0.2")
    monkeypatch.delenv("AUDIT_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    yield


def _fresh_queue(name: str) -> PriorityQueue:
    q = PriorityQueue(name, max_depth=50, timeout=0.2)
    for entry in q._list_entries():
        q.remove(entry.item_id)
    return q


def test_prune_removes_only_dead_pid_tickets():
    q = _fresh_queue("audit-test-prune-dead")
    dead_pid = 999_999  # far above any sane pid; will not exist
    q.enqueue(f"audit:TEST-A:{dead_pid}:0", 0)
    q.enqueue(f"audit:TEST-B:{__import__('os').getpid()}:0", 0)
    assert len(q) == 2

    pruned = audit_runner._prune_dead_audit_tickets(q)

    assert pruned == 1
    remaining = [e.item_id for e in q._list_entries()]
    assert remaining == [f"audit:TEST-B:{__import__('os').getpid()}:0"]


def test_prune_leaves_non_audit_and_malformed_entries():
    q = _fresh_queue("audit-test-prune-shapes")
    q.enqueue("SA-0MSOMEID", 0)           # batch-queue style id, no pid
    q.enqueue("audit:tooshort", 0)         # malformed ticket
    q.enqueue("audit:TEST-C:notanint:0", 0)  # non-numeric pid

    assert audit_runner._prune_dead_audit_tickets(q) == 0
    assert len(q) == 3


def test_acquire_audit_slot_removes_own_ticket_on_timeout():
    """A timed-out admission must not leave its ticket behind.

    The audit semaphore is flock-based (per-process), so saturation is
    simulated by patching ``Semaphore.acquire`` to raise ``TimeoutError``.
    """
    q = PriorityQueue(audit_runner.AUDIT_QUEUE_NAME)
    for entry in q._list_entries():
        q.remove(entry.item_id)

    with mock.patch.object(
        audit_runner.Semaphore, "acquire", side_effect=TimeoutError("busy"),
    ):
        with pytest.raises(TimeoutError, match="saturated"):
            audit_runner._acquire_audit_slot(
                "TEST-OWN-TICKET", priority=audit_runner.Priority.MEDIUM,
            )

    remaining = [e.item_id for e in q._list_entries()]
    assert remaining == [], f"stale ticket left behind: {remaining}"


def test_acquire_audit_slot_prunes_dead_peer_ticket():
    """A dead peer's head ticket must not block a live admission."""
    q = PriorityQueue(audit_runner.AUDIT_QUEUE_NAME)
    for entry in q._list_entries():
        q.remove(entry.item_id)
    dead_pid = 999_998
    q.enqueue(f"audit:DEAD-PEER:{dead_pid}:0", 0)

    # max_workers=1 with a single free slot lets admission succeed
    # immediately once the dead ticket is pruned.
    sem = audit_runner._acquire_audit_slot(
        "TEST-LIVE", priority=audit_runner.Priority.MEDIUM,
        max_concurrency=1,
    )
    try:
        remaining = [e.item_id for e in q._list_entries()]
        assert not any("DEAD-PEER" in e for e in remaining)
        assert not any("TEST-LIVE" in e for e in remaining)
    finally:
        sem.release()
