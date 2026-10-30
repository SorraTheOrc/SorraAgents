"""Failure-notice and output diagnostics on suite results.

Covers WL-0MVGUT787000LNDG: when a suite fails WITHOUT structured test
failures, ``run_suite`` must carry a stable machine-readable ``notice_code``
(so ``implement.py`` can classify slot contention by code, never by string
matching the human-readable text) and a bounded ``stdout``/``stderr`` tail
(so an unparsed vitest/node failure reports its output). ``run_all``
aggregates both so ``guarded_run_all`` consumers see them at the top level.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
_REPO_ROOT = _SCRIPT_DIR.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import run_tests
from run_tests import (
    NOTICE_COMMAND_NOT_FOUND,
    NOTICE_SLOT_BUSY,
    NOTICE_SUITE_TIMEOUT,
    SLOT_BUSY_NOTICE_PREFIX,
    run_all,
    run_suite,
)

# Deterministic infrastructure-failure simulations shared with the finish-gate
# contract tests (SA-0MVU8XQTU006DP37).
from skill.shared.tests import gate_failure_fixtures as fx

_CMD = "pytest -q -r a --disable-warnings"

_NOISE = "\n".join(f"stderr-line-{i}" for i in range(2000))


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        f'{{"suiteCommands": ["{_CMD}"]}}', encoding="utf-8"
    )
    return repo


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 1):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestRunSuiteDiagnostics:
    def test_carries_bounded_output_and_no_notice(self, tmp_path) -> None:
        with mock.patch.object(
            run_tests, "_run_cmd", return_value=_fake_run(stdout=_NOISE, stderr=_NOISE)
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        # The output tail survives; the head is dropped and the field stays
        # bounded so JSON reports cannot balloon.
        assert "stderr-line-1999" in result["stderr"]
        assert "stderr-line-0" not in result["stderr"]
        assert len(result["stderr"]) < len(_NOISE)
        assert "stderr-line-1999" in result["stdout"]
        assert len(result["stdout"]) < len(_NOISE)
        assert result["notice"] == ""
        assert result["notice_code"] == ""

    def test_command_not_found_is_classified(self, tmp_path) -> None:
        def raise_not_found(*args, **kwargs):
            # errno + filename form so ``exc.filename == "vitest"``.
            raise FileNotFoundError(2, "No such file or directory", "vitest")

        with mock.patch.object(run_tests, "_run_cmd", side_effect=raise_not_found), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        assert result["notice_code"] == NOTICE_COMMAND_NOT_FOUND
        assert "vitest" in result["notice"]
        assert result["success"] is False

    def test_suite_timeout_is_classified(self, tmp_path) -> None:
        from subprocess import TimeoutExpired

        def raise_timeout(*args, **kwargs):
            raise TimeoutExpired(_CMD, timeout=600)

        with mock.patch.object(run_tests, "_run_cmd", side_effect=raise_timeout), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        assert result["notice_code"] == NOTICE_SUITE_TIMEOUT
        assert "suite timed out" in result["notice"]

    def test_slot_contention_is_classified(self, tmp_path) -> None:
        def raise_slot_busy(*args, **kwargs):
            raise run_tests.TestConcurrencyTimeout(
                f"{SLOT_BUSY_NOTICE_PREFIX} no free slot within 600s"
            )

        with mock.patch.object(run_tests, "_run_cmd", side_effect=raise_slot_busy), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        assert result["notice_code"] == NOTICE_SLOT_BUSY
        assert result["notice"].startswith(SLOT_BUSY_NOTICE_PREFIX)
        assert result["failures"] == []

    def test_slot_busy_prefix_is_used_in_the_exception_message(self, tmp_path) -> None:
        class _BusySemaphore:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                raise TimeoutError("no slot")

            def __exit__(self, *a):
                return False

        with (
            mock.patch.object(run_tests, "Semaphore", _BusySemaphore),
            pytest.raises(run_tests.TestConcurrencyTimeout) as exc_info,
            run_tests._test_concurrency_slot(),
        ):
            pass  # pragma: no cover

        assert str(exc_info.value).startswith(SLOT_BUSY_NOTICE_PREFIX)


class TestRunAllAggregation:
    def test_aggregates_notices_codes_and_output_tails(self, monkeypatch) -> None:
        suites = {
            "all": {
                "success": False,
                "returncode": 1,
                "command": _CMD,
                "failures": [],
                "notice": f"{SLOT_BUSY_NOTICE_PREFIX} no free slot within 600s",
                "notice_code": NOTICE_SLOT_BUSY,
                "stdout": "head\ntail-of-stdout",
                "stderr": "tail-of-stderr",
                "scope": "full",
                "type": "full",
                "changed_files": [],
            },
            "node": {
                "success": True,
                "returncode": 0,
                "command": "npm --silent test",
                "failures": [],
                "notice": "",
                "notice_code": "",
                "stdout": "",
                "stderr": "",
                "scope": "full",
                "type": "full",
                "changed_files": [],
            },
        }

        def fake_run_suite(name, **kwargs):
            return suites[name]

        monkeypatch.setattr(run_tests, "run_suite", fake_run_suite)

        result = run_all(("all", "node"), use_cache=False)

        assert result["notices"] == [suites["all"]["notice"]]
        assert result["notice_codes"] == [NOTICE_SLOT_BUSY]
        assert "tail-of-stdout" in result["stdout"]
        assert "tail-of-stderr" in result["stderr"]


# ---------------------------------------------------------------------------
# Infrastructure-failure contract (SA-0MVU8XQTU006DP37)
#
# A timeout, a kill and an unparsed non-zero exit are each *provably distinct*
# from a code failure: `run_suite` must name what happened (configured timeout
# vs elapsed, signal vs exit code, the command, bounded tails) instead of
# leaving a consumer to infer it. Every case below is simulated through the
# shared fixture module, so no test sleeps for minutes, allocates gigabytes or
# contends for a host test slot.
#
# Red until the runner slice SA-0MVU8XRBQ000DG9D emits the facts.
# ---------------------------------------------------------------------------

_RUNNER_FACTS_XFAIL = pytest.mark.xfail(
    strict=True,
    reason=(
        "Contract pinned test-first by SA-0MVU8XQTU006DP37; the runner slice "
        "SA-0MVU8XRBQ000DG9D emits elapsed_seconds, configured_timeout and "
        "slot_wait_seconds plus the killed/unparsed_failure notice codes. "
        "Remove this marker in that slice (strict xfail fails on XPASS)."
    ),
)


def _run_fixture_suite(
    run_cmd,
    tmp_path: Path,
    *,
    timeout: int = fx.FIXTURE_TIMEOUT,
    slot=None,
) -> dict:
    """Run the fixture repo's suite with the command boundary stubbed."""
    slot_factory = slot or (lambda: fx.slot_acquisition(wait=0))
    with mock.patch.object(run_tests, "_run_cmd", side_effect=run_cmd), \
         mock.patch.object(run_tests, "_test_concurrency_slot", slot_factory):
        return run_suite(
            "all", cwd=_repo(tmp_path), use_cache=False, timeout=timeout
        )


