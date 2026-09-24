"""Tests for implement.py run_tests() scope-aware execution.

Covers SA-0MT6CENPW004V2JN:

- AC1: the worktree test loop runs changed-scope (affected tests only).
- AC2: finish runs a final full-suite gate before commit (scope=full), so
  the pre-push hook's full check is a cheap cache hit and a red full tree
  is never pushed.
- AC3: test results passed to the "blocked by test failures" gate carry
  ``scope`` info.
- AC4: when changed-scope selection yields nothing (all changed files are
  non-test / no dev baseline / custom suiteCommands / non-pytest tooling),
  run_tests warns and falls back to the full scope — a scoped run must
  never silently skip testing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

SCOPED_CMD = "pytest -q -r a --disable-warnings tests/test_foo.py"


def _load_implement() -> object:
    """Import implement.py as a module (mirrors canonical tests)."""
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_scope", str(_IMPLEMENT_PY),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "implement_scripts"
    sys.modules["implement_under_test_scope"] = mod
    spec.loader.exec_module(mod)
    return mod


def _canned_run(exit_code: int, stdout: str = "") -> dict:
    """A run_cached result shaped like skill.test_cache.run_cached returns."""
    return {
        "stdout": stdout,
        "stderr": "",
        "exit_code": exit_code,
        "completed_at": 0.0,
        "command": "test",
        "git_state": "test",
        "cached": True,
    }


def _make_mod(monkeypatch: pytest.MonkeyPatch, tooling: str = "pytest") -> object:
    """Load implement.py with tooling detection stubbed and a recording
    run_cached; returns (mod, captured)."""
    mod = _load_implement()
    monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: tooling)
    captured: list[str] = []

    def fake_run_cached(command: str, **kwargs) -> dict:
        captured.append(command)
        return _canned_run(exit_code=0)

    monkeypatch.setattr(mod, "run_cached", fake_run_cached)
    return mod, captured


class TestChangedScopeWorktreeLoop:
    def test_changed_scope_runs_selected_tests(self, monkeypatch: pytest.MonkeyPatch):
        """AC1: scope=changed invokes the scoped pytest command, not the
        full PYTEST_CMD, and records scope=changed."""
        mod, captured = _make_mod(monkeypatch)
        monkeypatch.setattr(mod, "_changed_scope_commands", lambda *a, **k: [SCOPED_CMD])

        result = mod.run_tests("/tmp", scope="changed")

        assert captured == [SCOPED_CMD], f"got {captured}"
        assert result["scope"] == "changed"

    def test_changed_scope_unavailable_falls_back_to_full(
        self, monkeypatch: pytest.MonkeyPatch, caplog
    ):
        """AC4: selection unavailable (None) → warn + fall back to full with
        scope=full in the result."""
        mod, captured = _make_mod(monkeypatch)
        monkeypatch.setattr(mod, "_changed_scope_commands", lambda *a, **k: None)

        with caplog.at_level("WARNING"):
            result = mod.run_tests("/tmp", scope="changed")

        assert captured == [mod.PYTEST_CMD], f"got {captured}"
        assert result["scope"] == "full"
        assert any("changed-scope selection unavailable" in r.message
                   for r in caplog.records)

    def test_changed_scope_node_only_selection_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A selection that resolves only to node commands falls back to full
        (implement's pytest channel cannot run node --test)."""
        mod, captured = _make_mod(monkeypatch)
        monkeypatch.setattr(
            mod, "_changed_scope_commands", lambda *a, **k: ['node --test x.mjs']
        )

        result = mod.run_tests("/tmp", scope="changed")

        assert captured == [mod.PYTEST_CMD]
        assert result["scope"] == "full"


class TestFullScopeGate:
    def test_full_scope_skips_changed_selection(self, monkeypatch: pytest.MonkeyPatch):
        """AC2: scope=full must bypass the changed selector entirely."""
        mod, captured = _make_mod(monkeypatch)
        calls: list[str] = []
        monkeypatch.setattr(
            mod, "_changed_scope_commands",
            lambda *a, **k: calls.append("selector") or [SCOPED_CMD],
        )

        result = mod.run_tests("/tmp", scope="full")

        assert captured == [mod.PYTEST_CMD], f"got {captured}"
        assert calls == []
        assert result["scope"] == "full"


class TestScopeMetadata:
    def test_results_carry_scope(self, monkeypatch: pytest.MonkeyPatch):
        """AC3: every result path carries a scope key."""
        mod = _load_implement()
        monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: None)
        result = mod.run_tests("/tmp", scope="changed")
        assert result["skipped"] is True
        assert result["scope"] == "full"  # skipped no-op records no partial evidence

    def test_npm_tooling_changed_scope_warns_and_runs_full(
        self, monkeypatch: pytest.MonkeyPatch, caplog
    ):
        """npm tooling is not subsettable — warn + run full scope."""
        mod, captured = _make_mod(monkeypatch, tooling="npm")

        with caplog.at_level("WARNING"):
            result = mod.run_tests("/tmp", scope="changed")

        assert captured == [mod.NPM_TEST_CMD]
        assert result["scope"] == "full"
        assert any("npm tooling is not subsettable" in r.message
                   for r in caplog.records)