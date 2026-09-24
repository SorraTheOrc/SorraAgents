"""Integration tests: test skill + shared concurrency semaphore.

Covers SA-0MTG5U75A001F1RG (extend the test skill to use the shared
concurrency primitive):

- AC1: `run_tests.py` bounds concurrent test runs via the shared "test"
  semaphore — a saturated slot makes an ACTUAL run wait (bounded, not
  fail-fast) and report a clear failure notice instead of crashing.
- AC2: test runs use the "test" semaphore namespace, independent of the
  "audit" namespace (holding the audit slot never blocks test runs).
- AC3: `TEST_MAX_CONCURRENCY` env var configures the ceiling; values are
  clamped/validated like the audit runner's `AUDIT_MAX_CONCURRENCY`.
- AC4: cache hits are served WITHOUT acquiring a slot — the semaphore only
  gates actual executions, so cache read/write/dedup/TTL are unchanged.
- AC5: concurrent test-style runs are bounded cross-process (real
  subprocesses through the real `_test_concurrency_slot` context manager).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap sys.path — mirror test_run_tests_scope.py so `import run_tests`
# works regardless of collection order (the stdlib `test` package would
# otherwise shadow <skills>/test when only the skills root is on sys.path).
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"  # <skills>/test/scripts
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

from run_tests import (
    TEST_LOCK_TIMEOUT_DEFAULT,
    TEST_LOCK_TIMEOUT_ENV,
    TEST_MAX_CONCURRENCY_DEFAULT,
    TEST_MAX_CONCURRENCY_ENV,
    TEST_SEMAPHORE_NAME,
    TestConcurrencyTimeout,
    _cached_runner,
    _test_lock_timeout,
    _test_semaphore_max_workers,
    run_suite,
)

pytest.importorskip("shared.process_semaphore")

from shared.process_semaphore import ENV_LOCK_DIR, Semaphore


@pytest.fixture(autouse=True)
def _isolate_semaphore_dir(tmp_path, monkeypatch):
    """Isolate the shared lock dir and clear test/audit env per test."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.delenv(TEST_MAX_CONCURRENCY_ENV, raising=False)
    monkeypatch.delenv(TEST_LOCK_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv("AUDIT_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("AUDIT_LOCK_TIMEOUT", raising=False)


# ---------------------------------------------------------------------------
# Cross-process worker shims (real _test_concurrency_slot in subprocesses)
# ---------------------------------------------------------------------------

WORKER_TEMPLATE = r"""
import json
import os
import sys
import time

sys.path.insert(0, os.environ["RUNNER_DIR"])

from run_tests import _test_concurrency_slot  # noqa: E402

hold = float(os.environ.get("SEM_HOLD", "0.3"))
result = {"acquired": None, "released": None, "error": None}
try:
    with _test_concurrency_slot():
        result["acquired"] = time.time()
        print(json.dumps(result), file=sys.stderr, flush=True)
        time.sleep(hold)
        result["released"] = time.time()
except Exception as exc:  # noqa: BLE001
    result["error"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(result))
sys.exit(0 if result["acquired"] is not None else 1)
"""


def _run_workers(n, hold=0.3, timeout=30):
    """Launch *n* concurrent worker subprocesses sharing the "test" slot.

    Each worker goes through the real ``_test_concurrency_slot`` context
    manager (the exact path run_tests.py uses to bound executions) with
    ``TEST_MAX_CONCURRENCY`` inherited from the test environment.
    """
    procs = []
    for _ in range(n):
        env = dict(os.environ)
        env.update(
            {
                "RUNNER_DIR": str(_RUNNER_DIR),
                "SEM_HOLD": str(hold),
            }
        )
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", WORKER_TEMPLATE],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
    results = []
    for p in procs:
        out, err = p.communicate(timeout=timeout + 30)
        try:
            results.append(json.loads(out.strip()))
        except json.JSONDecodeError:
            results.append(
                {
                    "acquired": None,
                    "released": None,
                    "error": f"bad output: {out} {err}",
                }
            )
    return results


def _max_concurrent(results):
    """Compute the maximum number of overlapping slot holdings."""
    intervals = [
        (r["acquired"], r["released"])
        for r in results
        if r.get("acquired") is not None and r.get("released") is not None
    ]
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], -e[1]))
    active = 0
    max_active = 0
    for _ts, delta in events:
        active += delta
        max_active = max(max_active, active)
    return max_active


# ---------------------------------------------------------------------------
# AC3: env resolution for the test ceiling and lock timeout
# ---------------------------------------------------------------------------


def test_default_ceiling():
    """Without TEST_MAX_CONCURRENCY, the ceiling is the documented default."""
    assert _test_semaphore_max_workers() == TEST_MAX_CONCURRENCY_DEFAULT


