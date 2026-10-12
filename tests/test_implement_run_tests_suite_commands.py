"""Tests: implement.py's run_tests() delegates suite resolution to the test skill.

Contract (WL-0MUL6LCE00042MB0): ``implement.py finish`` previously preferred
pytest whenever the repo contained pytest-style files, silently ignoring the
canonical full suite declared in ``.pi/test-config.json`` ``suiteCommands``
(e.g. ContextHub's vitest commands). ``run_tests()`` now delegates to the test
skill's ``full_suite_commands`` (the single source of truth: extension
``suiteCommands`` first, then convention detection) and to
``changed_scope_commands`` for changed-file scope.

Acceptance criteria covered:

- AC1: declared ``suiteCommands`` is the primary list — no convention fallback
  when present (including an explicitly empty list).
- AC2: ``run_tests()`` resolves commands via ``full_suite_commands``.
- AC3: ``scope="changed"`` uses the test skill's selector when available and
  falls back to the full suite with a logged warning otherwise.
- AC4: repos without ``suiteCommands`` keep working through the test skill's
  own convention detection; a partial install degrades to the legacy path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

CLI_CMD = "npm --silent test -- tests/cli"
UNIT_CMD = "npm --silent test -- tests/unit"
SCOPED_CMD = "pytest -q -r a --disable-warnings tests/test_foo.py"


def _load_implement() -> object:
    """Import implement.py as a module (mirrors the canonical test loaders)."""
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_suite_commands", str(_IMPLEMENT_PY),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "implement_scripts"
    sys.modules["implement_under_test_suite_commands"] = mod
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


def _write_suite_commands(repo: Path, commands: list[str]) -> None:
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        json.dumps({"suiteCommands": commands}), encoding="utf-8",
    )


def _record_runs(mod: object, monkeypatch: pytest.MonkeyPatch, codes=None) -> list[str]:
    """Patch the canonical guard-owning runner and record the commands it runs.

    ``run_tests()`` now delegates suite execution to the test skill's
    ``guarded_run_all`` (SA-0MUH1MJL6003767Q), so the delegation boundary —
    not ``run_cached`` — is the observable seam for the test-skill path.
    This stub records the flat command list the delegation was asked to run
    and returns a canned ``run_all``-shaped result.

    ``codes`` may be an int (same code for the whole run) or a callable
    taking the recorded command index. Default is exit code 0.
    """
    captured: list[str] = []

    def fake_guarded_run_all(
        cwd=None,
        timeout=600,
        *,
        commands=None,
        scope="full",
        base_ref="origin/dev",
        use_cache=True,
        **kwargs,
    ) -> dict:
        cmds = list(commands or [])
        captured.extend(cmds)
        exit_code = 0
        if codes is not None:
            exit_code = codes(len(captured) - 1) if callable(codes) else codes
        return {
            "success": exit_code == 0,
            "suites": {
                "all": {
                    "returncode": exit_code,
                    "success": exit_code == 0,
                    "failures": [],
                }
            },
            "failures": [],
            "notices": [],
            "scope": scope,
            "type": "full",
            "resolved_scopes": [scope],
        }

    monkeypatch.setattr(mod, "_guarded_run_all", fake_guarded_run_all)
    return captured


class TestSuiteCommandsArePrimary:
    def test_full_scope_runs_all_declared_suite_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1/AC2: both declared commands run, not a detected pytest suite."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_suite_commands(repo, [CLI_CMD, UNIT_CMD])
        mod = _load_implement()
        captured = _record_runs(mod, monkeypatch)

        result = mod.run_tests(str(repo), scope="full")

        assert captured == [CLI_CMD, UNIT_CMD], f"got {captured}"
        assert result["success"] is True
        assert result["scope"] == "full"

    def test_full_scope_ignores_pytest_even_when_pytest_files_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1: suiteCommands wins over convention detection — no pytest run."""
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "test_thing.py").write_text("def test_x(): pass\n")
        _write_suite_commands(repo, [CLI_CMD, UNIT_CMD])
        mod = _load_implement()
        captured = _record_runs(mod, monkeypatch)

        mod.run_tests(str(repo), scope="full")

        assert captured == [CLI_CMD, UNIT_CMD], f"got {captured}"
        assert all("pytest" not in c for c in captured)

    def test_multiple_commands_combine_and_first_failure_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure in any suite command blocks; the aggregate reports it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_suite_commands(repo, [CLI_CMD, UNIT_CMD])
        mod = _load_implement()
        _record_runs(
            mod, monkeypatch,
            codes=lambda i: 0 if i == 0 else 1,
        )

        result = mod.run_tests(str(repo), scope="full")

        assert result["success"] is False
        assert result["exit_code"] == 1
        assert result["scope"] == "full"

    def test_empty_suite_commands_skips_without_convention_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1: an explicitly-empty suiteCommands means 'no runnable suite'."""
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "test_thing.py").write_text("def test_x(): pass\n")
        _write_suite_commands(repo, [])
        mod = _load_implement()

        def _fail(*args, **kwargs):
            raise AssertionError("run_cached must not be called for an empty suite")

        monkeypatch.setattr(mod, "run_cached", _fail)

        result = mod.run_tests(str(repo), scope="full")

        assert result["skipped"] is True
        assert result["success"] is True


class TestChangedScope:
    def test_changed_scope_with_suite_commands_warns_and_runs_full(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        """AC3: custom suiteCommands aren't introspectable → full fallback."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_suite_commands(repo, [CLI_CMD, UNIT_CMD])
        mod = _load_implement()
        captured = _record_runs(mod, monkeypatch)

        with caplog.at_level("WARNING"):
            result = mod.run_tests(str(repo), scope="changed")

        assert captured == [CLI_CMD, UNIT_CMD], f"got {captured}"
        assert result["scope"] == "full"
        assert any(
            "changed-scope selection unavailable" in rec.getMessage()
            for rec in caplog.records
        )

    def test_changed_scope_uses_selector_when_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3: the test skill's changed-file selector drives scoped runs."""
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "test_thing.py").write_text("def test_x(): pass\n")
        mod = _load_implement()
        captured = _record_runs(mod, monkeypatch)
        monkeypatch.setattr(
            mod, "_changed_scope_commands", lambda *a, **k: [SCOPED_CMD]
        )

        result = mod.run_tests(str(repo), scope="changed")

        assert captured == [SCOPED_CMD], f"got {captured}"
        assert result["scope"] == "changed"


