"""Failure-notice and output diagnostics on suite results.

Covers WL-0MVGUT787000LNDG: when a suite fails WITHOUT structured test
failures, ``run_suite`` must carry a stable machine-readable ``notice_code``
(so ``implement.py`` can classify slot contention by code, never by string
matching the human-readable text) and a bounded ``stdout``/``stderr`` tail
(so an unparsed vitest/node failure reports its output). ``run_all``
aggregates both so ``guarded_run_all`` consumers see them at the top level.
"""

from __future__ import annotations

import json
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
    NOTICE_UNPARSED_FAILURE,
    SLOT_BUSY_NOTICE_PREFIX,
    run_all,
    run_suite,
)

# Deterministic infrastructure-failure simulations shared with the finish-gate
# contract tests (SA-0MVU8XQTU006DP37).
from skill.shared.tests import gate_failure_fixtures as fx

_CMD = "pytest -q -r a --disable-warnings"

_NOISE = "\n".join(f"stderr-line-{i}" for i in range(2000))


def _repo(tmp_path: Path, command: str = _CMD) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        json.dumps({"suiteCommands": [command]}), encoding="utf-8"
    )
    return repo


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 1):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestRunSuiteDiagnostics:
    def test_carries_bounded_output_and_classifies_unparsed_exit(
        self, tmp_path
    ) -> None:
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
        # A non-zero exit with no parsable failure is classified rather than
        # left as an unexplained non-zero result (SA-0MVU8XRBQ000DG9D).
        assert result["notice_code"] == NOTICE_UNPARSED_FAILURE
        assert "no parsable failures" in result["notice"]

    def test_inode_exhaustion_signature_is_unparsed_failure(self, tmp_path) -> None:
        """The captured inode-exhaustion output (SA-0MW2QED9M005FWOK) is
        classified ``unparsed_failure`` by the runner — an infrastructure
        condition, never a code failure.

        The incident surfaced as ``ENOSPC`` inside
        ``PlaywrightBrowserProvider.createContext`` while the ``/tmp`` tmpfs
        inode budget was at 100 %. The output contains no ``FAILED`` line, so
        ``run_suite`` must report ``unparsed_failure`` and the gate then maps
        it to ``reason: unparsed``.
        """
        proc = fx.enospc_in_playwright(exit_code=1)
        with mock.patch.object(run_tests, "_run_cmd", return_value=proc), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=False)

        assert result["notice_code"] == NOTICE_UNPARSED_FAILURE
        assert result["failures"] == []
        assert result["success"] is False
        assert result["exit_code"] == 1
        assert "ENOSPC" in result["stderr"]
        assert "PlaywrightBrowserProvider.createContext" in result["stderr"]
        assert "no parsable failures" in result["notice"]

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
# Infrastructure-failure contract (SA-0MVU8XQTU006DP37, implemented by
# SA-0MVU8XRBQ000DG9D)
#
# A timeout, a kill and an unparsed non-zero exit are each *provably distinct*
# from a code failure: `run_suite` must name what happened (configured timeout
# vs elapsed, signal vs exit code, the command, bounded tails) instead of
# leaving a consumer to infer it. Every case below is simulated through the
# shared fixture module, so no test sleeps for minutes, allocates gigabytes or
# contends for a host test slot.
# ---------------------------------------------------------------------------


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

    def test_successful_suite_reports_measured_elapsed_seconds(self, tmp_path) -> None:
        result = _run_fixture_suite(_slow_then(fx.healthy()), tmp_path)

        assert result["success"] is True
        assert result["notice_code"] == ""
        assert result["elapsed_seconds"] >= fx.FIXTURE_EXECUTION_DELAY * 0.8, (
            "elapsed_seconds must measure the command execution, not be a "
            f"placeholder (got {result['elapsed_seconds']!r})"
        )

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

    def test_queue_wait_is_not_folded_into_elapsed_seconds(self, tmp_path) -> None:
        """A long queue wait must not inflate the suite's execution time.

        The stub sleeps for the wait only; the command execution is the tiny
        fixture delay. If the wait were folded in, ``elapsed_seconds`` would
        be in the same order as the wait (parent AC3/AC4).
        """
        result = _run_fixture_suite(
            _slow_then(fx.healthy()),
            tmp_path,
            slot=lambda: fx.slot_acquisition(wait=1.0),
        )

        assert result["slot_wait_seconds"] >= 0.8
        assert result["elapsed_seconds"] < result["slot_wait_seconds"], (
            "elapsed_seconds must measure the command only, not the queue wait "
            f"(elapsed={result['elapsed_seconds']!r}, "
            f"slot_wait={result['slot_wait_seconds']!r})"
        )

    def test_cache_served_failure_is_classified_without_an_elapsed_time(
        self, tmp_path
    ) -> None:
        """A cached non-zero result is classified, and its elapsed stays null.

        Nothing executes on a cache hit, so the execution time is unknown:
        reporting ``0`` would read as an instantaneous suite.
        """
        cached_failure = {
            "stdout": "",
            "stderr": "error TS5023: unknown compiler option",
            "exit_code": 3,
            "cached": True,
            "scope": "full",
        }
        with mock.patch.object(run_tests, "run_cached", return_value=cached_failure):
            result = run_suite("all", cwd=_repo(tmp_path), use_cache=True)

        assert result["notice_code"] == fx.NOTICE_UNPARSED_FAILURE
        assert result["exit_code"] == 3
        assert result["elapsed_seconds"] is None, (
            "a cache hit has no elapsed time — report null, never 0"
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


def _run_suite_with_fixture(tmp_path: Path, command: str, proc) -> dict:
    """Run *command*'s suite against a fixture process (slot stubbed)."""
    with mock.patch.object(run_tests, "_run_cmd", side_effect=_slow_then(proc)), \
         mock.patch.object(
             run_tests, "_test_concurrency_slot", lambda: fx.slot_acquisition(wait=0)
         ):
        return run_suite("all", cwd=_repo(tmp_path, command=command), use_cache=False)


def test_vitest_browser_failure_is_a_code_failure_not_a_notice(tmp_path) -> None:
    """A real Vitest browser failure carries records and no notice code.

    Regression pin (MS-0MW3UR8SX009YZWS): the runner parsed only TAP, so the
    captured browser-mode failure produced ``failures: []`` and
    ``notice_code: unparsed_failure`` — the operator was told the command or
    configuration was wrong when the *code* was at fault.
    """
    result = _run_suite_with_fixture(
        tmp_path, fx.VITEST_BROWSER_FAILURE_COMMAND, fx.vitest_browser_failure()
    )

    assert len(result["failures"]) == 2, (
        "the failing file, suite and test must be named"
    )
    assert result["notice_code"] == ""
    assert "no parsable failures" not in result["notice"]
    assert result["success"] is False


def test_vitest_command_does_not_mask_infrastructure_failure(tmp_path) -> None:
    """Control: the same command with ENOSPC output stays ``unparsed``.

    The Vitest parser must never turn a genuine infrastructure failure into a
    ``test_failure``: without failure markers it produces no records, so the
    ENOSPC classification is unchanged.
    """
    result = _run_suite_with_fixture(
        tmp_path, fx.VITEST_BROWSER_FAILURE_COMMAND, fx.enospc_in_playwright()
    )

    assert result["failures"] == []
    assert result["notice_code"] == NOTICE_UNPARSED_FAILURE


#: Runner-level twin of the parser contract pin (SA-0MWDLP4SW0099MZM): a
#: run whose captured output is only a ``❯ <file>:<line>:<col>`` frame tail
#: must produce one structured failure and no infrastructure notice. Strict
#: xfail fails on XPASS once the parser slice SA-0MWDLPF320060T0H lands,
#: forcing marker removal there.
_FRAME_NOTICE_XFAIL = pytest.mark.xfail(
    strict=True,
    reason=(
        "Contract pinned test-first by SA-0MWDLP4SW0099MZM; the parser slice "
        "SA-0MWDLPF320060T0H parses the bare `❯ <file>:<line>:<col>` frame "
        "tail into a structured failure. Remove this marker in that slice "
        "(strict xfail fails on XPASS)."
    ),
)


@_FRAME_NOTICE_XFAIL
def test_frame_only_vitest_failure_is_a_code_failure_not_a_notice(tmp_path) -> None:
    """A 1200-char frame-only vitest tail carries a record and no notice.

    Regression pin (RISE-0MW5JFQCU001YYQK / RISE-0MW4XICXH006AEVS): the gate's
    tail window trimmed the ``FAIL`` header, ``AssertionError:`` line and
    ``|project|`` pipes from a genuine docs-assertion failure, so the runner
    parsed no record and reported ``unparsed_failure`` — the operator was told
    the command or configuration was wrong when the *code* was at fault.
    """
    result = _run_suite_with_fixture(
        tmp_path, fx.VITEST_FRAME_ONLY_COMMAND, fx.vitest_frame_only_failure()
    )

    assert len(result["failures"]) == 1, (
        "the failing file and frame must be named, never 'unparsed'"
    )
    assert result["notice_code"] == ""
    assert "no parsable failures" not in result["notice"]
    assert result["success"] is False


def test_frame_less_vitest_output_stays_unparsed_failure(tmp_path) -> None:
    """Control: marker-less non-zero vitest output keeps the unparsed notice.

    The frame parser must never turn a genuinely unparsable non-zero run into
    a ``test_failure`` (AC5, SA-0MWDLP4SW0099MZM): output with an arrow plus
    prose but no ``❯ <path>:<line>:<col>`` frame stays ``unparsed_failure``.
    """
    result = _run_suite_with_fixture(
        tmp_path, fx.VITEST_FRAME_ONLY_COMMAND, fx.vitest_frame_less_failure()
    )

    assert result["failures"] == []
    assert result["notice_code"] == NOTICE_UNPARSED_FAILURE
    assert result["success"] is False


def test_published_notice_codes_match_the_runner_constants() -> None:
    """The fixture vocabulary *is* the runner's contract, not a copy of it."""
    assert fx.NOTICE_SUITE_TIMEOUT == run_tests.NOTICE_SUITE_TIMEOUT
    assert fx.NOTICE_SLOT_BUSY == run_tests.NOTICE_SLOT_BUSY


def test_new_notice_codes_are_exported_by_the_runner() -> None:
    """``killed`` and ``unparsed_failure`` are stable exported codes."""
    assert run_tests.NOTICE_KILLED == fx.NOTICE_KILLED
    assert run_tests.NOTICE_UNPARSED_FAILURE == fx.NOTICE_UNPARSED_FAILURE


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


# ---------------------------------------------------------------------------
# Queue wait vs the per-command timeout budget (SA-0MVU8XSGI003DBWR)
#
# Parent AC4: time spent queued for a host test slot is neither charged to a
# suite's per-command timeout nor hidden inside its elapsed time. The slot
# acquisition is stubbed (no host semaphore is touched) and the command is a
# real subprocess, so the timeout under test is the production one.
# ---------------------------------------------------------------------------


class TestQueueWaitIsOutsideTheTimeoutBudget:
    """A blocking slot wait never consumes the suite's timeout budget."""

    #: Runtime of the suite command the timeout below must cover.
    COMMAND_SECONDS = 0.4

    #: Queue wait that would blow the timeout if it were charged to it.
    SLOT_WAIT_SECONDS = 1.2

    #: Per-command timeout: above ``COMMAND_SECONDS``, well below
    #: ``COMMAND_SECONDS + SLOT_WAIT_SECONDS``.
    TIMEOUT_SECONDS = 1

    def _repo_with_sleeping_suite(self, tmp_path: Path) -> Path:
        """A repo whose suite command really runs, for ``COMMAND_SECONDS``."""
        repo = tmp_path / "sleepy-repo"
        repo.mkdir()
        (repo / ".pi").mkdir()
        (repo / ".pi" / "test-config.json").write_text(
            json.dumps({"suiteCommands": [f"bash -c 'sleep {self.COMMAND_SECONDS}'"]}),
            encoding="utf-8",
        )
        return repo

    def test_blocking_slot_wait_does_not_consume_the_timeout_budget(
        self, tmp_path: Path
    ) -> None:
        """The command still succeeds although wait + runtime exceeds it.

        If the queue wait were inside the timed region, the 0.4 s command would
        be killed at the 1.0 s timeout after waiting 1.2 s for the slot.
        """
        waits: list[float] = []
        with mock.patch.object(
            run_tests,
            "_test_concurrency_slot",
            lambda: fx.slot_acquisition(wait=self.SLOT_WAIT_SECONDS, waits=waits),
        ):
            result = run_suite(
                "all",
                cwd=self._repo_with_sleeping_suite(tmp_path),
                use_cache=False,
                timeout=self.TIMEOUT_SECONDS,
            )

        assert waits == [self.SLOT_WAIT_SECONDS], (
            "the stubbed slot supplied the delay — no host semaphore was touched"
        )
        assert result["success"] is True, (
            "the queue wait must not count against the suite's timeout budget "
            f"(notice={result['notice']!r}, stderr={result['stderr']!r})"
        )
        assert result["configured_timeout"] == self.TIMEOUT_SECONDS, (
            "the report must still name the timeout that applied to the command"
        )

    def test_wait_is_reported_separately_from_the_measured_elapsed_time(
        self, tmp_path: Path
    ) -> None:
        """``slot_wait_seconds`` and ``elapsed_seconds`` measure different spans."""
        with mock.patch.object(
            run_tests,
            "_test_concurrency_slot",
            lambda: fx.slot_acquisition(wait=self.SLOT_WAIT_SECONDS),
        ):
            result = run_suite(
                "all",
                cwd=self._repo_with_sleeping_suite(tmp_path),
                use_cache=False,
                timeout=self.TIMEOUT_SECONDS,
            )

        assert result["slot_wait_seconds"] >= self.SLOT_WAIT_SECONDS * 0.8, (
            f"the wait must be reported (got {result['slot_wait_seconds']!r})"
        )
        assert result["elapsed_seconds"] < self.TIMEOUT_SECONDS, (
            "elapsed_seconds must stay within the command's own timeout, i.e. "
            f"exclude the wait (got {result['elapsed_seconds']!r})"
        )
        assert result["elapsed_seconds"] < result["slot_wait_seconds"], (
            "the two durations must be distinguishable, never the same span "
            f"(elapsed={result['elapsed_seconds']!r}, "
            f"slot_wait={result['slot_wait_seconds']!r})"
        )


# ---------------------------------------------------------------------------
# Mid-run checkout movement: state_changed_during_run (SA-0MW3FF18I003KGZE)
# ---------------------------------------------------------------------------


def _state_changed_flag(checkout: str, before: str, after: str) -> dict:
    """The flag dict ``run_cached`` returns when the checkout moved mid-run."""
    return {
        "notice_code": run_tests.NOTICE_STATE_CHANGED_DURING_RUN,
        "checkout": checkout,
        "before_commit": before,
        "after_commit": after,
        "message": "checkout moved while the suite executed",
    }


def _flagged_run(flag: dict, stdout: str = "", returncode: int = 75) -> dict:
    """A ``run_cached`` result for a flagged (moved-checkout) command."""
    return {
        "stdout": stdout,
        "stderr": "",
        "exit_code": returncode,
        "completed_at": 0.0,
        "command": _CMD,
        "git_state": "",
        "fingerprint_kind": "worktree",
        "scope": "full",
        "test_type": "full",
        "interpreter": "",
        "cached": False,
        "state_changed_during_run": flag,
    }


class TestStateChangedDuringRunNotice:
    """The runner reports a moved checkout instead of failing the tests."""

    def test_flagged_command_is_non_green_with_the_state_changed_exit_code(
        self, tmp_path
    ) -> None:
        """A moved checkout is non-green with 75, never a code failure."""
        flag = _state_changed_flag(str(tmp_path), "a" * 40, "b" * 40)
        with mock.patch.object(
            run_tests, "run_cached", return_value=_flagged_run(flag)
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path))

        assert result["success"] is False
        assert result["returncode"] == 75
        assert result["notice_code"] == run_tests.NOTICE_STATE_CHANGED_DURING_RUN
        assert result["state_changed_during_run"] == flag
        assert "re-run" in result["notice"]
        assert str(tmp_path) in result["notice"]

    def test_flagged_command_publishes_no_structured_failures(
        self, tmp_path
    ) -> None:
        """Even parseable failure text is dropped: it exists at no commit."""
        flag = _state_changed_flag(str(tmp_path), "a" * 40, "b" * 40)
        red_output = f"FAILED tests/test_x.py::test_x - AssertionError\n{_NOISE}"
        with mock.patch.object(
            run_tests,
            "run_cached",
            return_value=_flagged_run(flag, stdout=red_output, returncode=75),
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path))

        assert result["failures"] == [], "no mixed-tree failure may be published"
        assert result["success"] is False
        assert result["notice_code"] == run_tests.NOTICE_STATE_CHANGED_DURING_RUN

    def test_stable_run_result_shape_is_unchanged(self, tmp_path) -> None:
        """Control: a stable run keeps today's result exactly."""
        stable = {
            "stdout": "3 passed\n",
            "stderr": "",
            "exit_code": 0,
            "completed_at": 1.0,
            "command": _CMD,
            "git_state": "s",
            "fingerprint_kind": "worktree",
            "scope": "full",
            "test_type": "full",
            "interpreter": "",
            "cached": False,
            "state_changed_during_run": None,
        }
        with mock.patch.object(
            run_tests, "run_cached", return_value=stable
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite("all", cwd=_repo(tmp_path))

        assert result["success"] is True
        assert result["returncode"] == 0
        assert result["exit_code"] == 0
        assert result["notice_code"] == ""
        assert result["notice"] == ""
        assert result["failures"] == []
        assert result["state_changed_during_run"] is None
        assert result["cached"] is False

    def test_run_all_publishes_the_code_and_fails_the_aggregate(
        self, tmp_path, monkeypatch
    ) -> None:
        """The aggregate carries the code, no failures and a red verdict."""
        flag = _state_changed_flag(str(tmp_path), "a" * 40, "b" * 40)
        monkeypatch.setenv("TEST_SCOPE_SKIP", "1")
        with mock.patch.object(
            run_tests,
            "run_cached",
            return_value=_flagged_run(flag, stdout="FAILED tests/test_y.py::test_y\n"),
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            aggregate = run_all(("all",), cwd=_repo(tmp_path))

        assert aggregate["success"] is False
        assert run_tests.NOTICE_STATE_CHANGED_DURING_RUN in aggregate["notice_codes"]
        assert aggregate["failures"] == []
        assert aggregate["state_changed_during_run"] == [flag]

    def test_run_all_stable_aggregate_is_unchanged(self, tmp_path, monkeypatch) -> None:
        """Control: a stable aggregate keeps today's shape."""
        stable = {
            "stdout": "3 passed\n",
            "stderr": "",
            "exit_code": 0,
            "completed_at": 1.0,
            "command": _CMD,
            "git_state": "s",
            "fingerprint_kind": "worktree",
            "scope": "full",
            "test_type": "full",
            "interpreter": "",
            "cached": False,
            "state_changed_during_run": None,
        }
        monkeypatch.setenv("TEST_SCOPE_SKIP", "1")
        with mock.patch.object(
            run_tests, "run_cached", return_value=stable
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            aggregate = run_all(("all",), cwd=_repo(tmp_path))

        assert aggregate["success"] is True
        assert aggregate["notice_codes"] == []
        assert aggregate["failures"] == []
        assert aggregate["state_changed_during_run"] == []

    def test_notice_code_matches_the_cache_constant(self) -> None:
        """The runner and the cache share one vocabulary (no drift)."""
        import test_cache

        assert (
            run_tests.NOTICE_STATE_CHANGED_DURING_RUN
            == test_cache.STATE_CHANGED_NOTICE_CODE
        )
        assert run_tests.STATE_CHANGED_EXIT_CODE == test_cache.STATE_CHANGED_EXIT_CODE
