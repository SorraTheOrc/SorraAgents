"""Tests for pytest resolution precedence.

Covers the original PATH fallback contract (SA-0MSQ012QG005N22S) and its
replacement precedence (SA-0MVK154IE007OS3D): the interpreter that starts the
runner (``sys.executable -m pytest``) is preferred over a PATH ``pytest``
bound to a different interpreter, explicit ``PYTEST_BIN`` / ``TEST_PYTHON``
overrides win over every other candidate, every candidate is probed rather
than assumed, and the resolved interpreter is recorded in the run output and
in the cache entry.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test.scripts.run_tests as rt
import test_cache as tc
import test_runner as tr
from test_runner import (
    canonicalize_quiet_test_command,
    executable_test_command,
    resolve_pytest_command,
    resolve_pytest_executable,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _reset_pytest_cache() -> None:
    """Clear the module-level pytest command cache so tests are isolated."""
    tr._PYTEST_COMMAND = None


@pytest.fixture(autouse=True)
def _clean_pytest_env(monkeypatch: pytest.MonkeyPatch):
    """Isolate resolution state: no overrides leak between tests."""
    monkeypatch.delenv("PYTEST_BIN", raising=False)
    monkeypatch.delenv("TEST_PYTHON", raising=False)
    _reset_pytest_cache()
    yield
    _reset_pytest_cache()


def _script(path: Path, body: str) -> Path:
    """Write an executable shell script and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _which_pytest_missing(original_which, name, mode=0, path=None):
    """shutil.which stand-in that hides pytest but resolves python3."""
    if name == "pytest":
        return None  # pytest not on PATH
    return original_which(name, mode, path)


# ---------------------------------------------------------------------------
# resolve_pytest_command precedence (SA-0MVK154IE007OS3D AC1/AC2)
# ---------------------------------------------------------------------------


def test_sys_executable_wins_over_path_pytest_bound_to_another_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3(a): the runner's interpreter beats a PATH pytest that cannot run
    pytest (a foreign/system interpreter), so collection cannot fall over to
    a Python without the project's dependencies."""
    stale_pytest = _script(tmp_path / "bin" / "pytest", "#!/bin/sh\nexit 3\n")
    original_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, mode=0, path=None: str(stale_pytest) if name == "pytest" else original_which(name, mode, path),
    )

    resolved = resolve_pytest_command()
    assert resolved == f"{sys.executable} -m pytest"

    # The canonical command keeps its stable form; only the executable prefix
    # is rewritten to the runner's interpreter.
    canonical = canonicalize_quiet_test_command("pytest")
    assert executable_test_command(canonical) == f"{sys.executable} -m pytest -q -r a --disable-warnings"


def test_unrunnable_candidates_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: every candidate is probed; a missing/unrunnable override is
    skipped instead of being selected blindly."""
    monkeypatch.setenv("PYTEST_BIN", str(tmp_path / "does-not-exist" / "pytest"))
    monkeypatch.setenv("TEST_PYTHON", str(tmp_path / "does-not-exist" / "python"))

    assert resolve_pytest_command() == f"{sys.executable} -m pytest"


def test_pytest_bin_override_wins_over_every_other_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3(b): PYTEST_BIN wins even though sys.executable can run pytest."""
    explicit = _script(tmp_path / "explicit" / "pytest", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PYTEST_BIN", str(explicit))
    monkeypatch.setenv("TEST_PYTHON", sys.executable)

    assert resolve_pytest_command() == str(explicit)


def test_test_python_override_wins_over_sys_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3(b): TEST_PYTHON wins over the default ``sys.executable``."""
    wrapper = _script(
        tmp_path / "bin" / "mypython",
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
    )
    monkeypatch.setenv("TEST_PYTHON", str(wrapper))

    assert resolve_pytest_command() == f"{wrapper} -m pytest"


def test_all_candidates_unrunnable_falls_back_to_bare_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nothing probes successfully the historical bare ``pytest`` is
    returned so the caller surfaces the spawn error (never a silent skip)."""
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: False)

    assert resolve_pytest_command() == "pytest"


