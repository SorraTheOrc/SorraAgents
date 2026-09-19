"""Tests for implement.py's config-driven test timeout (SA-0MTXKIIRV009MJWG).

Covers the acceptance criteria:

- AC1: ``_resolve_test_timeout`` reads ``timeoutPerCommand`` from
  ``.pi/test-config.json`` with a 600s default fallback.
- AC2: every ``run_cached`` call in the finish test loop passes the
  resolved timeout (never a literal 600).
- AC3: repos without ``.pi/test-config.json`` keep the 600s default.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

DEFAULT_TIMEOUT = 600
CONFIGURED_TIMEOUT = 1500


def _load_implement() -> object:
    """Import implement.py as a module (mirrors canonical tests)."""
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_timeout", str(_IMPLEMENT_PY),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "implement_scripts"
    sys.modules["implement_under_test_timeout"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestResolveTestTimeout:
    def test_reads_timeout_per_command_from_config(self, tmp_path: Path):
        """AC1: a valid timeoutPerCommand in .pi/test-config.json wins."""
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": CONFIGURED_TIMEOUT}), encoding="utf-8",
        )
        mod = _load_implement()
        assert mod._resolve_test_timeout(str(tmp_path)) == CONFIGURED_TIMEOUT

    def test_absent_config_uses_600_default(self, tmp_path: Path):
        """AC3: no .pi/test-config.json → 600s default preserved."""
        mod = _load_implement()
        assert mod._resolve_test_timeout(str(tmp_path)) == DEFAULT_TIMEOUT

    def test_malformed_config_uses_600_default(self, tmp_path: Path):
        """Corrupt JSON is treated as absent — never an error."""
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text("{not valid json", encoding="utf-8")
        mod = _load_implement()
        assert mod._resolve_test_timeout(str(tmp_path)) == DEFAULT_TIMEOUT

    def test_non_dict_config_uses_600_default(self, tmp_path: Path):
        """A JSON array/string config root is treated as absent."""
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text("[1,2,3]", encoding="utf-8")
        mod = _load_implement()
        assert mod._resolve_test_timeout(str(tmp_path)) == DEFAULT_TIMEOUT

    def test_missing_field_uses_600_default(self, tmp_path: Path):
        """Config without timeoutPerCommand → 600 default."""
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"suiteCommands": ["npm test"]}), encoding="utf-8",
        )
        mod = _load_implement()
        assert mod._resolve_test_timeout(str(tmp_path)) == DEFAULT_TIMEOUT

    def test_non_positive_value_uses_600_default(self, tmp_path: Path):
        """Zero/negative timeouts are rejected (a 0s cap is never useful)."""
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": 0}), encoding="utf-8",
        )
        mod = _load_implement()
        assert mod._resolve_test_timeout(str(tmp_path)) == DEFAULT_TIMEOUT

    def test_boolean_value_uses_600_default(self, tmp_path: Path):
        """A bool (True is an int subclass) must not be accepted as a timeout."""
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": True}), encoding="utf-8",
        )
        mod = _load_implement()
        assert mod._resolve_test_timeout(str(tmp_path)) == DEFAULT_TIMEOUT


class TestRunCachedReceivesResolvedTimeout:
    def _make_mod(self, monkeypatch: pytest.MonkeyPatch) -> tuple[object, list]:
        """Load implement.py with tooling detection stubbed and a recording
        run_cached; returns (mod, captured_timeouts)."""
        mod = _load_implement()
        monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: "pytest")
        captured: list[int] = []

        def fake_run_cached(command: str, **kwargs) -> dict:
            captured.append(kwargs["timeout"])
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "completed_at": 0.0,
                "command": command,
                "git_state": "test",
                "cached": True,
            }

        monkeypatch.setattr(mod, "run_cached", fake_run_cached)
        return mod, captured

    def test_run_tests_passes_resolved_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """AC2: the pytest channel passes the config-resolved timeout to
        run_cached (never a literal 600)."""
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": CONFIGURED_TIMEOUT}), encoding="utf-8",
        )
        mod, captured = self._make_mod(monkeypatch)

        result = mod.run_tests(str(tmp_path), scope="full")

        assert result["success"] is True
        assert captured == [CONFIGURED_TIMEOUT], f"got {captured}"