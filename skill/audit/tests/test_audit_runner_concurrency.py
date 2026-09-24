"""Tests for audit_runner concurrency cap (SA-0MSAK2SNN005HCM5).

Validates that audit_runner.py bounds concurrent pi subprocess launches
via the shared flock-based semaphore (SA-0MSAK2P3J0065POO):

- AC1: _call_pi acquires a semaphore slot before launching pi subprocesses.
- AC2: When at max concurrency, new pi calls wait (bounded) instead of
  launching unbounded pi processes.
- AC3: Ceiling configurable via AUDIT_MAX_CONCURRENCY env var and a
  --max-concurrency CLI flag; default documented.
- AC4: Single-audit behaviour unchanged; timeout when the ceiling is hit
  yields a clear "unmet" verdict (not a crash).
- AC5: Existing audit tests pass (covered by full suite run).
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


@pytest.fixture(autouse=True)
def _isolate_lock_dir(tmp_path, monkeypatch):
    """Isolate the semaphore lock dir and clear ceiling env per test."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.delenv(ENV_MAX_WORKERS, raising=False)
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("AUDIT_QUEUE_TIMEOUT", raising=False)


class _MockProcess:
    """Fake Popen return value: communicates canned JSON then exits."""

    def __init__(self, stdout_text=None):
        if stdout_text is None:
            # pi --mode json stream: message_update with text_end whose
            # content is the model's JSON payload (verdict/evidence).
            inner = json.dumps({"verdict": "met", "evidence": "ok"})
            stdout_text = json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_end", "content": inner},
                }
            )
        self._text = stdout_text
        self.returncode = 0

    def communicate(self, timeout=None):
        return self._text, ""

    def kill(self):
        pass


def _mock_popen(stdout_text=None):
    """Return a mock.patch context for subprocess.Popen."""
    return mock.patch.object(
        audit_runner.subprocess,
        "Popen",
        return_value=_MockProcess(stdout_text),
    )


# ---------------------------------------------------------------------------
# AC1: _call_pi acquires a slot before launching pi subprocesses
# ---------------------------------------------------------------------------


def test_call_pi_uses_semaphore_context(monkeypatch):
    """_call_pi must acquire and release the audit semaphore around Popen."""
    from shared.process_semaphore import Semaphore

    events = []
    real_acquire = Semaphore.acquire
    real_release = Semaphore.release

    def spy_acquire(self, *a, **k):
        events.append("acquire")
        return real_acquire(self, *a, **k)

    def spy_release(self, *a, **k):
        events.append("release")
        return real_release(self, *a, **k)

    monkeypatch.setattr(Semaphore, "acquire", spy_acquire)
    monkeypatch.setattr(Semaphore, "release", spy_release)

    with _mock_popen():
        result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")

    assert result.get("verdict") == "met"
    assert events.count("acquire") >= 1
    assert events.count("release") >= 1
    # acquire happens before the first release (bounded window around Popen)
    assert events.index("acquire") < events.index("release")


def test_call_pi_slot_released_after_call():
    """After _call_pi returns, no slot remains held (no leak)."""
    from shared.process_semaphore import Semaphore

    with _mock_popen():
        audit_runner._call_pi("prompt", model="m", pi_bin="pi")

    sem = Semaphore("audit", max_workers=1, timeout=1)
    # A fresh acquire must succeed immediately -> previous call released.
    with sem:
        pass


# ---------------------------------------------------------------------------
# AC2: concurrent pi calls are bounded to the configured maximum
# ---------------------------------------------------------------------------


