"""Fast-lane semaphore tests for the test runner (SA-0MVWZQ269009SJXV).

Why these exist: with ~44 concurrent agent sessions the host-wide ``"test"``
pool (2 slots) queued 1-3 s partial runs behind 7-minute full suites — three
observed calls took 7.5, 14.3 and 18.2 minutes for pytest bodies of 0.65,
2.88 and 1.83 seconds. These tests pin the contract that makes that
impossible:

- AC1: a PARTIAL run (``--scope changed``, or a ``--type`` other than
  ``full``) contends in the independent ``"test-fast"`` namespace, and a
  saturated full-suite pool never delays it.
- AC2: the calibrated full-suite default is 6 and the fast ceiling is 4,
  both env-overridable with the same clamping/validation as before.
- AC3: ``slot_wait_seconds`` (queue time) is reported separately from
  ``elapsed_seconds`` (execution time).
- AC4: every suite result and aggregate states the lane it used.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap sys.path — mirror the sibling semaphore tests so `import run_tests`
# works regardless of collection order.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

from run_tests import (
    TEST_FAST_SEMAPHORE_NAME,
    TEST_MAX_CONCURRENCY_DEFAULT,
    TEST_MAX_CONCURRENCY_ENV,
    TEST_MAX_CONCURRENCY_FAST_DEFAULT,
    TEST_MAX_CONCURRENCY_FAST_ENV,
    TEST_SEMAPHORE_NAME,
    TEST_SLOT_LANE_FAST,
    TEST_SLOT_LANE_FULL,
    TestConcurrencyTimeout,
    _lane_semaphore_config,
    _test_concurrency_slot,
    _test_fast_semaphore_max_workers,
    _test_semaphore_max_workers,
    run_all,
    run_suite,
    slot_lane_for,
)

pytest.importorskip("shared.process_semaphore")

from shared.process_semaphore import ENV_LOCK_DIR, Semaphore

#: A command that executes in milliseconds and never touches the project.
TRIVIAL_COMMAND = f"{sys.executable} -c pass"


@pytest.fixture(autouse=True)
def _isolated_pools(tmp_path, monkeypatch):
    """Isolate both semaphore namespaces and clear every ceiling override."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    for name in (
        TEST_MAX_CONCURRENCY_ENV,
        TEST_MAX_CONCURRENCY_FAST_ENV,
        "TEST_LOCK_TIMEOUT",
        "TEST_LOCK_JITTER",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# AC1: lane selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "test_type", "expected"),
    [
        ("full", "full", TEST_SLOT_LANE_FULL),
        ("changed", "full", TEST_SLOT_LANE_FAST),
        ("full", "unit", TEST_SLOT_LANE_FAST),
        ("full", "smoke", TEST_SLOT_LANE_FAST),
        ("full", "group:proxy", TEST_SLOT_LANE_FAST),
        ("changed", "unit", TEST_SLOT_LANE_FAST),
    ],
)
def test_slot_lane_for_partial_runs_is_fast(scope, test_type, expected):
    """Only a whole-suite ``full``/``full`` run uses the full-suite pool."""
    assert slot_lane_for(scope, test_type) == expected


def test_lane_config_maps_to_independent_namespaces():
    """The two lanes resolve to different namespaces, each with its ceiling."""
    assert _lane_semaphore_config(TEST_SLOT_LANE_FULL) == (
        TEST_SLOT_LANE_FULL,
        TEST_SEMAPHORE_NAME,
        TEST_MAX_CONCURRENCY_DEFAULT,
    )
    assert _lane_semaphore_config(TEST_SLOT_LANE_FAST) == (
        TEST_SLOT_LANE_FAST,
        TEST_FAST_SEMAPHORE_NAME,
        TEST_MAX_CONCURRENCY_FAST_DEFAULT,
    )
    # An unrecognised lane must degrade to the conservative full pool, never
    # to an unbounded one.
    assert _lane_semaphore_config("nonsense") == (
        TEST_SLOT_LANE_FULL,
        TEST_SEMAPHORE_NAME,
        TEST_MAX_CONCURRENCY_DEFAULT,
    )


def test_fast_ceiling_from_env_and_clamping(monkeypatch):
    """TEST_MAX_CONCURRENCY_FAST overrides, clamps and validates like the full one."""
    assert _test_fast_semaphore_max_workers() == TEST_MAX_CONCURRENCY_FAST_DEFAULT
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_FAST_ENV, "7")
    assert _test_fast_semaphore_max_workers() == 7
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_FAST_ENV, "0")
    assert _test_fast_semaphore_max_workers() == 1
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_FAST_ENV, "bogus")
    assert _test_fast_semaphore_max_workers() == TEST_MAX_CONCURRENCY_FAST_DEFAULT
    # The full-suite ceiling is unaffected by the fast-lane env var.
    assert _test_semaphore_max_workers() == TEST_MAX_CONCURRENCY_DEFAULT


def test_fast_lane_ignores_a_saturated_full_pool(monkeypatch):
    """A held full-suite slot never delays a fast-lane acquire.

    This is the regression pin for the reported starvation: a 1-3 s partial run
    must not queue behind a full suite.
    """
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")
    monkeypatch.setenv("TEST_LOCK_TIMEOUT", "0.2")
    full_pool = Semaphore(TEST_SEMAPHORE_NAME, max_workers=1, timeout=0.2)
    with full_pool:
        # Control: the full lane really is saturated (else the assertion below
        # would pass for the wrong reason).
        with (
            pytest.raises(TestConcurrencyTimeout),
            _test_concurrency_slot(TEST_SLOT_LANE_FULL),
        ):
            pass

        started = time.monotonic()
        with _test_concurrency_slot(TEST_SLOT_LANE_FAST):
            elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"fast lane waited {elapsed:.2f}s behind the full pool"