def test_ceiling_from_env(monkeypatch):
    """TEST_MAX_CONCURRENCY env drives the ceiling; invalid/zero are safe."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")
    assert _test_semaphore_max_workers() == 1
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "0")
    assert _test_semaphore_max_workers() == 1  # clamped to >= 1
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "bogus")
    assert _test_semaphore_max_workers() == TEST_MAX_CONCURRENCY_DEFAULT


def test_lock_timeout_resolution(monkeypatch):
    """TEST_LOCK_TIMEOUT env drives the bounded wait; invalid falls back."""
    assert _test_lock_timeout() == TEST_LOCK_TIMEOUT_DEFAULT
    monkeypatch.setenv(TEST_LOCK_TIMEOUT_ENV, "0.25")
    assert _test_lock_timeout() == 0.25
    monkeypatch.setenv(TEST_LOCK_TIMEOUT_ENV, "bogus")
    assert _test_lock_timeout() == TEST_LOCK_TIMEOUT_DEFAULT


# ---------------------------------------------------------------------------
# AC1: run_tests.py execution is gated by the "test" semaphore
# ---------------------------------------------------------------------------


def test_run_suite_waits_bounded_on_saturated_slot(monkeypatch, tmp_path):
    """With the "test" slot held, an ACTUAL run waits (bounded, no
    fail-fast) and reports a clear concurrency notice instead of
    executing or crashing."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")
    monkeypatch.setenv(TEST_LOCK_TIMEOUT_ENV, "0.3")

    holder = Semaphore(TEST_SEMAPHORE_NAME, max_workers=1, timeout=5)
    holder.acquire()
    try:
        start = time.monotonic()
        result = run_suite("pytest", cwd=tmp_path, use_cache=False, force=True)
        elapsed = time.monotonic() - start
    finally:
        holder.release()

    assert result["success"] is False
    assert "concurrency slot" in result["notice"], result
    # Bounded wait (>= the 0.3s bound), not fail-fast and not a hang.
    assert elapsed >= 0.2, f"did not wait for the bound: {elapsed:.2f}s"
    assert elapsed < 5.0, f"wait exceeded the bound: {elapsed:.2f}s"


def test_cached_runner_waits_bounded_on_saturated_slot(monkeypatch, tmp_path):
    """The cache-miss runner (_cached_runner) is gated: with the slot held
    it raises TestConcurrencyTimeout after the bound (AC1 cache path)."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")
    monkeypatch.setenv(TEST_LOCK_TIMEOUT_ENV, "0.2")

    holder = Semaphore(TEST_SEMAPHORE_NAME, max_workers=1, timeout=5)
    holder.acquire()
    try:
        start = time.monotonic()
        with pytest.raises(TestConcurrencyTimeout):
            _cached_runner("pytest -q -r a", str(tmp_path), 60)
        elapsed = time.monotonic() - start
    finally:
        holder.release()

    assert elapsed >= 0.15, f"did not wait for the bound: {elapsed:.2f}s"
    assert elapsed < 5.0, f"wait exceeded the bound: {elapsed:.2f}s"


def test_run_suite_proceeds_when_slot_free(monkeypatch, tmp_path):
    """With a free slot the run executes normally (mocked command)."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")

    with mock.patch(
        "run_tests._run_cmd",
        return_value=SimpleNamespace(stdout="passed", stderr="", returncode=0),
    ):
        result = run_suite("pytest", cwd=tmp_path, use_cache=False, force=True)

    assert result["success"] is True
    assert result["notice"] == ""


# ---------------------------------------------------------------------------
# AC4: cache hits never acquire a slot
# ---------------------------------------------------------------------------


def test_cached_hit_served_without_slot(monkeypatch, tmp_path):
    """A cached result is served immediately even while the "test" slot is
    held — the semaphore gates executions only, never cache lookups."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")
    monkeypatch.setenv(TEST_LOCK_TIMEOUT_ENV, "0.2")

    holder = Semaphore(TEST_SEMAPHORE_NAME, max_workers=1, timeout=5)
    holder.acquire()
    try:
        hit = {
            "stdout": "passed",
            "stderr": "",
            "exit_code": 0,
            "cached": True,
            "scope": "full",
        }
        with mock.patch("run_tests.run_cached", return_value=hit):
            start = time.monotonic()
            result = run_suite("pytest", cwd=tmp_path, use_cache=True, force=True)
            elapsed = time.monotonic() - start
    finally:
        holder.release()

    assert result["success"] is True
    assert result["cached"] is True
    # Well under the 0.2s bound: served without acquiring the held slot.
    assert elapsed < 0.15, f"cache hit blocked on the slot: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# AC2: "test" namespace is independent of the "audit" namespace
# ---------------------------------------------------------------------------


def test_test_namespace_independent_of_audit(monkeypatch, tmp_path):
    """Holding the "audit" semaphore never blocks a "test" run (AC2)."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")

    audit_holder = Semaphore("audit", max_workers=1, timeout=5)
    audit_holder.acquire()
    try:
        with mock.patch(
            "run_tests._run_cmd",
            return_value=SimpleNamespace(stdout="passed", stderr="", returncode=0),
        ):
            start = time.monotonic()
            result = run_suite("pytest", cwd=tmp_path, use_cache=False, force=True)
            elapsed = time.monotonic() - start
    finally:
        audit_holder.release()

    assert result["success"] is True, result
    assert elapsed < 1.0, f"audit holder blocked test run: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# AC5: cross-process concurrency is bounded
# ---------------------------------------------------------------------------


def test_concurrent_runs_bounded_cross_process(monkeypatch):
    """N concurrent test-style runs (real subprocesses, real
    _test_concurrency_slot) never exceed the TEST_MAX_CONCURRENCY ceiling."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "2")
    results = _run_workers(6, hold=0.3, timeout=30)
    acquired = [r for r in results if r.get("acquired") is not None]
    assert len(acquired) == 6, f"not all workers acquired: {results}"
    assert _max_concurrent(results) <= 2, f"ceiling exceeded: {results}"
    assert all(r.get("error") is None for r in results), results


def test_single_ceiling_serializes_cross_process(monkeypatch):
    """TEST_MAX_CONCURRENCY=1 serializes concurrent runs (max 1 active)."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")
    results = _run_workers(3, hold=0.2, timeout=30)
    acquired = [r for r in results if r.get("acquired") is not None]
    assert len(acquired) == 3, f"starvation/deadlock: {results}"
    assert _max_concurrent(results) == 1, f"not serialized: {results}"