def _slow_then(result):
    """Side effect: burn a short, real interval before returning *result*.

    The interval is what ``elapsed_seconds`` must measure, so it must be real
    (a stub clock would make the assertion test the stub).
    """

    def _run(*args, **kwargs):
        time.sleep(fx.FIXTURE_EXECUTION_DELAY)
        if isinstance(result, BaseException):
            raise result
        return result

    return _run


class TestRunSuiteInfrastructureFacts:
    """Every non-green suite result states *what* happened, not just *that*."""

    @_RUNNER_FACTS_XFAIL
    def test_successful_suite_reports_measured_elapsed_seconds(self, tmp_path) -> None:
        result = _run_fixture_suite(_slow_then(fx.healthy()), tmp_path)

        assert result["success"] is True
        assert result["notice_code"] == ""
        assert result["elapsed_seconds"] >= fx.FIXTURE_EXECUTION_DELAY * 0.8, (
            "elapsed_seconds must measure the command execution, not be a "
            f"placeholder (got {result['elapsed_seconds']!r})"
        )

    @_RUNNER_FACTS_XFAIL
    def test_timeout_reports_configured_timeout_and_elapsed(self, tmp_path) -> None:
        result = _run_fixture_suite(
            _slow_then(fx.timeout_expired(fx.FIXTURE_TIMEOUT)),
            tmp_path,
            timeout=fx.FIXTURE_TIMEOUT,
        )

        assert result["notice_code"] == NOTICE_SUITE_TIMEOUT
        assert result["configured_timeout"] == fx.FIXTURE_TIMEOUT, (
            "a timeout must name the timeoutPerCommand it exceeded"
        )
        assert result["elapsed_seconds"] >= fx.FIXTURE_EXECUTION_DELAY * 0.8, (
            "a timeout must report how long the suite had been running"
        )

    @_RUNNER_FACTS_XFAIL
    def test_killed_suite_reports_signal_exit_code_and_command(self, tmp_path) -> None:
        result = _run_fixture_suite(_slow_then(fx.killed(signal=9)), tmp_path)

        assert result["notice_code"] == fx.NOTICE_KILLED
        assert result["signal"] == 9, "the killing signal must be reported"
        assert result["exit_code"] == -9, "a killed child's code is -signal"
        assert fx.FIXTURE_COMMAND in result["command"]
        assert result["failures"] == [], (
            "a kill yields no structured failures; it must not be presented "
            "as a code failure"
        )

    @_RUNNER_FACTS_XFAIL
    def test_killed_suite_reports_bounded_output_tails(self, tmp_path) -> None:
        result = _run_fixture_suite(
            _slow_then(fx.killed(stderr=fx.noisy_lines())), tmp_path
        )

        assert result["notice_code"] == fx.NOTICE_KILLED
        assert "stderr-line-1999" in result["stderr"], (
            "the tail of a killed run's output is the only diagnostic a "
            "consumer gets"
        )
        assert "stderr-line-0" not in result["stderr"]
        assert len(result["stderr"]) < len(fx.noisy_lines())

    @_RUNNER_FACTS_XFAIL
    def test_unparsed_non_zero_exit_reports_command_exit_code_and_tails(
        self, tmp_path
    ) -> None:
        result = _run_fixture_suite(
            _slow_then(fx.unparsed(exit_code=3, stderr=fx.noisy_lines())), tmp_path
        )

        assert result["notice_code"] == fx.NOTICE_UNPARSED_FAILURE
        assert result["exit_code"] == 3
        assert fx.FIXTURE_COMMAND in result["command"]
        assert result["failures"] == []
        assert "stderr-line-1999" in result["stderr"]
        assert len(result["stderr"]) < len(fx.noisy_lines())

    @_RUNNER_FACTS_XFAIL
    def test_slot_busy_reports_the_bounded_wait_as_slot_wait_seconds(
        self, tmp_path
    ) -> None:
        waits: list[float] = []
        busy = run_tests.TestConcurrencyTimeout(
            f"{SLOT_BUSY_NOTICE_PREFIX} no free slot within 600s"
        )
        result = _run_fixture_suite(
            _slow_then(fx.healthy()),
            tmp_path,
            slot=lambda: fx.slot_acquisition(
                wait=fx.FIXTURE_SLOT_WAIT, raises=busy, waits=waits
            ),
        )

        assert result["notice_code"] == NOTICE_SLOT_BUSY
        assert waits == [fx.FIXTURE_SLOT_WAIT], (
            "the stubbed slot supplied the delay — no host semaphore was "
            "touched"
        )
        assert result["slot_wait_seconds"] >= fx.FIXTURE_SLOT_WAIT * 0.8, (
            "a bounded wait that expired must report the time it waited"
        )


