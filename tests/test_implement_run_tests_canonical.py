"""Tests that implement.py's run_tests() routes through canonical cache keys.

Verifies the fix for SA-0MSN6FBFS006Z5QP: ``run_tests()`` previously ran
``python3 -m pytest -x --tb=short -q`` and ``npm test`` through ``run_cached``
— non-canonical (fail-fast ``-x``, ``python3 -m`` prefix) forms whose cache
keys differ from the test skill's full-suite runs (``pytest -q -r a
--disable-warnings`` / ``npm --silent test``). A cached result from the old
form was therefore not full-suite evidence and never shared /skill:test's
cache entry.

These tests assert the commands passed to ``run_cached`` are the canonical
forms and that their normalized cache keys are identical to the test skill's
canonical commands, so cached runs are shared full-suite evidence. The tooling
detection (dev's ``_detect_test_tooling``) is stubbed to ``pytest``/``npm`` so
the test exercises the command-routing branches without needing a real repo.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from test_runner import normalize_test_command

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


def _load_implement() -> object:
    """Import implement.py as a module (mirrors test_implement_abort_safety)."""
    spec = importlib.util.spec_from_file_location(
        "implement_under_test",
        str(_IMPLEMENT_PY),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "implement_scripts"
    sys.modules["implement_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _canned_run(exit_code: int, stdout: str = "") -> dict:
    """A run_cached result shaped like skill.test_cache.run_cached returns."""
    return {
        "stdout": stdout,
        "stderr": "",
        "exit_code": exit_code,
        "completed_at": 0.0,
        "command": "",
        "git_state": "test",
        "cached": True,
    }


def test_run_tests_passes_canonical_pytest_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_tests() must call run_cached with the canonical pytest command."""
    mod = _load_implement()
    captured: list[str] = []

    monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: "pytest")

    def fake_run_cached(command: str, **kwargs) -> dict:
        captured.append(command)
        return _canned_run(exit_code=0)

    monkeypatch.setattr(mod, "run_cached", fake_run_cached)
    result = mod.run_tests("/tmp")

    assert result["exit_code"] == 0
    assert result["success"] is True
    assert captured == [mod.PYTEST_CMD]
    assert mod.PYTEST_CMD == "pytest -q -r a --disable-warnings"
    # No fail-fast / python3 -m prefix: the cache key must match the test skill.
    assert normalize_test_command(mod.PYTEST_CMD) == "pytest -q -r a --disable-warnings"


def test_run_tests_pytest_failure_falls_back_to_canonical_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    """On pytest failure with an npm script, the fallback uses canonical npm."""
    mod = _load_implement()
    captured: list[str] = []

    monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: "pytest")
    monkeypatch.setattr(mod, "_has_test_script", lambda cwd: True)

    def fake_run_cached(command: str, **kwargs) -> dict:
        captured.append(command)
        if command == mod.PYTEST_CMD:
            return _canned_run(exit_code=1, stdout="FAILED test_thing.py::test_x")
        return _canned_run(exit_code=0)

    monkeypatch.setattr(mod, "run_cached", fake_run_cached)
    result = mod.run_tests("/tmp")

    assert result["exit_code"] == 0
    assert result["success"] is True
    assert captured == [mod.PYTEST_CMD, mod.NPM_TEST_CMD]
    assert mod.NPM_TEST_CMD == "npm --silent test"
    assert normalize_test_command(mod.NPM_TEST_CMD) == "npm --silent test"


def test_run_tests_npm_only_uses_canonical_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo with only an npm test script routes through canonical npm."""
    mod = _load_implement()
    captured: list[str] = []

    monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: "npm")

    def fake_run_cached(command: str, **kwargs) -> dict:
        captured.append(command)
        return _canned_run(exit_code=0)

    monkeypatch.setattr(mod, "run_cached", fake_run_cached)
    result = mod.run_tests("/tmp")

    assert result["exit_code"] == 0
    assert result["success"] is True
    assert captured == [mod.NPM_TEST_CMD]
    assert normalize_test_command(mod.NPM_TEST_CMD) == "npm --silent test"


def test_run_tests_cache_keys_match_test_skill_canonical_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalized commands must equal the test skill's canonical suite commands."""
    mod = _load_implement()

    # The test skill's run_tests.py builds PYTEST_CMD the same way:
    # canonicalize_quiet_test_command("pytest").
    from test_runner import canonicalize_quiet_test_command

    assert mod.PYTEST_CMD == canonicalize_quiet_test_command("pytest")
    assert mod.NPM_TEST_CMD == canonicalize_quiet_test_command("npm test")

    # The old non-canonical form normalized to a DIFFERENT key — locked in here
    # so a regression back to it fails this test.
    assert normalize_test_command("python3 -m pytest -x --tb=short -q") != normalize_test_command(
        mod.PYTEST_CMD
    )


def test_run_tests_uses_shlex_safe_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical commands must be split into clean argv lists (no quoting).

    The subprocess boundary (``mod.run_cmd``) is stubbed so the test does not
    require a real ``pytest`` console-script binary on PATH — environments
    where pytest is installed under ``~/.local/bin`` (not on PATH) previously
    failed this test with FileNotFoundError (SA-0MSOMWTXV008BVA8).
    """
    mod = _load_implement()
    argv_calls: list[list[str]] = []

    monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: "pytest")

    def fake_run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        argv_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod, "run_cmd", fake_run_cmd)

    def fake_run_cached(command: str, **kwargs) -> dict:
        runner = kwargs["runner"]
        proc = runner(command, "/tmp", 600)
        assert argv_calls == [
            ["pytest", "-q", "-r", "a", "--disable-warnings"]
        ]  # argv already captured by the stubbed run_cmd
        assert proc.args == ["pytest", "-q", "-r", "a", "--disable-warnings"]
        return _canned_run(exit_code=0)

    monkeypatch.setattr(mod, "run_cached", fake_run_cached)
    mod.run_tests("/tmp")

    assert argv_calls == [["pytest", "-q", "-r", "a", "--disable-warnings"]]
