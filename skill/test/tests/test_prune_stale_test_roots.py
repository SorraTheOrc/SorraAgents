"""Stale pytest-root prune wiring in the test runner (SA-0MW3ENF32006BGN1).

The full-scope entry path (``run_all`` with ``scope="full"``) prunes stale
``pytest-of-<user>/pytest-*`` roots once before executing, delegating to the
shared F1 helper (``skill/shared/test_tmpdir.py``, SA-0MW3EN72O005BMIP). The
prune is:

* **scoped to full runs** — a changed-scope run (the agent's fast iteration
  loop) never prunes and never regresses command selection (AC2);
* **fail-open** — a raising prune helper is logged and swallowed; the run's
  exit status, cache entry and scope resolution are unchanged (AC3);
* **logged at INFO** for the removed count, DEBUG for a no-op, and WARNING
  for a failure (AC5).

The tests drive the real ``run_all`` (the prune lives inside it) with a
stubbed ``run_suite``, so no suite actually executes and nothing touches a
host-wide test slot or the real ``/tmp`` bases.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap sys.path — mirror test_guarded_run_all.py.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

import run_tests
from run_tests import run_all
from shared import test_tmpdir

_LOG_NAME = "skill.test.run_tests"


def _fake_suite_result(**overrides):
    base = {
        "success": True,
        "command": "pytest -q -r a --disable-warnings",
        "failures": [],
        "cached": False,
        "scope": "full",
        "type": "full",
        "exit_code": 0,
        "elapsed_seconds": 0.1,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _stub_suite_execution(monkeypatch):
    """Stub the per-suite execution so the tests never spawn a real command.

    ``run_all`` is the unit under test (the prune hook lives inside it);
    ``run_suite`` is stubbed to a fake green aggregate so no suite executes,
    no cache write happens and no host-wide test slot is acquired.
    """
    monkeypatch.setattr(
        run_tests, "run_suite", lambda *a, **k: _fake_suite_result()
    )


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """Isolate the prune config env from an operator override."""
    for name in (
        test_tmpdir.TEST_TMPDIR_ENV,
        test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV,
        test_tmpdir.TEST_ROOT_MAX_AGE_ENV,
        test_tmpdir.TEST_ROOT_KEEP_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _run(scope: str, **kwargs) -> dict:
    """Drive ``run_all`` with the requested scope; suites never execute."""
    return run_all(
        suites=("all",),
        cwd=REPO_ROOT,
        scope=scope,
        use_cache=False,
        no_cache=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AC2 — the prune runs for full scope only
# ---------------------------------------------------------------------------


def test_full_scope_invokes_the_prune(monkeypatch):
    """``scope="full"`` calls the shared prune helper once, before the run."""
    calls: list[str] = []

    def _recording_prune(*args, **kwargs):
        calls.append("prune")
        return 0, "noop"

    monkeypatch.setattr(run_tests, "prune_stale_test_roots", _recording_prune)

    result = _run("full")

    assert calls == ["prune"]
    assert result["success"] is True
    assert result["scope"] == "full"


def test_changed_scope_never_prunes_and_keeps_selection(monkeypatch):
    """A changed-scope run skips the prune and does not regress selection."""
    calls: list[str] = []

    def _recording_prune(*args, **kwargs):
        calls.append("prune")
        return 0, "noop"

    monkeypatch.setattr(run_tests, "prune_stale_test_roots", _recording_prune)

    result = _run("changed")

    assert calls == []
    # Command selection is untouched: the changed scope still reaches the
    # suite-resolution layer unchanged (a changed-file fallback to full is
    # resolved inside ``run_suite``, never here).
    assert result["scope"] == "changed"
    assert result["success"] is True


# ---------------------------------------------------------------------------
# AC3 — fail-open: a raising prune helper never changes the run
# ---------------------------------------------------------------------------


def test_raising_prune_is_fail_open(monkeypatch, caplog):
    """A prune exception is logged; the run proceeds with unchanged results."""

    def _boom(*args, **kwargs):
        raise RuntimeError("forced prune failure")

    monkeypatch.setattr(run_tests, "prune_stale_test_roots", _boom)
    with caplog.at_level(logging.WARNING, logger=_LOG_NAME):
        result = _run("full")

    assert result["success"] is True
    assert result["scope"] == "full"
    warning = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("prune failed" in r.getMessage() for r in warning), caplog.text


# ---------------------------------------------------------------------------
# AC5 — logging levels for the prune
# ---------------------------------------------------------------------------


def test_prune_logs_removed_count_at_info(monkeypatch, caplog):
    """A prune that removed roots logs the count and bases at INFO."""
    monkeypatch.setattr(
        run_tests,
        "prune_stale_test_roots",
        lambda *a, **k: (3, "base: removed=3 kept_recent=0 kept_young=0"),
    )
    with caplog.at_level(logging.INFO, logger=_LOG_NAME):
        _run("full")

    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("Pruned 3" in r.getMessage() for r in info), caplog.text


def test_prune_noop_logs_at_debug(monkeypatch, caplog):
    """A no-op prune (nothing removed) is DEBUG, not INFO noise."""
    monkeypatch.setattr(
        run_tests, "prune_stale_test_roots", lambda *a, **k: (0, "noop")
    )
    with caplog.at_level(logging.DEBUG, logger=_LOG_NAME):
        _run("full")

    info = [r for r in caplog.records if r.levelno == logging.INFO]
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert not any("Pruned" in r.getMessage() for r in info)
    assert any("No stale pytest roots" in r.getMessage() for r in debug), caplog.text


# ---------------------------------------------------------------------------
# End-to-end: the real prune helper removes a genuinely stale root
# ---------------------------------------------------------------------------


def _make_stale_root(base: Path, name: str, *, age_seconds: float) -> Path:
    root = base / name
    root.mkdir(parents=True)
    (root / "tmpfile").write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(root, (stamp, stamp))
    return root


def test_real_prune_removes_stale_root_through_full_scope(tmp_path, monkeypatch):
    """Full-scope wiring drives the REAL shared prune against a tmp base.

    A stale root (older than the age threshold, below the keep-recent bound)
    under an explicit TMPDIR is removed by the full-scope entry path — the
    prune hook is wired to the shared helper, not a stub (F1 parity,
    SA-0MW2QED9M005FWOK).

    The prune helper's no-arg form considers BOTH the resolved test TMPDIR
    and ``tempfile.gettempdir()`` (F1 ``_pytest_base_candidates``); both are
    sandboxed to ``tmp_path`` so the test never touches real ``/tmp`` roots.
    """
    import tempfile as real_tempfile

    from shared import test_tmpdir as td

    override = tmp_path / "pi-test-dir"
    override.mkdir()
    monkeypatch.setenv(td.TEST_TMPDIR_ENV, str(override))
    # Sandbox the second candidate base (``tempfile.gettempdir()``) too.
    monkeypatch.setattr(real_tempfile, "gettempdir", lambda: str(tmp_path))
    # Force the age threshold to zero so the fixture's stale root qualifies,
    # and keep zero newest roots so the fixture root is not protected. Both
    # thresholds apply only to the sandboxed bases above.
    monkeypatch.setenv(td.TEST_ROOT_MAX_AGE_ENV, "0")
    monkeypatch.setenv(td.TEST_ROOT_KEEP_ENV, "0")
    # No live pytest process references a tmp_path root (test isolation).
    monkeypatch.setattr(td, "_active_pytest_paths", list)

    base = override / td.PYTEST_BASE_TEMPLATE.format(user=td._current_user())
    base.mkdir(parents=True)
    stale = _make_stale_root(base, "pytest-1", age_seconds=7200)

    _run("full")

    assert not stale.exists(), "the full-scope prune must remove the stale root"
