"""Disk-backed TMPDIR wiring in the test runner (SA-0MW3ENF32006BGN1).

The test runner passes a disk-backed ``TMPDIR`` to every suite command it
spawns, resolving it once per process through the shared F1 helper
(``skill/shared/test_tmpdir.py``, SA-0MW3EN72O005BMIP) so a suite subprocess
never exhausts a tiny tmpfs ``/tmp`` inode budget (SA-0MW2QED9M005FWOK).

Assertions are behavioural and env-only:

* ``_suite_command_env()`` — the single child-env construction point for
  suite commands — carries the resolved TMPDIR; read back through a real
  child process via :func:`_run_cmd` (regression: the env handed to the
  spawned command IS the value the resolver returned).
* The explicit-override path (``PI_TEST_TMPDIR``) is exercised with the real
  F1 helper so the wiring is not a self-referential stub.
* The fallback path (no usable disk-backed candidate) and the fail-open
  contract (a raising resolver) never change the env's viability — the
  command still runs with a TMPDIR.
* Command strings / cache keys are byte-identical: the change is env-only
  (AC4).
* Logging uses INFO for a chosen disk-backed TMPDIR and WARNING for an
  unusable candidate / resolution failure (AC5).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap sys.path — mirror test_vitest_worker_cap.py so `import
# run_tests` works regardless of collection order.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

import run_tests
from run_tests import (
    _run_cmd,
    _suite_command_env,
    _test_vitest_max_workers,
    full_suite_commands,
    pytest_command,
    run_all,
)
from shared import test_tmpdir

_OVERRIDE_ENV = test_tmpdir.TEST_TMPDIR_ENV


@pytest.fixture(autouse=True)
def _isolated_resolution(monkeypatch):
    """Reset the module-level TMPDIR cache and scrub ambient overrides.

    The resolver cache is module-wide (resolved once per process) — each
    test must start from an unresolved state and from a clean override env,
    or a previous test's resolution leaks into the assertion.
    """
    for name in (
        test_tmpdir.TEST_TMPDIR_ENV,
        test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV,
        test_tmpdir.TEST_ROOT_MAX_AGE_ENV,
        test_tmpdir.TEST_ROOT_KEEP_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(run_tests, "_TEST_TMPDIR_CACHE", None)


def _child_env_probe(tmp_path: Path) -> dict[str, str]:
    """Read the child TMPDIR through the real suite command execution path."""
    script = (
        "import json, os;"
        "print(json.dumps({'TMPDIR': os.environ.get('TMPDIR'),"
        "'VITEST_MAX_WORKERS': os.environ.get('VITEST_MAX_WORKERS')}))"
    )
    proc = _run_cmd([sys.executable, "-c", script], cwd=tmp_path, timeout=60)
    assert proc.returncode == 0, proc.stderr
    import json

    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# AC1 — the spawned command's env carries the resolved TMPDIR
# ---------------------------------------------------------------------------


def test_explicit_override_reaches_the_child_env(tmp_path, monkeypatch):
    """PI_TEST_TMPDIR (an explicit operator override) flows to the child.

    Exercises the real F1 resolver (the override path creates the dir and
    wins) so the assertion is not a self-referential stub of the runner's own
    resolution call.
    """
    target = tmp_path / "override" / "nested"
    monkeypatch.setenv(_OVERRIDE_ENV, str(target))

    observed = _child_env_probe(tmp_path)

    assert observed["TMPDIR"] == str(target)
    assert target.is_dir(), "the resolver must create the chosen dir"


def test_suite_command_env_carries_the_resolved_tmpdir(tmp_path, monkeypatch):
    """The env built for every suite command carries the resolved TMPDIR."""
    exclusions_root = tmp_path / "disk-root"
    exclusions_root.mkdir()
    monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV, str(exclusions_root))
    monkeypatch.setattr(test_tmpdir, "_is_tmpfs", lambda _path: False)

    resolved = str(test_tmpdir.resolve_test_tmpdir()[0])
    ambient = dict(os.environ)
    env = _suite_command_env()

    assert env["TMPDIR"] == resolved
    # Env-only: the runner's own process env is never mutated by resolution.
    assert os.environ == ambient


# ---------------------------------------------------------------------------
# Fallback path (no usable disk-backed candidate)
# ---------------------------------------------------------------------------


def test_fallback_when_no_disk_backed_candidate(tmp_path, monkeypatch, caplog):
    """A resolver fallback is accepted and surfaced at WARNING (AC5)."""
    fallback = tmp_path / "portable-fallback"
    fallback.mkdir()
    monkeypatch.setattr(
        run_tests,
        "resolve_test_tmpdir",
        lambda: (fallback, f"fallback tempfile.gettempdir()={fallback}"),
    )

    env = _suite_command_env()

    assert env["TMPDIR"] == str(fallback)
    warning = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("unusable" in r.getMessage() for r in warning), caplog.text


# ---------------------------------------------------------------------------
# AC3 — fail-open: a raising resolver never breaks the run
# ---------------------------------------------------------------------------


def test_raising_resolver_is_fail_open(tmp_path, monkeypatch, caplog):
    """A resolver exception is logged; the command still runs with a TMPDIR.

    Proven through the real execution path (:func:`_run_cmd`): the child
    exit status is unchanged (0) and the env remains viable.
    """
    def _boom():
        raise RuntimeError("forced resolver failure")

    monkeypatch.setattr(run_tests, "resolve_test_tmpdir", _boom)
    monkeypatch.delenv("TMPDIR", raising=False)

    observed = _child_env_probe(tmp_path)

    assert observed["TMPDIR"], "a TMPDIR must still be present (portable fallback)"
    warning = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("resolution failed" in r.getMessage() for r in warning), caplog.text


def test_raising_resolver_leaves_scope_and_commands_untouched(monkeypatch, caplog):
    """Fail-open is env-only: resolution failure changes nothing else."""
    def _boom():
        raise RuntimeError("forced resolver failure")

    monkeypatch.setattr(run_tests, "resolve_test_tmpdir", _boom)
    before = full_suite_commands(REPO_ROOT)

    env = _suite_command_env()  # must not raise

    assert env["TMPDIR"]
    assert "VITEST_MAX_WORKERS" in env
    assert env["VITEST_MAX_WORKERS"] == str(_test_vitest_max_workers())
    assert full_suite_commands(REPO_ROOT) == before


def test_raising_resolver_and_prune_keep_run_and_scope_unchanged(
    monkeypatch, caplog
):
    """AC3 combined: resolver AND prune raise — run/scope/cache unchanged.

    Both fail-open contracts fire in one run (through the real ``run_all``
    with a stubbed ``run_suite``): the exit status stays green, the scope
    resolution is unchanged and no cache entry is written or consulted —
    only warnings are logged.
    """
    def _boom(*_a, **_k):
        raise RuntimeError("forced fail-open failure")

    def _fake_suite(*_a, **_k):
        return {
            "success": True,
            "command": "pytest -q -r a --disable-warnings",
            "failures": [],
            "cached": False,
            "scope": "full",
            "type": "full",
            "exit_code": 0,
            "elapsed_seconds": 0.1,
        }

    monkeypatch.setattr(run_tests, "resolve_test_tmpdir", _boom)
    monkeypatch.setattr(run_tests, "prune_stale_test_roots", _boom)
    monkeypatch.setattr(run_tests, "run_suite", _fake_suite)

    # The child-env builder is the resolver's fail-open call site; a raising
    # resolver must surface as a warning, never an exception.
    env = _suite_command_env()  # must not raise
    assert env["TMPDIR"]

    with caplog.at_level(logging.WARNING):
        result = run_all(
            ("all",),
            cwd=REPO_ROOT,
            use_cache=False,
            no_cache=True,
            scope="full",
        )

    assert result["success"] is True
    assert result["scope"] == "full"
    assert result["cache_entries"] == []
    warning = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("resolution failed" in r.getMessage() for r in warning), caplog.text
    assert any("prune failed" in r.getMessage() for r in warning), caplog.text


# ---------------------------------------------------------------------------
# AC4 — env-only: command strings and cache keys are byte-identical
# ---------------------------------------------------------------------------


def test_command_strings_and_cache_key_are_env_only(monkeypatch):
    """The TMPDIR/VITEST work never rewrites a command string.

    The canonical pytest command and the full-suite command list are
    byte-identical regardless of TMPDIR resolution state — the per-repo
    cache key (derived from the command, git state and type) is therefore
    unchanged (AC4).
    """
    canonical = "pytest -q -r a --disable-warnings"
    assert pytest_command() == canonical

    before = full_suite_commands(REPO_ROOT)
    monkeypatch.setattr(run_tests, "resolve_test_tmpdir", lambda: (Path("/x"), "x"))
    _suite_command_env()
    after = full_suite_commands(REPO_ROOT)

    assert after == before
    assert all("TMPDIR" not in cmd for cmd in after)


def test_env_helper_differs_only_by_tmpdir_and_worker_cap(monkeypatch):
    """The child env equals the scrub base plus exactly TMPDIR and the cap."""
    from test_runner import subprocess_env_with_scrubbed_overrides

    target = Path("/explicit/override")
    monkeypatch.setattr(
        run_tests,
        "resolve_test_tmpdir",
        lambda: (target, f"explicit {_OVERRIDE_ENV} override"),
    )
    # Hermetic: neither the ambient TMPDIR nor an ambient worker cap may leak
    # into the diff (the parent suite may export either).
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("VITEST_MAX_WORKERS", raising=False)

    base = subprocess_env_with_scrubbed_overrides()
    env = _suite_command_env()

    base_keys = set(base)
    env_keys = set(env)
    # Everything in the scrub base is preserved byte-for-byte.
    assert base_keys <= env_keys
    assert {k: env[k] for k in base_keys} == base
    # Exactly two additions: the worker cap and TMPDIR.
    assert env_keys - base_keys == {"TMPDIR", run_tests.TEST_VITEST_MAX_WORKERS_ENV}
    assert env["TMPDIR"] == str(target)