def test_bare_pytest_on_path_used_when_earlier_candidates_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updated which-first test (AC3c): PATH ``pytest`` is only selected once
    the override and ``sys.executable -m pytest`` candidates fail the probe."""
    fake_pytest = _script(tmp_path / "bin" / "pytest", "#!/bin/sh\nexit 0\n")

    original_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, mode=0, path=None: str(fake_pytest) if name == "pytest" else original_which(name, mode, path),
    )
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: candidate == ["pytest"])

    assert resolve_pytest_command() == "pytest"


def test_local_bin_fallback_used_when_earlier_candidates_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updated ~/.local/bin fallback test (AC3c, SA-0MSQ012QG005N22S): the
    user-installed copy is still used when PATH lacks pytest and the
    interpreter candidates cannot run it."""
    local_pytest = _script(tmp_path / ".local" / "bin" / "pytest", "#!/bin/sh\nexit 0\n")

    original_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path),
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        tr, "_candidate_runs_pytest", lambda candidate: candidate == [str(local_pytest)]
    )

    assert resolve_pytest_command() == str(local_pytest)


def test_python3_m_pytest_last_resort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Updated python3 fallback test (AC3c): ``python3 -m pytest`` is the last
    candidate, selected only when nothing earlier runs pytest."""
    original_which = shutil.which

    def which(name, mode=0, path=None):
        if name == "pytest":
            return None
        return original_which(name, mode, path)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(
        tr, "_candidate_runs_pytest", lambda candidate: candidate == ["python3", "-m", "pytest"]
    )

    assert resolve_pytest_command() == "python3 -m pytest"


def test_resolve_pytest_command_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3(d): the resolved command is memoised — candidates are probed once
    per process, not on every call."""
    probes: list[list[str]] = []

    def probe(candidate: list[str]) -> bool:
        probes.append(candidate)
        return True

    monkeypatch.setattr(tr, "_candidate_runs_pytest", probe)

    first = resolve_pytest_command()
    second = resolve_pytest_command()

    assert first == second
    assert len(probes) == 1


# ---------------------------------------------------------------------------
# resolve_pytest_executable: the auditable interpreter label (AC2)
# ---------------------------------------------------------------------------


def test_resolve_pytest_executable_reports_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label names the concrete interpreter/exe, not the bare command."""
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: True)

    assert resolve_pytest_executable() == sys.executable


def test_resolve_pytest_executable_resolves_bare_pytest_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected bare ``pytest`` is reported as its absolute path."""
    fake_pytest = _script(tmp_path / "bin" / "pytest", "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: str(fake_pytest) if name == "pytest" else None
    )
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: candidate == ["pytest"])

    assert resolve_pytest_command() == "pytest"
    assert resolve_pytest_executable() == str(fake_pytest)


# ---------------------------------------------------------------------------
# executable_test_command unit tests
# ---------------------------------------------------------------------------


def test_executable_command_unresolved_when_resolution_is_bare_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With bare ``pytest`` selected the canonical command stays as-is."""
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: candidate == ["pytest"])
    fake = shutil.which("pytest")
    monkeypatch.setattr(shutil, "which", lambda name, mode=0, path=None: fake if name == "pytest" else None)

    cmd = canonicalize_quiet_test_command("pytest")
    assert cmd == "pytest -q -r a --disable-warnings"
    assert executable_test_command(cmd) == cmd


def test_executable_command_resolves_bare_pytest_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare pytest prefix is replaced by the resolved executable."""
    local_pytest = _script(tmp_path / ".local" / "bin" / "pytest", "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(shutil, "which", lambda name, mode=0, path=None: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        tr, "_candidate_runs_pytest", lambda candidate: candidate == [str(local_pytest)]
    )

    resolved = resolve_pytest_command()
    assert resolved == str(local_pytest)

    cmd = "pytest -q -r a --disable-warnings"
    executable = executable_test_command(cmd)
    assert executable.startswith(str(local_pytest))
    assert "-q -r a --disable-warnings" in executable


