"""Tests: implement.py finish-gate suites are paced by the test skill.

Covers SA-0MUKHCO02009EQFG (route implement.py test execution through the
test skill pacer):

- AC1: every real suite execution triggered by implement.py (changed and
  full scope) acquires the shared "test" semaphore before spawning.
- AC2: cache hits do not consume a semaphore slot (parity with run_tests.py).
- AC3: the pacer is honoured for implement-gate runs with a bounded wait and
  a clear, actionable failure on timeout.
- AC4: no direct unguarded ``run_cached`` execution path remains in
  implement.py.
"""
from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

# Make the repo root importable so implement.py can be loaded by path.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

SCOPED_CMD = "pytest -q -r a --disable-warnings tests/test_foo.py"


def _load_implement() -> object:
    """Import implement.py as a module (mirrors canonical tests)."""
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_pacer", str(_IMPLEMENT_PY),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "implement_scripts"
    sys.modules["implement_under_test_pacer"] = mod
    spec.loader.exec_module(mod)
    return mod


def _canned_run(exit_code: int, stdout: str = "", cached: bool = True) -> dict:
    return {
        "stdout": stdout,
        "stderr": "",
        "exit_code": exit_code,
        "completed_at": 0.0,
        "command": "test",
        "git_state": "test",
        "cached": cached,
    }


def _patch_slot(monkeypatch, mod, slot) -> None:
    """Patch ``_test_concurrency_slot`` in the *real* paced_runner globals.

    implement.py imports ``paced_runner`` from ``test.scripts.run_tests``,
    while this test module imports ``run_tests`` — under two different
    sys.path roots these are distinct module objects. Patching the
    function's own ``__globals__`` is therefore the reliable way to observe
    slot acquisition without depending on module identity.
    """
    globals_ = mod._test_paced_runner.__globals__
    monkeypatch.setitem(globals_, "_test_concurrency_slot", slot)


def _record_slot(events: list[str]):
    """A ``_test_concurrency_slot`` stand-in recording acquire/release."""

    @contextlib.contextmanager
    def _slot():
        events.append("acquire")
        try:
            yield
        finally:
            events.append("release")

    return _slot


@pytest.fixture
def _capture_runner(monkeypatch, tmp_path):
    """Patch implement.py's run_cached to capture the runner it was given.

    The test-skill path delegates suite execution to the canonical
    ``guarded_run_all`` (which owns pacing internally, SA-0MUH1MJL6003767Q),
    so the ``run_cached`` seam only exists on the direct/legacy path. The
    test skill is disabled here to exercise that direct path; the delegation
    boundary is covered by ``test_implement_guard_ownership.py``.
    """
    mod = _load_implement()
    monkeypatch.setattr(mod, "_full_suite_commands", None)
    monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: "pytest")
    captured: dict = {}

    def fake_run_cached(command, **kwargs):
        captured["runner"] = kwargs.get("runner")
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        captured["timeout"] = kwargs.get("timeout")
        return _canned_run(exit_code=0)

    monkeypatch.setattr(mod, "run_cached", fake_run_cached)
    # Never actually spawn a suite command when the captured runner is invoked.
    monkeypatch.setattr(
        mod, "run_cmd",
        lambda *a, **k: SimpleNamespace(stdout="ok", stderr="", returncode=0),
    )
    return mod, captured


class TestImplementExecutionsArePaced:
    """AC1: real executions acquire the shared "test" semaphore."""

    def test_full_scope_execution_acquires_slot(self, monkeypatch, _capture_runner):
        mod, captured = _capture_runner
        events: list[str] = []
        _patch_slot(monkeypatch, mod, _record_slot(events))

        mod.run_tests("/tmp", scope="full")

        runner = captured["runner"]
        assert runner is not None, "run_cached must receive an explicit runner"
        result = runner(captured["command"], captured["cwd"], captured["timeout"])
        assert result.returncode == 0
        assert events[0] == "acquire", f"slot not acquired: {events}"
        assert events[-1] == "release", f"slot not released: {events}"

    def test_changed_scope_execution_acquires_slot(
        self, monkeypatch, _capture_runner
    ):
        mod, captured = _capture_runner
        monkeypatch.setattr(mod, "_changed_scope_commands", lambda *a, **k: [SCOPED_CMD])
        events: list[str] = []
        _patch_slot(monkeypatch, mod, _record_slot(events))

        mod.run_tests("/tmp", scope="changed")

        assert captured["command"] == SCOPED_CMD
        runner = captured["runner"]
        runner(captured["command"], captured["cwd"], captured["timeout"])
        assert "acquire" in events, f"slot not acquired: {events}"