def test_structured_failures_are_never_classified_as_infrastructure(
    tmp_path,
) -> None:
    """Precedence guard: a real code failure is never reclassified.

    Passes today and must keep passing — the new codes apply only when no
    failure could be parsed.
    """
    result = _run_fixture_suite(_slow_then(fx.structured_failure()), tmp_path)

    assert result["notice_code"] == ""
    assert result["failures"], "the parsable failure must be reported"
    assert result["returncode"] != 0


def test_published_notice_codes_match_the_runner_constants() -> None:
    """The fixture vocabulary *is* the runner's contract, not a copy of it."""
    assert fx.NOTICE_SUITE_TIMEOUT == run_tests.NOTICE_SUITE_TIMEOUT
    assert fx.NOTICE_SLOT_BUSY == run_tests.NOTICE_SLOT_BUSY


@_RUNNER_FACTS_XFAIL
def test_new_notice_codes_are_exported_by_the_runner() -> None:
    """``killed`` and ``unparsed_failure`` are stable exported codes."""
    assert run_tests.NOTICE_KILLED == fx.NOTICE_KILLED
    assert run_tests.NOTICE_UNPARSED_FAILURE == fx.NOTICE_UNPARSED_FAILURE


@_RUNNER_FACTS_XFAIL
def test_run_all_aggregates_elapsed_and_slot_wait_separately(monkeypatch) -> None:
    """Aggregation keeps elapsed and queue wait as distinct sums."""
    suites = {
        "all": {
            "success": True,
            "returncode": 0,
            "command": "pytest -q skill/test/tests/",
            "failures": [],
            "notice": "",
            "notice_code": "",
            "elapsed_seconds": 12.5,
            "slot_wait_seconds": 30.0,
            "scope": "full",
            "type": "full",
            "changed_files": [],
        },
        "node": {
            "success": False,
            "returncode": -9,
            "command": "npm --silent test",
            "failures": [],
            "notice": "suite killed by signal 9",
            "notice_code": fx.NOTICE_KILLED,
            "signal": 9,
            "elapsed_seconds": 7.5,
            "slot_wait_seconds": 0.0,
            "scope": "full",
            "type": "full",
            "changed_files": [],
        },
    }
    monkeypatch.setattr(run_tests, "run_suite", lambda name, **kw: suites[name])

    result = run_all(("all", "node"), use_cache=False)

    assert result["elapsed_seconds_total"] == pytest.approx(20.0), (
        "aggregate elapsed is the sum of command executions only"
    )
    assert result["slot_wait_seconds"] == pytest.approx(30.0), (
        "queue wait is aggregated separately and never folded into elapsed"
    )
    assert result["notice_codes"] == [fx.NOTICE_KILLED]
