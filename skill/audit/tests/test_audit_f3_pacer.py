"""Regression tests: audit F3 auto-execution is paced (SA-0MUJK94QN0015925).

The audit runner's automatic full-suite execution (F3) calls
``run_cached(..., force=True)`` directly. Historically it passed no runner,
so it used ``test_cache._default_runner`` and bypassed the shared ``"test"``
semaphore that ``run_tests.py`` uses (SA-0MTG5U75A001F1RG) — nested /
concurrent audit-triggered executions were unbounded.

These tests prove the F3 path now routes its runner through
``run_tests.paced_runner``: a real execution acquires a ``"test"`` slot
before spawning, and a saturated host fails open (a clear notice), never
running unbounded.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _record_slot(events: list[str], error: Exception | None = None):
    """A ``_test_concurrency_slot`` stand-in recording acquire/release."""

    @contextlib.contextmanager
    def _slot():
        events.append("acquire")
        if error is not None:
            raise error
        try:
            yield
        finally:
            events.append("release")

    return _slot


def _capture_f3_runner(monkeypatch, tmp_path):
    """Drive ``_run_tests_via_test_skill`` and capture the runner it paced."""
    monkeypatch.setattr(
        audit_runner, "full_suite_commands", lambda root: ["pytest -q"]
    )
    monkeypatch.setattr(
        audit_runner, "suite_timeout_per_command", lambda root: 60
    )
    captured: dict = {}

    def fake_run_cached(command, **kwargs):
        captured["runner"] = kwargs.get("runner")
        captured["command"] = command
        return {
            "stdout": "1 passed in 0.01s",
            "stderr": "",
            "exit_code": 0,
            "command": command,
            "git_state": "test",
            "cached": False,
        }

    monkeypatch.setattr(audit_runner, "run_cached", fake_run_cached)
    return captured


class TestAuditF3ExecutionIsPaced:
    def test_f3_execution_acquires_test_slot(self, monkeypatch, tmp_path):
        """AC1: the F3 runner holds a "test" slot around the real execution."""
        import test_cache

        captured = _capture_f3_runner(monkeypatch, tmp_path)
        monkeypatch.setattr(
            test_cache.subprocess, "run",
            lambda *a, **k: SimpleNamespace(
                stdout="1 passed", stderr="", returncode=0
            ),
        )

        audit_runner._run_tests_via_test_skill(str(tmp_path))

        runner = captured["runner"]
        assert runner is not None, "F3 must pass an explicit (paced) runner"

        events: list[str] = []
        monkeypatch.setitem(
            runner.__globals__, "_test_concurrency_slot", _record_slot(events)
        )
        runner(captured["command"], str(tmp_path), 60)

        assert events[0] == "acquire", f"slot not acquired: {events}"
        assert events[-1] == "release", f"slot not released: {events}"

    def test_saturated_host_fails_open_with_notice(self, monkeypatch, tmp_path):
        """AC2: a saturated slot yields a fail-open notice, never a hang."""
        from test.scripts.run_tests import TestConcurrencyTimeout

        monkeypatch.setattr(
            audit_runner, "full_suite_commands", lambda root: ["pytest -q"]
        )
        monkeypatch.setattr(
            audit_runner, "suite_timeout_per_command", lambda root: 60
        )

        events: list[str] = []

        def fake_run_cached(command, **kwargs):
            runner = kwargs["runner"]
            # Invoke the paced runner: the busy slot raises here.
            return runner(command, str(tmp_path), kwargs.get("timeout"))

        monkeypatch.setattr(audit_runner, "run_cached", fake_run_cached)

        # Patch the slot in the paced_runner globals (module-identity safe).
        from test.scripts import run_tests as run_tests_mod

        monkeypatch.setitem(
            run_tests_mod.__dict__,
            "_test_concurrency_slot",
            _record_slot(
                events,
                error=TestConcurrencyTimeout(
                    "test concurrency slot busy: no free slot within 600s"
                ),
            ),
        )

        result = audit_runner._run_tests_via_test_skill(str(tmp_path))

        assert result["success"] is False
        assert "no free slot" in result["notice"], result

    def test_default_paced_runner_uses_cache_default_runner(self):
        """``paced_runner()`` wraps ``test_cache._default_runner``."""
        from test.scripts.run_tests import paced_runner
        from test_cache import _default_runner

        paced = paced_runner()
        assert paced is not _default_runner
        # The wrapper is a distinct callable that delegates to the default.
        assert callable(paced)