def test_fast_lane_serialises_at_its_own_ceiling(monkeypatch):
    """The fast pool has its own bound: ceiling 1 blocks a second fast acquire."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_FAST_ENV, "1")
    monkeypatch.setenv("TEST_LOCK_TIMEOUT", "0.2")
    # Nested on purpose: the OUTER acquisition saturates the pool so the inner
    # one must time out (SIM117 would invert that meaning).
    with _test_concurrency_slot(TEST_SLOT_LANE_FAST):  # noqa: SIM117
        with (
            pytest.raises(TestConcurrencyTimeout) as excinfo,
            _test_concurrency_slot(TEST_SLOT_LANE_FAST),
        ):
            pass
    message = str(excinfo.value)
    assert "lane=fast" in message, message
    assert TEST_MAX_CONCURRENCY_FAST_ENV in message, message


def test_full_lane_timeout_names_the_full_lane(monkeypatch):
    """A saturated full pool reports the lane and env var in its notice."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")
    monkeypatch.setenv("TEST_LOCK_TIMEOUT", "0.2")
    # Nested on purpose: hold the only full-lane slot, then require a timeout.
    with _test_concurrency_slot(TEST_SLOT_LANE_FULL):  # noqa: SIM117
        with (
            pytest.raises(TestConcurrencyTimeout) as excinfo,
            _test_concurrency_slot(TEST_SLOT_LANE_FULL),
        ):
            pass
    message = str(excinfo.value)
    assert "lane=full" in message, message
    assert TEST_MAX_CONCURRENCY_ENV in message, message


# ---------------------------------------------------------------------------
# AC3/AC4: the lane and the wait are reported on the result
# ---------------------------------------------------------------------------


def _run_trivial_suite(tmp_path, **kwargs):
    return run_suite(
        "all",
        cwd=tmp_path,
        commands=[TRIVIAL_COMMAND],
        use_cache=False,
        **kwargs,
    )


def test_changed_scope_run_reports_the_fast_lane(tmp_path):
    """A changed-scope run reports lane=fast and never the full pool."""
    result = _run_trivial_suite(tmp_path, scope="changed")
    assert result["success"] is True, result
    assert result["slot_lane"] == TEST_SLOT_LANE_FAST
    assert result["slot_wait_seconds"] == pytest.approx(0.0, abs=0.5)


def test_full_scope_run_reports_the_full_lane(tmp_path):
    """A whole-suite run reports lane=full."""
    result = _run_trivial_suite(tmp_path, scope="full")
    assert result["success"] is True, result
    assert result["slot_lane"] == TEST_SLOT_LANE_FULL


def test_queue_wait_is_reported_separately_from_execution(tmp_path, monkeypatch):
    """A run that waits for a fast slot reports the wait, not a slow suite.

    The queue wait is measured while another holder occupies the single fast
    slot, and must appear in ``slot_wait_seconds`` while the command's own
    ``elapsed_seconds`` stays far below it.
    """
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_FAST_ENV, "1")
    held_seconds = 0.6
    holder_ready = threading.Event()
    holder_done = threading.Event()

    def _hold_fast_slot():
        with _test_concurrency_slot(TEST_SLOT_LANE_FAST):
            holder_ready.set()
            time.sleep(held_seconds)
        holder_done.set()

    holder = threading.Thread(target=_hold_fast_slot, daemon=True)
    holder.start()
    assert holder_ready.wait(5), "holder never acquired the fast slot"
    try:
        result = _run_trivial_suite(tmp_path, scope="changed")
    finally:
        holder_done.wait(5)
        holder.join(timeout=5)

    assert result["success"] is True, result
    assert result["slot_lane"] == TEST_SLOT_LANE_FAST
    assert result["slot_wait_seconds"] >= held_seconds * 0.8, result
    assert result["elapsed_seconds"] < result["slot_wait_seconds"], result


def test_run_all_aggregates_lane_and_slot_wait(tmp_path):
    """The aggregate names the lane(s) used and sums the queue wait."""
    result = run_all(
        ("all",),
        cwd=tmp_path,
        commands=[TRIVIAL_COMMAND],
        use_cache=False,
        scope="changed",
    )
    assert result["success"] is True, result
    assert result["slot_lanes"] == [TEST_SLOT_LANE_FAST]
    assert "slot_wait_seconds" in result


def test_slot_busy_result_names_the_lane(tmp_path, monkeypatch):
    """A starved run reports ``slot_busy`` with the lane in the notice."""
    monkeypatch.setenv(TEST_MAX_CONCURRENCY_ENV, "1")
    monkeypatch.setenv("TEST_LOCK_TIMEOUT", "0.2")
    # Hold the only full-lane slot for the whole run: the runner must report
    # slot_busy (with the lane named) instead of hanging or crashing.
    with _test_concurrency_slot(TEST_SLOT_LANE_FULL):
        result = _run_trivial_suite(tmp_path, scope="full")
    assert result["success"] is False
    assert result["notice_code"] == "slot_busy"
    assert "lane=full" in result["notice"], result["notice"]