def test_concurrent_pi_calls_bounded(monkeypatch):
    """With AUDIT_MAX_CONCURRENCY=2, 6 concurrent _call_pi calls never have
    more than 2 active subprocess launches at once."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "2")

    lock = threading.Lock()
    active = 0
    peak = 0

    class _SlowPopen(_MockProcess):
        def communicate(self, timeout=None):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.15)
            with lock:
                active -= 1
            return self._text, ""

    monkeypatch.setattr(audit_runner.subprocess, "Popen", lambda *a, **k: _SlowPopen())

    threads = [
        threading.Thread(
            target=audit_runner._call_pi,
            args=("p",),
            kwargs={"model": "m", "pi_bin": "pi"},
        )
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert peak <= 2, f"pi concurrency exceeded ceiling: peak={peak}"


def test_concurrency_ceiling_respected_with_serialization(monkeypatch):
    """With AUDIT_MAX_CONCURRENCY=1, concurrent calls fully serialize."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")

    lock = threading.Lock()
    active = 0
    peak = 0

    class _SlowPopen(_MockProcess):
        def communicate(self, timeout=None):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.1)
            with lock:
                active -= 1
            return self._text, ""

    monkeypatch.setattr(audit_runner.subprocess, "Popen", lambda *a, **k: _SlowPopen())

    threads = [
        threading.Thread(
            target=audit_runner._call_pi,
            args=("p",),
            kwargs={"model": "m", "pi_bin": "pi"},
        )
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert peak == 1, f"expected full serialization, peak={peak}"


# ---------------------------------------------------------------------------
# AC3: configurable ceiling via env var
# ---------------------------------------------------------------------------


def test_default_ceiling_documented(monkeypatch):
    """Without env override, the audit semaphore uses the documented default."""
    from shared.process_semaphore import DEFAULT_MAX_WORKERS

    monkeypatch.delenv(ENV_MAX_WORKERS, raising=False)
    with _mock_popen():
        audit_runner._call_pi("prompt", model="m", pi_bin="pi")
    # The audit ceiling helper must report the default.
    assert audit_runner._audit_semaphore_max_workers() == DEFAULT_MAX_WORKERS


def test_env_ceiling_applied(monkeypatch):
    """AUDIT_MAX_CONCURRENCY=4 must be picked up by the ceiling helper."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "4")
    assert audit_runner._audit_semaphore_max_workers() == 4


def test_invalid_env_ceiling_falls_back(monkeypatch, capsys):
    """A non-integer AUDIT_MAX_CONCURRENCY warns and uses the default."""
    from shared.process_semaphore import DEFAULT_MAX_WORKERS

    monkeypatch.setenv(ENV_MAX_WORKERS, "not-a-number")
    assert audit_runner._audit_semaphore_max_workers() == DEFAULT_MAX_WORKERS
    assert "invalid" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# SA-0MSGEAZMC009LHKL: fail fast when the concurrency ceiling is saturated
# ---------------------------------------------------------------------------


def test_default_lock_timeout_is_fail_fast(monkeypatch):
    """The default bounded wait for a concurrency slot is 0s (fail fast).

    Regression for SA-0MSGEAZMC009LHKL: the default was 300s, which
    exceeded the parent bash-tool execution timeout (~120s) and killed
    audits mid-wait. The per-attempt semaphore try must now be 0.0 so a
    saturated ceiling NEVER blocks the poll loop; the TOTAL bounded wait
    for admission is governed separately by AUDIT_QUEUE_TIMEOUT
    (SA-0MTG5RYH8005RQNM).
    """
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    assert audit_runner.AUDIT_LOCK_TIMEOUT_DEFAULT == 0.0
    assert audit_runner._audit_lock_timeout() == 0.0


def test_default_queue_timeout_is_bounded_not_fail_fast(monkeypatch):
    """The default queue wait is a bounded value (not 0s fail fast).

    SA-0MTG5RYH8005RQNM AC1: audits under saturation WAIT in the priority
    queue rather than failing immediately. The default must be inside the
    parent bash-tool budget (~120s, SA-0MSGEAZMC009LHKL) so an exhausted
    wait still returns the graceful unmet verdict instead of being killed
    mid-wait.
    """
    monkeypatch.delenv("AUDIT_QUEUE_TIMEOUT", raising=False)
    assert 0 < audit_runner.AUDIT_QUEUE_TIMEOUT_DEFAULT <= 120.0
    assert audit_runner._audit_queue_timeout() == audit_runner.AUDIT_QUEUE_TIMEOUT_DEFAULT


def test_queue_timeout_env_override_still_wins(monkeypatch):
    """AUDIT_QUEUE_TIMEOUT env var overrides the bounded default."""
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "30")
    assert audit_runner._audit_queue_timeout() == 30.0


def test_invalid_queue_timeout_env_falls_back(monkeypatch, capsys):
    """A non-numeric AUDIT_QUEUE_TIMEOUT warns and uses the default."""
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "not-a-number")
    assert audit_runner._audit_queue_timeout() == audit_runner.AUDIT_QUEUE_TIMEOUT_DEFAULT
    assert "invalid" in capsys.readouterr().err.lower()


def test_resolve_audit_priority_maps_worklog_levels():
    """Worklog priority strings map to numeric queue priorities."""
    from shared.queue import Priority

    assert audit_runner._resolve_audit_priority({"priority": "critical"}) == Priority.CRITICAL
    assert audit_runner._resolve_audit_priority({"priority": "high"}) == Priority.HIGH
    assert audit_runner._resolve_audit_priority({"priority": "medium"}) == Priority.MEDIUM
    assert audit_runner._resolve_audit_priority({"priority": "low"}) == Priority.LOW
    # Unknown/missing priority defaults to medium (never crashes an audit).
    assert audit_runner._resolve_audit_priority({}) == Priority.MEDIUM
    assert audit_runner._resolve_audit_priority(None) == Priority.MEDIUM
    assert audit_runner._resolve_audit_priority({"priority": "urgent"}) == Priority.MEDIUM


def test_lock_timeout_env_override_still_wins(monkeypatch):
    """AUDIT_LOCK_TIMEOUT env var overrides the fail-fast default (AC2)."""
    monkeypatch.setenv("AUDIT_LOCK_TIMEOUT", "30")
    assert audit_runner._audit_lock_timeout() == 30.0


def test_call_pi_saturated_ceiling_waits_then_times_out_bounded(monkeypatch):
    """A saturated ceiling now WAITS on the priority queue (not fail fast).

    SA-0MTG5RYH8005RQNM AC1: with both slots held, a new pi call must not
    return immediately with an unmet verdict; it enqueues and waits for
    the AUDIT_QUEUE_TIMEOUT bound, then reports the honest unmet
    concurrency verdict. Regression for the SA-0MSGEAZMC009LHKL hang: the
    wait must stay inside the parent bash-tool timeout.
    """
    from shared.process_semaphore import Semaphore

    # Saturate the single slot, then attempt another launch.
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "0.3")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)

    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()
    try:
        start = time.monotonic()
        with _mock_popen():
            result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
        elapsed = time.monotonic() - start
    finally:
        sem.release()

    assert result.get("verdict") == "unmet"
    assert "concurr" in result.get("evidence", "").lower()
    assert result.get("_concurrency_timeout") is True
    # It WAITED (not fail-fast) but stayed inside a small multiple of the
    # queue bound, far below the old 300s hang.
    assert elapsed >= 0.1, f"_call_pi failed fast instead of queue-waiting ({elapsed:.1f}s)"
    assert elapsed < 5.0, f"_call_pi blocked {elapsed:.1f}s on saturated ceiling"


def test_call_pi_saturated_ceiling_recovers_without_fail_fast(monkeypatch):
    """When a slot frees inside the queue bound, the queued call proceeds
    (AC1: no fail-fast unmet under momentary saturation)."""
    from shared.process_semaphore import Semaphore

    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "30")

    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()

    def _release_after_delay():
        time.sleep(0.3)
        sem.release()

    releaser = threading.Thread(target=_release_after_delay)
    releaser.start()
    try:
        with _mock_popen():
            result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
    finally:
        releaser.join(timeout=10)

    assert result.get("verdict") == "met", result
    assert result.get("_concurrency_timeout") is not True


# ---------------------------------------------------------------------------
# AC2/AC4: bounded wait -> clear verdict on timeout (no crash)
# ---------------------------------------------------------------------------


def test_call_pi_timeout_returns_unmet_verdict(monkeypatch):
    """When the ceiling stays saturated past the bounded wait, _call_pi must
    return an 'unmet' verdict with a clear message (not raise)."""
    from shared.process_semaphore import Semaphore

    # Saturate all slots: hold 1 slot in this process, then attempt another.
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_LOCK_TIMEOUT", "0")  # fail-fast per-attempt try
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "0.2")

    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()
    try:
        with _mock_popen():
            result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
    finally:
        sem.release()

    assert result.get("verdict") == "unmet"
    assert "concurr" in result.get("evidence", "").lower() or "busy" in result.get(
        "evidence", ""
    ).lower()


# ---------------------------------------------------------------------------
# CLI flag (AC3)
# ---------------------------------------------------------------------------


def test_cli_has_max_concurrency_flags():
    """Both `issue` and `project` subparsers expose --max-concurrency."""
    parser = audit_runner.build_parser()
    args = parser.parse_args(["issue", "SA-TEST-001", "--max-concurrency", "3"])
    assert args.max_concurrency == 3
    args = parser.parse_args(["project", "--max-concurrency", "3"])
    assert args.max_concurrency == 3


def test_cli_max_concurrency_applied_to_env(monkeypatch):
    """A --max-concurrency value must win over the env var."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    assert audit_runner._audit_semaphore_max_workers(cli_value=3) == 3


# ---------------------------------------------------------------------------
# AC4: single-audit behaviour unchanged
# ---------------------------------------------------------------------------


def test_single_call_pi_unchanged():
    """A single _call_pi returns the same verdict shape as before."""
    with _mock_popen():
        result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
    assert result["verdict"] == "met"
    assert result["evidence"] == "ok"


def test_pi_missing_binary_still_raises():
    """FileNotFoundError for a missing pi binary is still surfaced."""
    from unittest import mock as _mock

    with _mock.patch.object(
        audit_runner.subprocess, "Popen", side_effect=FileNotFoundError("pi")
    ), pytest.raises(RuntimeError):
        audit_runner._call_pi("prompt", model="m", pi_bin="missing-pi")


# ---------------------------------------------------------------------------
# SA-0MU32T8SH001R8MZ: Prevent concurrency saturation from aborting Phase 1
# ---------------------------------------------------------------------------


class _MockProcessWithCapture(_MockProcess):
    """Captures the cmd list so callers can verify priority was passed."""

    captured_cmd: list | None = None

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

    @classmethod
    def reset(cls):
        cls.captured_cmd = None

    @classmethod
    def factory(cls):
        def _factory(*cmd, **kw):
            cls.captured_cmd = list(cmd)
            return cls()
        return _factory


def _mock_popen_with_capture():
    """Return a mock.patch context for Popen that captures the argv."""
    return mock.patch.object(
        audit_runner.subprocess,
        "Popen",
        side_effect=_MockProcessWithCapture.factory(),
    )


# ---------------------------------------------------------------------------
# AC1: Strict priority for Phase 1 parent screening
# ---------------------------------------------------------------------------


def test_phase1_parent_forces_critical_priority(monkeypatch):
    """When context='parent', _call_phase1_screen must force CRITICAL
    priority regardless of the work item's own priority (SA-0MU32T8SH001R8MZ AC1).

    The Phase 1 parent screen is the highest-value call in the pipeline;
    it must never be starved by lower-priority concurrent audits."""
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("AUDIT_QUEUE_TIMEOUT", raising=False)

    captured_priority = []
    original_acquire = audit_runner._acquire_audit_slot

    def spy_acquire(issue_id="", priority=audit_runner.Priority.MEDIUM, **kw):
        captured_priority.append(priority)
        return original_acquire(issue_id=issue_id, priority=priority, **kw)

    monkeypatch.setattr(audit_runner, "_acquire_audit_slot", spy_acquire)

    with _mock_popen_with_capture():
        audit_runner._call_phase1_screen(
            issue_id="SA-TEST-001",
            context="parent",
            prompt="test prompt",
            model="test-model",
            pi_bin="pi",
            debug_log=None,
            timeout=None,
            ac_fallback_used=None,
            on_runtime_error=lambda *a: None,
            failure_label="test",
            priority=audit_runner.Priority.MEDIUM,
        )

    assert len(captured_priority) >= 1
    assert all(
        p == audit_runner.Priority.CRITICAL for p in captured_priority
    ), f"Expected CRITICAL priority, got {captured_priority}"


def test_phase1_parent_low_work_item_gets_critical_priority(monkeypatch):
    """Even a LOW-priority work item's Phase 1 parent screen gets CRITICAL
    priority (SA-0MU32T8SH001R8MZ AC1)."""
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("AUDIT_QUEUE_TIMEOUT", raising=False)

    captured_priority = []
    original_acquire = audit_runner._acquire_audit_slot

    def spy_acquire(issue_id="", priority=audit_runner.Priority.MEDIUM, **kw):
        captured_priority.append(priority)
        return original_acquire(issue_id=issue_id, priority=priority, **kw)

    monkeypatch.setattr(audit_runner, "_acquire_audit_slot", spy_acquire)

    with _mock_popen_with_capture():
        audit_runner._call_phase1_screen(
            issue_id="SA-TEST-002",
            context="parent",
            prompt="test prompt",
            model="test-model",
            pi_bin="pi",
            debug_log=None,
            timeout=None,
            ac_fallback_used=None,
            on_runtime_error=lambda *a: None,
            failure_label="test",
            priority=audit_runner.Priority.LOW,
        )

    assert len(captured_priority) >= 1
    assert all(
        p == audit_runner.Priority.CRITICAL for p in captured_priority
    )


def test_phase1_child_does_not_get_critical_priority(monkeypatch):
    """Child screens (context != 'parent') must NOT be forced to CRITICAL.
    They pass through the caller's priority unchanged (SA-0MU32T8SH001R8MZ AC1).

    This ensures the override is scoped only to the parent context and
    existing callers are unaffected."""
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("AUDIT_QUEUE_TIMEOUT", raising=False)

    captured_priority = []
    original_acquire = audit_runner._acquire_audit_slot

    def spy_acquire(issue_id="", priority=audit_runner.Priority.MEDIUM, **kw):
        captured_priority.append(priority)
        return original_acquire(issue_id=issue_id, priority=priority, **kw)

    monkeypatch.setattr(audit_runner, "_acquire_audit_slot", spy_acquire)

    with _mock_popen_with_capture():
        audit_runner._call_phase1_screen(
            issue_id="SA-TEST-003",
            context="child:SA-TEST-004",
            prompt="test prompt",
            model="test-model",
            pi_bin="pi",
            debug_log=None,
            timeout=None,
            ac_fallback_used=None,
            on_runtime_error=lambda *a: None,
            failure_label="test",
            priority=audit_runner.Priority.MEDIUM,
        )

    assert len(captured_priority) >= 1
    # Child screens keep their original priority
    assert all(
        p == audit_runner.Priority.MEDIUM for p in captured_priority
    )


# ---------------------------------------------------------------------------
# AC2: Actionable failure mode
# ---------------------------------------------------------------------------


def test_timeout_error_includes_queue_depth_and_wait_time(monkeypatch):
    """When _acquire_audit_slot times out, the TimeoutError must include
    queue depth, elapsed wait time, and an explicit retry action
    (SA-0MU32T8SH001R8MZ AC2)."""
    from shared.process_semaphore import Semaphore

    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "0.2")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)

    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()
    try:
        with _mock_popen():
            result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
    finally:
        sem.release()

    assert result.get("verdict") == "unmet"
    evidence = result.get("evidence", "")
    # Must include queue depth
    assert "item(s) in queue" in evidence or "items in queue" in evidence or "queue" in evidence.lower()
    # Must include elapsed wait time
    assert "waited" in evidence.lower() or "s)" in evidence
    # Must include retry action
    assert "retry" in evidence.lower()


