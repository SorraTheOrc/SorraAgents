"""Tests: audit runner batch drain mode.

Covers SA-0MTG5TP5Z008QBL5 (add batch drain mode to the audit runner):

- AC1: configurable N items (default 5) processed per dispatch window;
  batch activates when the batch queue holds >= 2 pending items, else
  single-item (backward compatible).
- AC2: strict priority + FIFO ordering among drained items.
- AC3: wall-clock budget enforcement (stop when budget exceeded or queue
  empty).
- AC4: the concurrency slot is released between items.
- AC5: observability metrics (batch_start, batch_end, items_processed,
  queue_remaining, items_included).
- AC6: integration-style coverage of the drain loop under saturation.

The batch drain operates on its OWN queue (``audit-batch``) holding
work-item ids — never ``audit:`` admission tickets — so it cannot disturb
a live launch's admission protocol (SA-0MTG5RYH8005RQNM).
"""

import json
import sys
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
    """Isolate queue + semaphore lock dirs and clear relevant env per test."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.delenv(ENV_MAX_WORKERS, raising=False)
    monkeypatch.delenv("AUDIT_QUEUE_TIMEOUT", raising=False)
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv(audit_runner.AUDIT_BATCH_MAX_ITEMS_ENV, raising=False)
    monkeypatch.delenv(audit_runner.AUDIT_BATCH_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(audit_runner.AUDIT_BATCH_DISABLE_ENV, raising=False)


def _batch_queue() -> PriorityQueue:
    return PriorityQueue(audit_runner.AUDIT_BATCH_QUEUE_NAME)


def _enqueue(item_id, priority, timestamp=None):
    _batch_queue().enqueue(item_id, priority, timestamp=timestamp)


def _recording_audit_one():
    """Return (records, audit_one) where audit_one records item/priority."""
    records = []

    def _audit_one(item_id, priority):
        records.append((item_id, priority))
        return 0

    return records, _audit_one


# ---------------------------------------------------------------------------
# AC1: multiple items per window + queue-depth activation threshold
# ---------------------------------------------------------------------------


def test_batch_drain_processes_multiple_items_in_one_window():
    """Three queued items are all drained in one pass (multi-item window)."""
    _enqueue("SA-A", Priority.MEDIUM, timestamp=1.0)
    _enqueue("SA-B", Priority.MEDIUM, timestamp=2.0)
    _enqueue("SA-C", Priority.MEDIUM, timestamp=3.0)

    records, audit_one = _recording_audit_one()
    metrics = audit_runner._batch_drain_cycle(audit_one, max_items=5, timeout=60)

    assert [i for i, _ in records] == ["SA-A", "SA-B", "SA-C"]
    assert metrics["items_processed"] == 3
    assert metrics["queue_remaining"] == 0
    assert metrics["items_included"] == ["SA-A", "SA-B", "SA-C"]


def test_batch_drain_activates_at_queue_depth_two():
    """Batch triggers only at queue depth >= AUDIT_BATCH_MIN_QUEUE_DEPTH."""
    assert audit_runner._batch_drain_should_run(0) is False
    assert audit_runner._batch_drain_should_run(1) is False
    assert audit_runner._batch_drain_should_run(2) is True
    assert audit_runner._batch_drain_should_run(5) is True


def test_batch_drain_empty_queue_is_noop():
    """An empty batch queue drains zero items without error."""
    records, audit_one = _recording_audit_one()
    metrics = audit_runner._batch_drain_cycle(audit_one, max_items=5, timeout=60)
    assert records == []
    assert metrics["items_processed"] == 0
    assert metrics["queue_remaining"] == 0


def test_batch_max_items_configurable_via_env(monkeypatch):
    """AUDIT_BATCH_MAX_ITEMS overrides the default ceiling (AC1)."""
    assert audit_runner._resolve_batch_max_items() == 5
    monkeypatch.setenv(audit_runner.AUDIT_BATCH_MAX_ITEMS_ENV, "3")
    assert audit_runner._resolve_batch_max_items() == 3
    monkeypatch.setenv(audit_runner.AUDIT_BATCH_MAX_ITEMS_ENV, "0")
    assert audit_runner._resolve_batch_max_items() == 1  # clamped
    monkeypatch.setenv(audit_runner.AUDIT_BATCH_MAX_ITEMS_ENV, "banana")
    assert audit_runner._resolve_batch_max_items() == 5  # invalid -> default


def test_batch_timeout_configurable_via_env(monkeypatch):
    """AUDIT_BATCH_TIMEOUT overrides the default 1800s budget (AC3)."""
    assert audit_runner._resolve_batch_timeout() == 1800.0
    monkeypatch.setenv(audit_runner.AUDIT_BATCH_TIMEOUT_ENV, "42")
    assert audit_runner._resolve_batch_timeout() == 42.0
    monkeypatch.setenv(audit_runner.AUDIT_BATCH_TIMEOUT_ENV, "banana")
    assert audit_runner._resolve_batch_timeout() == 1800.0  # invalid -> default


# ---------------------------------------------------------------------------
# AC2: strict priority + FIFO ordering
# ---------------------------------------------------------------------------


def test_batch_drain_priority_order_fifo_within_tier():
    """Drain order is critical > high > medium > low; FIFO within a tier."""
    _enqueue("SA-HI1", Priority.HIGH, timestamp=1.0)
    _enqueue("SA-CRIT", Priority.CRITICAL, timestamp=0.5)
    _enqueue("SA-MED", Priority.MEDIUM, timestamp=1.0)
    _enqueue("SA-LO", Priority.LOW, timestamp=1.0)
    _enqueue("SA-HI2", Priority.HIGH, timestamp=2.0)

    records, audit_one = _recording_audit_one()
    audit_runner._batch_drain_cycle(audit_one, max_items=10, timeout=60)

    assert [i for i, _ in records] == [
        "SA-CRIT", "SA-HI1", "SA-HI2", "SA-MED", "SA-LO",
    ]
    # Each drained item carried the priority it was queued at (AC2).
    assert dict(records)["SA-CRIT"] == Priority.CRITICAL
    assert dict(records)["SA-LO"] == Priority.LOW


# ---------------------------------------------------------------------------
# AC3: budget enforcement
# ---------------------------------------------------------------------------


def test_batch_drain_stops_at_max_items_any_priority():
    """More queued items than max_items: only max_items are drained, the
    rest stay queued for the next window."""
    for i in range(7):
        _enqueue(f"SA-{i}", Priority.MEDIUM, timestamp=float(i))

    records, audit_one = _recording_audit_one()
    metrics = audit_runner._batch_drain_cycle(audit_one, max_items=5, timeout=60)

    assert len(records) == 5
    assert metrics["items_processed"] == 5
    assert metrics["queue_remaining"] == 2


def test_batch_drain_stops_at_budget():
    """Wall-clock budget stops the loop mid-window: a slow audit_one eats
    the budget and remaining items stay queued."""
    import time

    for i in range(3):
        _enqueue(f"SA-{i}", Priority.MEDIUM, timestamp=float(i))

    records = []

    def _slow_audit_one(item_id, priority):
        records.append(item_id)
        time.sleep(0.4)  # each item burns most of the budget
        return 0

    # Budget 0.55s: item1 finishes at ~0.4, item2 at ~0.8; the loop-top
    # check after item2 (0.8 >= 0.55) stops before the 3rd item.
    metrics = audit_runner._batch_drain_cycle(
        _slow_audit_one, max_items=5, timeout=0.55
    )

    assert records == ["SA-0", "SA-1"]
    assert metrics["items_processed"] == 2
    assert metrics["queue_remaining"] == 1


# ---------------------------------------------------------------------------
# AC4: the concurrency slot is released between items
# ---------------------------------------------------------------------------

def test_batch_drain_releases_slot_between_items(monkeypatch):
    """With max_workers=1, each drained item's audit_one must acquire the
    audit semaphore successfully — proving the previous item released its
    slot before the next item was dequeued."""
    from shared.process_semaphore import Semaphore

    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    for i in range(3):
        _enqueue(f"SA-{i}", Priority.MEDIUM, timestamp=float(i))

    acquisitions = []

    def _slot_audit_one(item_id, priority):
        sem = Semaphore("audit", max_workers=1, timeout=5)
        acquired = sem.acquire(timeout=5)
        acquisitions.append(acquired)
        sem.release()
        return 0

    audit_runner._batch_drain_cycle(_slot_audit_one, max_items=5, timeout=60)

    assert acquisitions == [True, True, True], (
        f"slot not available between items: {acquisitions}"
    )


# ---------------------------------------------------------------------------
# AC5: observability metrics
# ---------------------------------------------------------------------------


def test_batch_drain_logs_metrics_line(capsys):
    """Each window emits one metrics line with all AC5 fields."""
    _enqueue("SA-1", Priority.MEDIUM, timestamp=1.0)
    _enqueue("SA-2", Priority.MEDIUM, timestamp=2.0)

    records, audit_one = _recording_audit_one()
    audit_runner._batch_drain_cycle(audit_one, max_items=5, timeout=60)
    assert len(records) == 2

    line = next(
        l for l in capsys.readouterr().err.splitlines()
        if "Audit batch drain:" in l
    )
    assert "batch_start=20" in line  # ISO timestamp year prefix
    assert "batch_end=20" in line
    assert "items_processed=2" in line
    assert "queue_remaining=0" in line
    assert "items_included=['SA-1', 'SA-2']" in line


def test_batch_drain_metrics_line_emitted_even_when_empty(capsys):
    """A zero-item window still emits the metrics line (observability)."""
    records, audit_one = _recording_audit_one()
    audit_runner._batch_drain_cycle(audit_one, max_items=5, timeout=60)
    assert records == []
    line = next(
        l for l in capsys.readouterr().err.splitlines()
        if "Audit batch drain:" in l
    )
    assert "items_processed=0" in line
    assert "items_included=[]" in line


# ---------------------------------------------------------------------------
# Defensive guards: no admission-ticket theft, no duplicates, skip primary
# ---------------------------------------------------------------------------


def test_batch_drain_skips_admission_tickets_and_primary():
    """The drain never audits `audit:` admission tickets or the caller's
    own primary item."""
    _enqueue("audit:SA-GHOST:999:1", Priority.CRITICAL, timestamp=0.0)
    _enqueue("SA-PRIMARY", Priority.HIGH, timestamp=0.5)
    _enqueue("SA-REAL", Priority.MEDIUM, timestamp=1.0)
    # An idempotent re-enqueue of SA-REAL (same id) must not double-audit.
    _enqueue("SA-REAL", Priority.MEDIUM, timestamp=1.5)

    records, audit_one = _recording_audit_one()
    audit_runner._batch_drain_cycle(
        audit_one, max_items=10, timeout=60,
        skip_ids=frozenset({"SA-PRIMARY"}),
    )

    assert records == [("SA-REAL", Priority.MEDIUM)]


# ---------------------------------------------------------------------------
# Queue priming from the in_review backlog
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    returncode = 0
    stderr = ""

    def __init__(self, payload):
        self.stdout = json.dumps(payload)


def _in_review_item(item_id, priority, created_at):
    return {
        "id": item_id,
        "priority": priority,
        "createdAt": created_at,
        "stage": "in_review",
        "status": "completed",
        "auditResult": None,
    }


def test_enqueue_pending_audit_items_priority_and_age_order():
    """Priming enqueues oldest-first, critical-before-high-before-medium, and
    caps the batch at max_items."""
    items = [
        _in_review_item("SA-LOW", "low", "2026-08-20T00:00:00Z"),
        _in_review_item("SA-HIGH1", "high", "2026-08-18T00:00:00Z"),
        _in_review_item("SA-CRIT", "critical", "2026-08-19T00:00:00Z"),
        _in_review_item("SA-HIGH2", "high", "2026-08-17T00:00:00Z"),
        _in_review_item("SA-MED", "medium", "2026-08-21T00:00:00Z"),
    ]
    runner = mock.Mock()
    runner.return_value = _FakeCompletedProcess(
        {"success": True, "count": len(items), "workItems": items}
    )

    enqueued = audit_runner._enqueue_pending_audit_items(
        max_items=3, runner=runner
    )

    # Critical first; within high, oldest (SA-HIGH2 from Aug 17) first.
    assert enqueued == ["SA-CRIT", "SA-HIGH2", "SA-HIGH1"]
    queue = _batch_queue()
    assert len(queue) == 3
    # Dequeue all entries: order must match the enqueue (priority + FIFO).
    drained = []
    while True:
        entry = queue.dequeue(timeout=0)
        if entry is None:
            break
        drained.append(entry.item_id)
    assert drained == ["SA-CRIT", "SA-HIGH2", "SA-HIGH1"]


def test_enqueue_pending_audit_items_is_idempotent():
    """Re-priming must not duplicate already-queued work items."""
    items = [
        _in_review_item("SA-A", "high", "2026-08-18T00:00:00Z"),
        _in_review_item("SA-B", "medium", "2026-08-19T00:00:00Z"),
    ]
    runner = mock.Mock()
    runner.return_value = _FakeCompletedProcess(
        {"success": True, "count": len(items), "workItems": items}
    )

    audit_runner._enqueue_pending_audit_items(max_items=5, runner=runner)
    audit_runner._enqueue_pending_audit_items(max_items=5, runner=runner)

    queue = _batch_queue()
    assert len(queue) == 2


def test_enqueue_pending_audit_items_failure_is_best_effort():
    """A failing worklog query primes nothing and never raises."""
    runner = mock.Mock()
    runner.return_value = _FakeCompletedProcess({"success": False, "error": "boom"})
    enqueued = audit_runner._enqueue_pending_audit_items(max_items=5, runner=runner)
    assert enqueued == []
    assert len(_batch_queue()) == 0


# ---------------------------------------------------------------------------
# Trigger wiring: _maybe_run_batch_drain gates on queue depth, never recurses
# ---------------------------------------------------------------------------


def _fake_context(**overrides):
    ctx = mock.Mock(spec=audit_runner._AuditContext)
    ctx.issue_id = "SA-PRIMARY"
    defaults = {
        "persist": True,
        "timeout": None,
        "parent_timeout": None,
        "pi_bin": "pi",
        "model": None,
        "phase1_model": None,
        "model_source": "local",
        "runner": None,
        "json_mode": False,
        "debug_log": None,
        "force": False,
        "worklog_dir": None,
        "batch_phase2": False,
        "green_run": None,
        "audit_children": False,
        "max_child_audits": None,
        "run_tests": False,
        "no_execute": False,
        "max_citations_per_ac": None,
        "child_in_main_slot": False,
    }
    for k, v in defaults.items():
        setattr(ctx, k, overrides.get(k, v))
    return ctx


def test_maybe_run_batch_drain_runs_when_backlogged(monkeypatch, capsys):
    """Queue depth >= 2 triggers a drain over the batch queue with the
    primary item skipped."""
    _enqueue("SA-Q1", Priority.HIGH, timestamp=1.0)
    _enqueue("SA-Q2", Priority.MEDIUM, timestamp=2.0)

    records = []

    def _audit_one(item_id, priority):
        records.append(item_id)
        return 0

    monkeypatch.setattr(
        audit_runner, "_batch_audit_one", lambda ctx: _audit_one
    )
    audit_runner._maybe_run_batch_drain(_fake_context())

    assert records == ["SA-Q1", "SA-Q2"]
    line = next(
        l for l in capsys.readouterr().err.splitlines()
        if "Audit batch drain:" in l
    )
    assert "items_processed=2" in line


def test_maybe_run_batch_drain_noop_when_queue_shallow():
    """Depth < 2: no drain runs (backward-compatible single item)."""
    _enqueue("SA-ONLY", Priority.MEDIUM, timestamp=1.0)
    with mock.patch.object(audit_runner, "_batch_drain_cycle",
                           wraps=audit_runner._batch_drain_cycle) as cycle:
        audit_runner._maybe_run_batch_drain(_fake_context())
        assert cycle.call_count == 0


def test_batch_drain_enabled_optout_via_env(monkeypatch):
    """AUDIT_BATCH_DRAIN=0 disables the automatic drain (opt-out)."""
    assert audit_runner._batch_drain_enabled() is True
    monkeypatch.setenv(audit_runner.AUDIT_BATCH_DISABLE_ENV, "0")
    assert audit_runner._batch_drain_enabled() is False


def test_batch_audit_one_never_recurses(monkeypatch):
    """The drained-item callback invokes cmd_issue with batch_drain=False
    so the batch drain cannot recurse into itself."""
    sentinel_runner = lambda cmd: None  # opaque passthrough
    ctx = _fake_context(runner=sentinel_runner)
    calls = []

    def _fake_cmd_issue(item_id, **kwargs):
        calls.append((item_id, kwargs))
        return 0

    monkeypatch.setattr(audit_runner, "cmd_issue", _fake_cmd_issue)
    callback = audit_runner._batch_audit_one(ctx)
    rc = callback("SA-DRAINED", Priority.HIGH)

    assert rc == 0
    assert len(calls) == 1
    item_id, kwargs = calls[0]
    assert item_id == "SA-DRAINED"
    assert kwargs["batch_drain"] is False
    assert kwargs["no_checkpoint"] is True
    assert kwargs["runner"] is sentinel_runner  # inherits origin runner
    assert kwargs["persist"] is True  # inherits origin persistence