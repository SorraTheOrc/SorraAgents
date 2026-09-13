"""Tests for the test skill ``--type`` parameter (SA-0MTJQB2MA008HMO6).

Covers:

- AC1: ``--type`` parsing/defaults/``--help`` and unknown-type errors listing
  the allowed values.
- AC2: machine-readable local extension delegation and the missing-minimum
  diagnostic (never a silent full-suite substitution).
- AC3: locally-defined extra types are accepted and dispatched; unknown-type
  errors list them.
- AC4: convention fallbacks (``tests/unit``) and clear diagnostics when no
  subset can be resolved.
- AC5/AC6: backwards compatibility and the cache/evidence contract — only
  ``full`` populates the audit-accepted full-suite entry; typed runs use
  independent cache keys and record their type.
"""

from __future__ import annotations

import json
import sys as _sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"  # <skills>/test/scripts
if str(_RUNNER_DIR) not in _sys.path:
    _sys.path.insert(0, str(_RUNNER_DIR))

import run_tests
from run_tests import (
    DEFAULT_TEST_TYPE,
    MINIMUM_TEST_TYPES,
    TypeResolutionError,
    UnknownTestTypeError,
    allowed_test_types,
    build_parser,
    convention_type_commands,
    local_test_types,
    resolve_type_commands,
    run_all,
    run_suite,
    run_summary,
)
from test_cache import cache_key, query_cached, run_cached

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, *, pytest_suite: bool = True) -> Path:
    """Create a throwaway project with a conventional pytest unit suite."""
    repo = tmp_path / "proj"
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_alpha.py").write_text(
        "def test_alpha():\n    assert True\n", encoding="utf-8"
    )
    if pytest_suite:
        (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return repo


def _write_types(repo: Path, types: object) -> Path:
    """Write the local test extension's ``types`` map (may be malformed)."""
    directory = repo / ".pi" / "skills_extensions" / "test"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "extension.json"
    path.write_text(json.dumps({"types": types}), encoding="utf-8")
    return path


def _fake_runner(calls: list[str] | None = None):
    """A cache runner returning success without spawning anything."""
    def runner(command: str, cwd: str | Path, timeout: int) -> SimpleNamespace:
        if calls is not None:
            calls.append(command)
        return SimpleNamespace(stdout="1 passed\n", stderr="", returncode=0)

    return runner


# ---------------------------------------------------------------------------
# AC1: CLI parsing, defaults and --help
# ---------------------------------------------------------------------------


class TestParser:
    def test_default_type_is_full(self) -> None:
        args = build_parser().parse_args([])
        assert args.test_type == "full"
        assert DEFAULT_TEST_TYPE == "full"

    def test_type_flag_is_accepted(self) -> None:
        assert build_parser().parse_args(["--type", "unit"]).test_type == "unit"
        assert build_parser().parse_args(["--type", "dev"]).test_type == "dev"

    def test_help_documents_type(self) -> None:
        help_text = build_parser().format_help()
        assert "--type" in help_text
        assert "unit" in help_text


# ---------------------------------------------------------------------------
# AC2/AC3: local extension loading
# ---------------------------------------------------------------------------


class TestLocalTestTypes:
    def test_no_extension_returns_none(self, tmp_path: Path) -> None:
        assert local_test_types(tmp_path) is None

    def test_types_map_is_normalised(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"unit": ["pytest tests/unit -q"], "dev": "pytest tests -q"})
        loaded = local_test_types(repo)
        assert loaded == {"unit": ["pytest tests/unit -q"], "dev": ["pytest tests -q"]}

    def test_prose_only_extension_has_no_types(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        directory = repo / ".pi" / "skills_extensions" / "test"
        directory.mkdir(parents=True)
        (directory / "SKILL_PREFIX.md").write_text("hello", encoding="utf-8")
        assert local_test_types(repo) is None

    def test_non_object_types_map_is_rejected(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, ["unit"])
        with pytest.raises(run_tests.SkillExtensionError) as excinfo:
            local_test_types(repo)
        assert "extension.json" in str(excinfo.value)

    def test_invalid_commands_are_rejected(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"unit": []})
        with pytest.raises(run_tests.SkillExtensionError) as excinfo:
            local_test_types(repo)
        assert "extension.json" in str(excinfo.value)

    def test_allowed_types_include_local_extras(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        assert allowed_test_types(repo) == sorted(MINIMUM_TEST_TYPES)
        _write_types(repo, {"dev": ["echo dev"]})
        assert allowed_test_types(repo) == sorted([*MINIMUM_TEST_TYPES, "dev"])


# ---------------------------------------------------------------------------
# AC2/AC3/AC4: type → command resolution
# ---------------------------------------------------------------------------


class TestResolveTypeCommands:
    def test_full_without_extension_uses_full_suite(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        assert resolve_type_commands(repo, "full") == run_tests.full_suite_commands(repo)

    def test_unit_without_extension_uses_convention(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        assert resolve_type_commands(repo, "unit") == convention_type_commands(repo, "unit")
        assert resolve_type_commands(repo, "unit") == [
            run_tests.canonicalize_quiet_test_command("pytest tests/unit")
        ]

    def test_unit_without_convention_raises(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path, pytest_suite=False)
        (repo / "tests" / "unit" / "test_alpha.py").unlink()
        with pytest.raises(TypeResolutionError):
            resolve_type_commands(repo, "unit")

    def test_node_unit_convention(self, tmp_path: Path) -> None:
        repo = tmp_path / "node_proj"
        (repo / "tests" / "unit").mkdir(parents=True)
        (repo / "tests" / "unit" / "a.test.mjs").write_text("// test\n", encoding="utf-8")
        assert resolve_type_commands(repo, "unit") == ['node --test "tests/unit/**/*.mjs"']

    def test_smoke_without_convention_raises(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        with pytest.raises(TypeResolutionError) as excinfo:
            resolve_type_commands(repo, "smoke")
        assert "smoke" in str(excinfo.value)

    def test_local_unit_commands_win(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"unit": ["echo local-unit"]})
        assert resolve_type_commands(repo, "unit") == ["echo local-unit"]

    def test_locally_defined_extra_type_resolves(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"dev": ["echo dev-profile"], "full": ["echo local-full"]})
        assert resolve_type_commands(repo, "dev") == ["echo dev-profile"]

    def test_local_full_override_wins(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"full": ["echo local-full"]})
        assert resolve_type_commands(repo, "full") == ["echo local-full"]

    def test_local_extension_omitting_minimum_type_fails(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        extension = _write_types(repo, {"dev": ["echo dev"]})
        with pytest.raises(TypeResolutionError) as excinfo:
            resolve_type_commands(repo, "unit")
        message = str(excinfo.value)
        assert "unit" in message
        assert "extension.json" in message
        # Never silently substitute the full suite.
        assert extension.exists()

    def test_local_extension_omitting_full_falls_back(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"unit": ["echo unit"]})
        assert resolve_type_commands(repo, "full") == run_tests.full_suite_commands(repo)

    def test_unknown_type_raises(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        with pytest.raises(UnknownTestTypeError):
            resolve_type_commands(repo, "e2e")


# ---------------------------------------------------------------------------
# AC1/AC3: main() validation and dispatch
# ---------------------------------------------------------------------------


class TestMainDispatch:
    def _capture_run_all(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        captured: dict = {}

        def fake_run_all(**kwargs):
            captured.update(kwargs)
            return {
                "success": True,
                "type": kwargs.get("test_type"),
                "suites": {},
                "failures": [],
                "notices": [],
                "scope": "full",
                "resolved_scopes": [],
                "timing": {},
            }

        monkeypatch.setattr(run_tests, "run_all", fake_run_all)
        return captured

    def test_bare_invocation_is_full_legacy_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _make_project(tmp_path)
        captured = self._capture_run_all(monkeypatch)
        assert run_tests.main(["--json", "--project-root", str(repo)]) == 0
        assert captured["test_type"] == "full"
        # Legacy path: no explicit command override (AC5).
        assert captured["commands"] is None

    def test_unit_convention_fallback_dispatched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _make_project(tmp_path)
        captured = self._capture_run_all(monkeypatch)
        assert run_tests.main(["--type", "unit", "--json", "--project-root", str(repo)]) == 0
        assert captured["commands"] == [
            run_tests.canonicalize_quiet_test_command("pytest tests/unit")
        ]

    def test_locally_defined_type_dispatched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"dev": ["echo dev-profile"]})
        captured = self._capture_run_all(monkeypatch)
        rc = run_tests.main(["--type", "dev", "--json", "--project-root", str(repo)])
        assert rc == 0
        assert captured["test_type"] == "dev"
        assert captured["commands"] == ["echo dev-profile"]

    def test_unknown_type_lists_minimum_types(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        repo = _make_project(tmp_path)
        rc = run_tests.main(["--type", "nope", "--project-root", str(repo)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown test type" in err
        for minimum in MINIMUM_TEST_TYPES:
            assert minimum in err

    def test_unknown_type_lists_local_types(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"dev": ["echo dev"]})
        rc = run_tests.main(["--type", "e2e", "--project-root", str(repo)])
        assert rc == 2
        assert "dev" in capsys.readouterr().err

    def test_missing_minimum_type_names_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        repo = _make_project(tmp_path)
        _write_types(repo, {"dev": ["echo dev"]})
        rc = run_tests.main(["--type", "unit", "--project-root", str(repo)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "unit" in err
        assert "extension.json" in err


# ---------------------------------------------------------------------------
# AC6: type-aware cache and evidence
# ---------------------------------------------------------------------------


class TestTypeAwareCache:
    def test_full_type_preserves_legacy_cache_key(self) -> None:
        assert cache_key("cmd", "state") == cache_key("cmd", "state", "full")

    def test_non_full_type_has_independent_cache_key(self) -> None:
        assert cache_key("cmd", "state", "unit") != cache_key("cmd", "state", "full")
        assert cache_key("cmd", "state", "unit") != cache_key("cmd", "state", "smoke")

    def test_typed_run_never_populates_full_entry(self, tmp_path: Path) -> None:
        run_cached("echo unit", cwd=tmp_path, runner=_fake_runner(), test_type="unit")
        assert query_cached("echo unit", cwd=tmp_path) is None
        entry = query_cached("echo unit", cwd=tmp_path, test_type="unit")
        assert entry is not None
        assert entry["test_type"] == "unit"

    def test_full_run_populates_full_entry(self, tmp_path: Path) -> None:
        run_cached("echo full", cwd=tmp_path, runner=_fake_runner(), test_type="full")
        assert query_cached("echo full", cwd=tmp_path) is not None

    def test_run_suite_records_type_and_uses_typed_cache(self, tmp_path: Path) -> None:
        with mock.patch.object(
            run_tests, "resolve_type_commands", return_value=["echo typed-unit"]
        ):
            result = run_suite("all", cwd=tmp_path, test_type="unit")
        assert result["type"] == "unit"
        assert result["success"] is True
        assert "echo typed-unit" in result["command"]
        assert query_cached("echo typed-unit", cwd=tmp_path) is None
        assert query_cached("echo typed-unit", cwd=tmp_path, test_type="unit") is not None

    def test_run_all_aggregate_records_type(self, tmp_path: Path) -> None:
        with mock.patch.object(
            run_tests, "resolve_type_commands", return_value=["echo typed-smoke"]
        ):
            result = run_all(("all",), cwd=tmp_path, test_type="smoke")
        assert result["type"] == "smoke"
        assert result["suites"]["all"]["type"] == "smoke"

    def test_summary_reports_recorded_type(self, tmp_path: Path) -> None:
        run_cached(
            "echo unit-summary", cwd=tmp_path, runner=_fake_runner(), test_type="unit"
        )
        summary = run_summary(
            ("all",), cwd=tmp_path, test_type="unit", commands=["echo unit-summary"]
        )
        assert summary["success"] is True
        assert summary["types"]["all"] == "unit"

    def test_summary_full_query_misses_typed_entry(self, tmp_path: Path) -> None:
        run_cached(
            "echo unit-summary", cwd=tmp_path, runner=_fake_runner(), test_type="unit"
        )
        summary = run_summary(("all",), cwd=tmp_path, commands=["echo unit-summary"])
        assert summary["success"] is False
        assert "all" in summary["missing"]
