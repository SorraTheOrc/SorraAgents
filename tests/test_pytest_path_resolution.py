"""Tests for pytest path resolution fallback (SA-0MSQ012QG005N22S).

Verifies that when `pytest` is not on PATH, the runner falls back
through ~/.local/bin/pytest → python3 -m pytest, and that the resolved
executable is used at spawn time while the canonical command string
(cache key) stays stable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import skill.test.scripts.run_tests as rt
from skill.test_runner import (
    canonicalize_quiet_test_command,
    executable_test_command,
    resolve_pytest_command,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _reset_pytest_cache() -> None:
    """Clear the module-level pytest command cache so tests are isolated."""
    import skill.test_runner as tr

    tr._PYTEST_COMMAND = None


def _which_pytest_missing(original_which, name, mode=0, path=None):
    """shutil.which stand-in that hides pytest but resolves python3."""
    if name == "pytest":
        return None  # pytest not on PATH
    return original_which(name, mode, path)


# ---------------------------------------------------------------------------
# resolve_pytest_command unit tests
# ---------------------------------------------------------------------------


def test_resolve_pytest_command_uses_which_when_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When pytest is on PATH (shutil.which finds it), use the plain command."""
    _reset_pytest_cache()
    fake_pytest = tmp_path / "bin" / "pytest"
    fake_pytest.parent.mkdir(parents=True, exist_ok=True)
    fake_pytest.write_text("#!/bin/sh\nexit 0\n")
    fake_pytest.chmod(0o755)

    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: str(fake_pytest) if name == "pytest" else None
    )
    assert resolve_pytest_command() == "pytest"


def test_resolve_pytest_command_fallback_to_local_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When pytest is NOT on PATH but ~/.local/bin/pytest exists, use it."""
    _reset_pytest_cache()
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    local_pytest = local_bin / "pytest"
    local_pytest.write_text("#!/bin/sh\nexit 0\n")
    local_pytest.chmod(0o755)

    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path)
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_pytest_command() == str(local_pytest)


def test_resolve_pytest_command_fallback_to_python_m_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When pytest is not on PATH and no ~/.local/bin/pytest, fall back to python3 -m pytest."""
    _reset_pytest_cache()
    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path)
    )
    monkeypatch.delenv("HOME", raising=False)

    result = resolve_pytest_command()
    assert result == "python3 -m pytest"


def test_resolve_pytest_command_caches_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved command should be stable across calls (cached at module level)."""
    _reset_pytest_cache()
    import skill.test_runner as tr

    monkeypatch.delenv("HOME", raising=False)
    calls = 0

    def counting_which(name, mode=0, path=None):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(tr.shutil, "which", counting_which)
    first = resolve_pytest_command()
    second = resolve_pytest_command()
    assert first == second
    assert calls == 2  # both PATH probes happen on the FIRST call only


# ---------------------------------------------------------------------------
# executable_test_command unit tests
# ---------------------------------------------------------------------------


def test_executable_command_unresolved_when_pytest_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With pytest on PATH the canonical command stays as-is."""
    _reset_pytest_cache()
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: "/usr/bin/pytest" if name == "pytest" else None
    )
    cmd = canonicalize_quiet_test_command("pytest")
    assert cmd == "pytest -q -r a --disable-warnings"
    assert executable_test_command(cmd) == cmd


def test_executable_command_resolves_bare_pytest_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare pytest prefix is replaced by the resolved executable."""
    _reset_pytest_cache()
    local_pytest = tmp_path / ".local" / "bin" / "pytest"
    local_pytest.parent.mkdir(parents=True, exist_ok=True)
    local_pytest.write_text("#!/bin/sh\nexit 0\n")
    local_pytest.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name, mode=0, path=None: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = resolve_pytest_command()
    assert resolved == str(local_pytest)

    cmd = "pytest -q -r a --disable-warnings"
    executable = executable_test_command(cmd)
    assert executable.startswith(str(local_pytest))
    assert "-q -r a --disable-warnings" in executable


def test_executable_command_python_m_pytest_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit ``python3 -m pytest`` forms are left unchanged."""
    _reset_pytest_cache()
    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path)
    )
    monkeypatch.delenv("HOME", raising=False)

    cmd = canonicalize_quiet_test_command("python3 -m pytest -k foo")
    assert cmd.startswith("python3 -m pytest")
    assert executable_test_command(cmd) == cmd


def test_executable_command_leaves_non_pytest_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-pytest commands are never rewritten."""
    assert executable_test_command("npm --silent test") == "npm --silent test"
    assert executable_test_command("node --test tests/node/*.mjs") == "node --test tests/node/*.mjs"


# ---------------------------------------------------------------------------
# Integration: run_tests.py executes the resolved pytest
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway git repo used as project root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "rt-test@example.com")
    git(repo, "config", "user.name", "RT Test")
    (repo / "file.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    monkeypatch.setattr(rt, "detect_project_root", lambda: repo)
    return repo


def test_pytest_command_stays_canonical_when_pytest_missing(
    cache_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pytest_command() keeps the canonical form even when the executable is
    missing — the cache key and cross-consumer contract stay stable."""
    _reset_pytest_cache()
    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path)
    )
    monkeypatch.delenv("HOME", raising=False)

    assert rt.pytest_command() == "pytest -q -r a --disable-warnings"


def test_run_tests_spawns_resolved_pytest_when_missing(
    cache_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """run_tests.py spawns the resolved pytest binary when pytest is not on
    PATH (no manual export needed — SA-0MSQ012QG005N22S AC1)."""
    _reset_pytest_cache()
    local_bin = cache_repo / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    fake_pytest = local_bin / "pytest"
    fake_pytest.write_text("#!/bin/sh\nexit 0\n")
    fake_pytest.chmod(0o755)

    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path)
    )
    monkeypatch.setenv("HOME", str(cache_repo))

    captured: list[str] = []

    def fake_run(cmd, **kwargs):
        captured.append(" ".join(cmd))
        return SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(rt, "_run_cmd", fake_run)

    code = rt.main(["--suite", "pytest", "--json", "--no-cache"])
    assert code == 0
    assert len(captured) == 1
    assert captured[0].startswith(str(fake_pytest))  # resolved executable spawned
    assert "-q -r a --disable-warnings" in captured[0]