def test_executable_command_rewrites_python_m_pytest_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``python3 -m pytest`` forms run under the resolved interpreter
    (the runner's interpreter), not the PATH ``python3``."""
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: True)

    cmd = canonicalize_quiet_test_command("python3 -m pytest -k foo")
    assert cmd.startswith("python3 -m pytest")
    rewritten = executable_test_command(cmd)
    assert rewritten == f"{sys.executable} -m pytest" + cmd[len("python3 -m pytest") :]
    # Idempotent: resolving an already-resolved command changes nothing.
    assert executable_test_command(rewritten) == rewritten


def test_executable_command_leaves_non_pytest_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-pytest commands are never rewritten."""
    assert executable_test_command("npm --silent test") == "npm --silent test"
    assert executable_test_command("node --test tests/node/*.mjs") == "node --test tests/node/*.mjs"


# ---------------------------------------------------------------------------
# Default runner: direct run_cached consumers (audit F3, implement gate)
# ---------------------------------------------------------------------------


def test_default_runner_spawns_resolved_pytest_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_cached``'s default runner (used by the audit F3 auto-run) spawns
    the resolved interpreter instead of a bare PATH ``pytest``."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tc.subprocess, "run", fake_run)
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: True)

    tc._default_runner("pytest -q -r a --disable-warnings", "/tmp", 10)

    assert captured["cmd"][:3] == [sys.executable, "-m", "pytest"]
    assert captured["cmd"][3:] == ["-q", "-r", "a", "--disable-warnings"]


# ---------------------------------------------------------------------------
# Cache metadata: the interpreter actually used is recorded (AC2)
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

    # Isolate the semaphore namespace for the in-process nested run in
    # ``test_run_tests_spawns_runner_interpreter_when_pytest_missing``: this
    # suite's outer run already holds a host-wide "test" slot, so the nested
    # runner must not contend for it (SA-0MUY6U05F008RG86). The directory is a
    # sibling of ``repo`` so it never counts as a live-repo mutation.
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(rt.nested_semaphore_dir(tmp_path)))

    monkeypatch.setattr(rt, "detect_project_root", lambda: repo)
    return repo


def test_cache_entry_records_pytest_interpreter(
    cache_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor's interpreter is stored in metadata.json and read back on
    a cache hit, so the choice is auditable."""
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: True)
    command = "pytest -q -r a --disable-warnings"

    def fake_runner(cmd, cwd, timeout):
        return SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")

    run = tc.run_cached(command, cwd=cache_repo, runner=fake_runner, force=True)
    assert run["interpreter"] == sys.executable

    metas = [
        json.loads(path.read_text())
        for path in Path(tc.cache_dir(cache_repo)).rglob("metadata.json")
    ]
    assert any(meta.get("interpreter") == sys.executable for meta in metas)

    def must_not_spawn(cmd, cwd, timeout):  # pragma: no cover - cache hit path
        raise AssertionError("cache hit must not execute the suite")

    hit = tc.run_cached(command, cwd=cache_repo, runner=must_not_spawn)
    assert hit["cached"] is True
    assert hit["interpreter"] == sys.executable


def test_non_pytest_cache_entry_records_no_interpreter(
    cache_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-pytest commands record an empty interpreter label."""
    monkeypatch.setattr(tr, "_candidate_runs_pytest", lambda candidate: True)

    def fake_runner(cmd, cwd, timeout):
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    run = tc.run_cached("npm --silent test", cwd=cache_repo, runner=fake_runner, force=True)
    assert run["interpreter"] == ""


# ---------------------------------------------------------------------------
# Integration: run_tests.py executes the resolved pytest
# ---------------------------------------------------------------------------


def test_pytest_command_stays_canonical_when_pytest_missing(
    cache_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pytest_command() keeps the canonical form even when the executable is
    missing — the cache key and cross-consumer contract stay stable."""
    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path)
    )
    monkeypatch.delenv("HOME", raising=False)

    assert rt.pytest_command() == "pytest -q -r a --disable-warnings"


def test_run_tests_spawns_runner_interpreter_when_pytest_missing(
    cache_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """run_tests.py spawns the runner's interpreter (``sys.executable -m
    pytest``) when pytest is not on PATH, and reports that interpreter in the
    JSON run output (AC1/AC2)."""
    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path)
    )

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(rt, "_run_cmd", fake_run)

    code = rt.main(["--suite", "pytest", "--json", "--no-cache"])
    assert code == 0
    assert len(captured) == 1
    assert captured[0][:3] == [sys.executable, "-m", "pytest"]
    assert captured[0][3:] == ["-q", "-r", "a", "--disable-warnings"]

    payload = json.loads(capsys.readouterr().out)
    assert payload["suites"]["pytest"]["interpreter"] == sys.executable


