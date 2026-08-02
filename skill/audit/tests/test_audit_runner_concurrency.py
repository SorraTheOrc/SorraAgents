#!/usr/bin/env python3
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
import os
import subprocess
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

from skill.audit.scripts import audit_runner  # noqa: E402
from skill.shared.process_semaphore import ENV_LOCK_DIR, ENV_MAX_WORKERS  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_lock_dir(tmp_path, monkeypatch):
    """Isolate the semaphore lock dir and clear ceiling env per test."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.delenv(ENV_MAX_WORKERS, raising=False)
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)


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
    from skill.shared.process_semaphore import Semaphore

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
    from skill.shared.process_semaphore import Semaphore

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
    from skill.shared.process_semaphore import DEFAULT_MAX_WORKERS

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
    from skill.shared.process_semaphore import DEFAULT_MAX_WORKERS

    monkeypatch.setenv(ENV_MAX_WORKERS, "not-a-number")
    assert audit_runner._audit_semaphore_max_workers() == DEFAULT_MAX_WORKERS
    assert "invalid" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# AC2/AC4: bounded wait -> clear verdict on timeout (no crash)
# ---------------------------------------------------------------------------


def test_call_pi_timeout_returns_unmet_verdict(monkeypatch):
    """When the ceiling stays saturated past the bounded wait, _call_pi must
    return an 'unmet' verdict with a clear message (not raise)."""
    from skill.shared.process_semaphore import Semaphore

    # Saturate all slots: hold 1 slot in this process, then attempt another.
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    monkeypatch.setenv("AUDIT_LOCK_TIMEOUT", "0")  # fail fast

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
    assert getattr(args, "max_concurrency") == 3
    args = parser.parse_args(["project", "--max-concurrency", "3"])
    assert getattr(args, "max_concurrency") == 3


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
    ):
        with pytest.raises(RuntimeError):
            audit_runner._call_pi("prompt", model="m", pi_bin="missing-pi")