class TestCacheHitsDoNotConsumeSlot:
    """AC2: a cache hit never acquires a slot (runner is not invoked)."""

    def test_pacing_wrapper_is_lazy(self, monkeypatch, _capture_runner):
        """Building the runner must not touch the semaphore; only invoking
        the runner (a cache miss) acquires a slot — ``run_cached`` itself
        returns a cache hit without calling the runner."""
        mod, _captured = _capture_runner
        events: list[str] = []
        _patch_slot(monkeypatch, mod, _record_slot(events))

        mod.run_tests("/tmp", scope="full")
        # run_cached was a (canned) cache hit and never called the runner.
        assert events == [], f"a cache hit consumed a slot: {events}"


class TestConcurrencyTimeoutIsActionable:
    """AC3: a saturated host yields a clear failed result, never a hang."""

    def test_timeout_returns_failed_result(self, monkeypatch, tmp_path, caplog):
        mod = _load_implement()
        monkeypatch.setattr(mod, "_full_suite_commands", None)
        monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: "pytest")

        @contextlib.contextmanager
        def _busy_slot():
            # Use implement.py's own imported class so the ``except`` clause
            # matches exactly as it does in production (both come from
            # ``test.scripts.run_tests``).
            raise mod._TestConcurrencyTimeout(
                "test concurrency slot busy: no free slot within 600s "
                "(TEST_MAX_CONCURRENCY=2)"
            )
            yield  # pragma: no cover

        _patch_slot(monkeypatch, mod, _busy_slot)

        with caplog.at_level("ERROR"):
            result = mod.run_tests(str(tmp_path), scope="full")

        assert result["success"] is False
        assert "no free slot within 600s" in result["stderr"]
        assert any(
            "could not acquire a test-run slot" in rec.message
            for rec in caplog.records
        ), "no actionable log line emitted"


class TestNoUnguardedExecutionPath:
    """AC4: every run_cached call in implement.py is paced.

    A behavioural check: monkeypatch the real ``run_cached`` to record
    whether the runner it receives is a *paced* wrapper (i.e. invoking it
    acquires a slot), across every tooling branch.
    """

    @pytest.mark.parametrize(
        "tooling,scope,override_cmd",
        [
            ("pytest", "full", None),
            ("pytest", "changed", None),
            ("npm", "full", None),
            ("npm", "changed", None),
        ],
    )
    def test_all_tooling_branches_receive_paced_runner(
        self, monkeypatch, tmp_path, tooling, scope, override_cmd
    ):
        mod = _load_implement()
        monkeypatch.setattr(mod, "_full_suite_commands", None)
        monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: tooling)
        if scope == "changed":
            monkeypatch.setattr(
                mod, "_changed_scope_commands", lambda *a, **k: [SCOPED_CMD]
            )
        monkeypatch.setattr(mod, "_has_test_script", lambda cwd: False)
        monkeypatch.setattr(
            mod, "run_cmd",
            lambda *a, **k: SimpleNamespace(stdout="ok", stderr="", returncode=0),
        )

        events: list[str] = []
        _patch_slot(monkeypatch, mod, _record_slot(events))

        captured_runners: list = []

        def fake_run_cached(command, **kwargs):
            captured_runners.append(kwargs.get("runner"))
            return _canned_run(exit_code=0)

        monkeypatch.setattr(mod, "run_cached", fake_run_cached)

        mod.run_tests(str(tmp_path), scope=scope)

        assert captured_runners, "no run_cached call recorded"
        for runner in captured_runners:
            assert runner is not None
            runner("pytest", str(tmp_path), 10)
        assert "acquire" in events, (
            f"{tooling}/{scope} branch passed an unpaced runner: {events}"
        )


class TestImplementGatePacingTelemetry:
    """AC3 (SA-0MUA8BSAG000YZA2): implement-gate runs emit pacing telemetry."""

    def test_full_scope_gate_logs_queued_at_and_wait_seconds(
        self, monkeypatch, caplog, _capture_runner
    ):
        mod, captured = _capture_runner
        events: list[str] = []
        _patch_slot(monkeypatch, mod, _record_slot(events))

        mod.run_tests("/tmp", scope="full")

        runner = captured["runner"]
        with caplog.at_level("INFO"):
            runner(captured["command"], captured["cwd"], captured["timeout"])

        messages = [rec.getMessage() for rec in caplog.records]
        assert any(
            "queued_at=" in m and "wait_seconds=" in m for m in messages
        ), f"no queued_at/wait_seconds telemetry line: {messages}"

    def test_changed_scope_gate_logs_telemetry(
        self, monkeypatch, caplog, _capture_runner
    ):
        mod, captured = _capture_runner
        monkeypatch.setattr(mod, "_changed_scope_commands", lambda *a, **k: [SCOPED_CMD])
        events: list[str] = []
        _patch_slot(monkeypatch, mod, _record_slot(events))

        mod.run_tests("/tmp", scope="changed")

        runner = captured["runner"]
        with caplog.at_level("INFO"):
            runner(captured["command"], captured["cwd"], captured["timeout"])

        assert any(
            "queued_at=" in rec.getMessage() and "wait_seconds=" in rec.getMessage()
            for rec in caplog.records
        )