class TestConventionDetectionDelegation:
    def test_pytest_convention_delegates_to_full_suite_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2/AC4: convention detection now resolves via the test skill."""
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "test_thing.py").write_text("def test_x(): pass\n")
        mod = _load_implement()
        captured = _record_runs(mod, monkeypatch)

        result = mod.run_tests(str(repo), scope="full")

        assert captured == [mod.PYTEST_CMD], f"got {captured}"
        assert result["tooling"] == "suite"

    def test_npm_convention_delegates_to_full_suite_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2/AC4: an npm-only repo resolves to the canonical npm command."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8",
        )
        mod = _load_implement()
        captured = _record_runs(mod, monkeypatch)

        mod.run_tests(str(repo), scope="full")

        assert captured == [mod.NPM_TEST_CMD], f"got {captured}"

    def test_node_suite_dirs_delegate_to_full_suite_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2/AC4: node suite dirs resolve to node --test commands."""
        repo = tmp_path / "repo"
        (repo / "tests" / "node").mkdir(parents=True)
        (repo / "tests" / "node" / "thing.test.mjs").write_text("// test\n")
        mod = _load_implement()
        captured = _record_runs(mod, monkeypatch)

        mod.run_tests(str(repo), scope="full")

        assert captured == ['node --test "tests/node/**/*.mjs"'], f"got {captured}"


class TestGracefulDegradation:
    def test_partial_install_falls_back_to_legacy_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4: without the test skill, the legacy convention path still runs."""
        repo = tmp_path / "repo"
        repo.mkdir()
        mod = _load_implement()
        monkeypatch.setattr(mod, "_full_suite_commands", None)
        monkeypatch.setattr(mod, "_detect_test_tooling", lambda cwd: "pytest")
        captured: list[str] = []

        def fake_run_cached(command: str, **kwargs) -> dict:
            captured.append(command)
            return _canned_run(0)

        monkeypatch.setattr(mod, "run_cached", fake_run_cached)

        result = mod.run_tests(str(repo), scope="full")

        assert captured == [mod.PYTEST_CMD], f"got {captured}"
        assert result["tooling"] == "pytest"


class TestResolveFullSuiteCommands:
    def test_declared_suite_commands_returned_verbatim(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_suite_commands(repo, [CLI_CMD, UNIT_CMD])
        mod = _load_implement()

        assert mod._resolve_full_suite_commands(str(repo)) == [CLI_CMD, UNIT_CMD]

    def test_declared_empty_list_returns_empty(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_suite_commands(repo, [])
        mod = _load_implement()

        assert mod._resolve_full_suite_commands(str(repo)) == []

    def test_undeclared_empty_returns_none(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        mod = _load_implement()

        assert mod._resolve_full_suite_commands(str(repo)) is None