def test_timeout_error_contains_retry_seconds(monkeypatch):
    """The actionable error message must include an explicit retry time
    (e.g. 'retry in X seconds')."""
    from shared.process_semaphore import Semaphore

    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "0.2")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)

    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()
    try:
        with _mock_popen():
            result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
    finally:
        sem.release()

    evidence = result.get("evidence", "")
    assert "retry" in evidence.lower()
    # Should include a number of seconds
    import re
    assert re.search(r"\d+s", evidence), f"Expected 'retry in Ns' in: {evidence}"


def test_timeout_error_elapsed_time_is_positive(monkeypatch):
    """The elapsed wait time in the TimeoutError must be a positive,
    human-readable number — not a garbage value from mixing
    time.monotonic() with time.time() (SA-0MU32T8SH001R8MZ AC2).

    Prior to the fix, the code computed
    ``time.monotonic() - queued_wall`` where ``queued_wall`` was a
    ``time.time()`` value, producing large negative numbers
    (e.g. -1788131492.7s).
    """
    from shared.process_semaphore import Semaphore

    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "0.2")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)

    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()
    try:
        with _mock_popen():
            result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")
    finally:
        sem.release()

    evidence = result.get("evidence", "")

    # Extract the elapsed wait time from the message: "waited X.Xs"
    import re
    match = re.search(r"waited ([\d.]+)s", evidence)
    assert match is not None, (
        f"Expected 'waited Ns' in TimeoutError: {evidence}"
    )
    elapsed = float(match.group(1))
    # The elapsed time must be a positive, reasonable value
    # (the queue timeout is 0.2s, so elapsed should be close to that).
    assert elapsed >= 0, (
        f"Elapsed wait time must be non-negative, got {elapsed:.1f}s. "
        "This indicates clock-mixing (time.monotonic() vs time.time()). "
        "Evidence: {evidence}"
    )
    assert elapsed < 10, (
        f"Elapsed wait time {elapsed:.1f}s exceeds expected bound. "
        f"Evidence: {evidence}"
    )