def test_run_tests_spawns_local_bin_pytest_when_earlier_candidates_fail(
    cache_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained ~/.local/bin fallback is still spawned end-to-end when no
    earlier candidate can run pytest (AC3c)."""
    local_bin = cache_repo / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    fake_pytest = _script(local_bin / "pytest", "#!/bin/sh\nexit 0\n")

    original_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, mode=0, path=None: _which_pytest_missing(original_which, name, mode, path)
    )
    monkeypatch.setenv("HOME", str(cache_repo))
    monkeypatch.setattr(
        tr, "_candidate_runs_pytest", lambda candidate: candidate == [str(fake_pytest)]
    )

    captured: list[str] = []

    def fake_run(cmd, **kwargs):
        captured.append(" ".join(cmd))
        return SimpleNamespace(returncode=0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(rt, "_run_cmd", fake_run)

    code = rt.main(["--suite", "pytest", "--json", "--no-cache"])
    assert code == 0
    assert captured[0].startswith(str(fake_pytest))
    assert "-q -r a --disable-warnings" in captured[0]


def test_foreign_pytest_on_path_is_skipped_end_to_end(tmp_path: Path) -> None:
    """AC2/AC4: an end-to-end ``run_tests.py`` run whose PATH holds a pytest
    bound to an interpreter that cannot run pytest.

    Reproduces the host failure mode that motivated this change
    (SA-0MVK154IE007OS3D) without needing that machine: the foreign PATH
    ``pytest`` is probed and skipped, the suite executes under the runner's
    interpreter (``sys.executable -m pytest``), the JSON result names that
    interpreter, the canonical command form is unchanged, and the foreign
    shim never executes the suite (its probe ``--version`` call fails and it
    is never selected for the real run).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "rt-test@example.com")
    git(repo, "config", "user.name", "RT Test")
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n")
    (repo / "pytest.ini").write_text("[pytest]\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_green.py").write_text(
        "def test_green():\n    assert True\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    # A PATH ``pytest`` whose shebang points at an interpreter that cannot
    # run pytest (the RossGameDev `~/.local/bin/pytest` failure mode). The
    # resolution probe (``--version``) fails; any other invocation records a
    # marker so the test can prove the shim was never used to run the suite.
    marker = repo / "foreign-pytest-executed"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    shim = fakebin / "pytest"
    shim.write_text(
        "#!/bin/sh\n"
        '# the availability probe must fail: this "pytest" cannot run pytest\n'
        'case " $* " in *" --version "*) exit 4 ;; esac\n'
        f"touch {marker}\n"
        "exit 4\n"
    )
    shim.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
    # The nested runner must not queue behind this suite's host-wide slot.
    env["PI_SEMAPHORE_DIR"] = str(rt.nested_semaphore_dir(tmp_path))
    # Keep the throwaway repo byte-identical across the run so the live-repo
    # guard sees no mutation (no .pytest_cache, no __pycache__ bytecode).
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"

    proc = subprocess.run(
        [
            sys.executable,
            str(Path(rt.__file__).resolve()),
            "--project-root",
            str(repo),
            "--suite",
            "pytest",
            "--type",
            "full",
            "--json",
            "--no-cache",
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    result = payload["suites"]["pytest"]
    assert result["success"] is True, payload
    # The interpreter actually used is the runner's, and it is named in the
    # run output (auditability, AC2) — never the foreign PATH ``pytest``.
    assert result["interpreter"] == sys.executable, result
    # The canonical command form (the cache key) is unchanged; only the
    # executable prefix was rewritten at run time.
    assert result["command"] == "pytest -q -r a --disable-warnings"
    assert not marker.exists(), "the foreign PATH pytest executed the suite"
    # The canonical command form is reported as-is, so the cache key and
    # cross-consumer contract stay stable (only the prefix was rewritten).
    assert result["scope"] == "full"