# ---------------------------------------------------------------------------
# AC3: Concurrency test — parent screen completes under saturation
# ---------------------------------------------------------------------------


def test_concurrent_parent_screens_complete_under_saturation(monkeypatch):
    """With >= 2 simultaneous audits each with a Phase 1 parent screen,
    the first audit's Phase 1 parent screen must complete (it may wait in
    the queue) rather than returning 'unmet' with zero Pi calls
    (SA-0MU32T8SH001R8MZ AC3).

    The Phase 1 parent screen uses CRITICAL priority (AC1) so it is
    dequeued before any lower-priority concurrent calls.
    """
    from shared.process_semaphore import Semaphore

    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "30")

    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()
    completed = []

    def _release_after_delay():
        time.sleep(0.3)
        sem.release()

    releaser = threading.Thread(target=_release_after_delay)
    releaser.start()

    try:
        # First call: already holding the slot, this will fail to acquire.
        # But since it's a parent context with CRITICAL priority, it should
        # still wait and eventually complete when the slot frees.
        with _mock_popen():
            result = audit_runner._call_pi(
                "prompt", model="m", pi_bin="pi",
                issue_id="SA-AC3",
                priority=audit_runner.Priority.CRITICAL,
            )
        completed.append(result.get("verdict"))
    finally:
        releaser.join(timeout=10)

    # The first caller's CRITICAL ticket should have been admitted once
    # the slot freed (not failed-fast).
    assert any(v == "met" for v in completed), (
        f"Phase 1 parent screen did not complete under saturation: {completed}"
    )


def test_parent_screen_priority_beats_child_under_saturation(monkeypatch, capsys):
    """A Phase 1 parent screen (CRITICAL) must be admitted before a
    child screen (MEDIUM) when both contend for a freed slot
    (SA-0MU32T8SH001R8MZ AC1 + AC3)."""
    from shared.process_semaphore import Semaphore

    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "30")

    monkeypatch.setattr(
        audit_runner.subprocess,
        "Popen",
        lambda *a, **k: _MockProcessWithCapture(),
    )

    # Hold the only slot
    sem = Semaphore("audit", max_workers=1, timeout=10)
    sem.acquire()

    results = {}

    def _parent_call():
        results["parent"] = audit_runner._call_pi(
            "prompt", model="m", pi_bin="pi",
            priority=audit_runner.Priority.CRITICAL,
        )

    def _child_call():
        results["child"] = audit_runner._call_pi(
            "prompt", model="m", pi_bin="pi",
            priority=audit_runner.Priority.MEDIUM,
        )

    # Start parent first (enqueues CRITICAL), then child enqueues MEDIUM.
    # When the slot frees, parent must be admitted first.
    t_parent = threading.Thread(target=_parent_call)
    t_parent.start()
    time.sleep(0.1)
    t_child = threading.Thread(target=_child_call)
    t_child.start()

    # Release the held slot
    time.sleep(0.2)
    sem.release()

    t_parent.join(timeout=30)
    t_child.join(timeout=30)

    # Both should eventually complete
    assert all(r.get("verdict") == "met" for r in results.values()), results

    # Admission order: parent (CRITICAL) before child (MEDIUM)
    lines = [
        l
        for l in capsys.readouterr().err.splitlines()
        if "Audit slot acquired" in l
    ]
    assert len(lines) == 2, f"expected 2 admission logs, got {len(lines)}"
    seq = [re.search(r"priority=(\w+)", l).group(1) for l in lines]
    assert seq[0] == "critical", f"parent (CRITICAL) must admit first: {seq}"
