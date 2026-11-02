"""Deterministic infrastructure-failure fixtures for test-gate classification.

Contract owner: **SA-0MVTET8KQ0031R6Z** (finish gate: classify timeouts and
kills distinctly from code failures). This module is the *test-first* slice
(SA-0MVU8XQTU006DP37): it supplies the simulations the runner contract tests
(``skill/test/tests/test_run_tests_failure_notices.py``) and the finish-gate
contract tests (``skill/implement/tests/test_implement_gate_failure_reason.py``)
share, so a timeout, a kill, an unparsed non-zero exit and a blocked slot
acquisition are each reproducible **without** a real multi-minute sleep, a
real OOM kill, or genuine host-slot contention.

Design rules (see ``skill/shared/test-writing-guidelines.md``):

* every factory returns a **genuine** ``subprocess`` object, so a monkeypatched
  ``run_tests._run_cmd`` is indistinguishable from the real call — the tests
  then exercise the *production* classification code rather than a copy of it
  (no Self-Referential Simulations);
* bounded intervals only (``FIXTURE_EXECUTION_DELAY`` / ``FIXTURE_SLOT_WAIT``
  are tens of milliseconds), so the suite stays fast and deterministic;
* the host test semaphore is never acquired or contended: the blocked-slot
  fixture is a **stub context manager**, and the pacer is never touched.

Published vocabulary
--------------------
The strings below are the contract the follow-on slices implement and the
values an operator greps for in a report. They are defined here (rather than
imported) so a test compares against the *published* value even while the
producer constant does not exist yet:

* runner notice codes — ``killed`` and ``unparsed_failure``
  (runner slice SA-0MVU8XRBQ000DG9D);
* finish-gate reasons — ``timeout``, ``killed`` and ``unparsed``
  (finish-gate slice SA-0MVU8XRTE008VDO2);
* fact fields — ``elapsed_seconds``, ``configured_timeout``,
  ``slot_wait_seconds``, ``command``, ``exit_code``, ``signal``.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager

#: The suite command the fixtures pretend to run. Matches the shape
#: ``.pi/test-config.json`` gives the fixture repository in the runner tests.
FIXTURE_COMMAND = "pytest -q -r a --disable-warnings"

#: The configured per-command timeout (``timeoutPerCommand``) the fixtures
#: report as ``configured_timeout``.
FIXTURE_TIMEOUT = 600

#: Wall-clock cost of a simulated suite execution. Deliberately tiny: long
#: enough that ``elapsed_seconds`` is measurably non-zero, far too short to
#: slow the suite down.
FIXTURE_EXECUTION_DELAY = 0.05

#: Duration of the simulated slot-queue wait (see :func:`slot_acquisition`).
FIXTURE_SLOT_WAIT = 0.05

#: Runner notice codes (published contract). ``suite_timeout`` and
#: ``slot_busy`` already exist as ``run_tests.NOTICE_*``; ``killed`` and
#: ``unparsed_failure`` arrive with the runner slice SA-0MVU8XRBQ000DG9D.
#: ``test_published_notice_codes_match_the_runner_constants`` keeps these
#: literals bound to the production constants.
NOTICE_SUITE_TIMEOUT = "suite_timeout"
NOTICE_SLOT_BUSY = "slot_busy"
NOTICE_KILLED = "killed"
NOTICE_UNPARSED_FAILURE = "unparsed_failure"

#: Finish-gate reasons (published contract, gate slice SA-0MVU8XRTE008VDO2).
REASON_TIMEOUT = "timeout"
REASON_KILLED = "killed"
REASON_UNPARSED = "unparsed"

#: Fields a consumer must be able to read off the gate/finish report, with the
#: unit each carries.
FACT_FIELDS = (
    "configured_timeout",  # seconds (int)
    "elapsed_seconds",  # seconds (float), command execution only
    "slot_wait_seconds",  # seconds (float), queue wait only — never folded in
    "command",  # the suite command that was run
    "exit_code",  # process exit code (negative => signal-terminated)
    "signal",  # signal number, only when the child was killed
)

#: Default signal used by :func:`killed` — SIGKILL, which is what an OOM kill
#: or a supervisor kill delivers.
FIXTURE_SIGNAL = 9


def completed(
    *, stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    """Return a real ``CompletedProcess`` (the ``_run_cmd`` return shape)."""
    return subprocess.CompletedProcess(
        args=FIXTURE_COMMAND.split(),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def healthy(*, stdout: str = "1 passed in 0.10s") -> subprocess.CompletedProcess:
    """A green suite: exit 0 with a parsable summary line."""
    return completed(stdout=stdout)


def structured_failure(
    *, name: str = "tests/test_fixture.py::test_boom", exit_code: int = 1
) -> subprocess.CompletedProcess:
    """A genuine code failure: non-zero exit **with** a parsable failure line.

    Used to prove classification precedence: a structured failure is a code
    failure even when a runner code also arrived.
    """
    summary = f"FAILED {name} - AssertionError: boom"
    return completed(stdout=f"{summary}\n1 failed in 0.10s", returncode=exit_code)


def killed(
    *,
    signal: int = FIXTURE_SIGNAL,
    stdout: str = "",
    stderr: str = "Killed",
) -> subprocess.CompletedProcess:
    """A signal-terminated child: ``returncode`` is ``-signal`` (Popen rule).

    This is what an OOM kill or a worker crash looks like to ``subprocess``:
    the exit *code* is negative, and no failure is parsable from the output.
    """
    return completed(stdout=stdout, stderr=stderr, returncode=-abs(signal))


def unparsed(
    *,
    exit_code: int = 3,
    stdout: str = "",
    stderr: str = "error TS5023: unknown compiler option",
) -> subprocess.CompletedProcess:
    """A non-zero exit with no parsable test failure (e.g. bad config)."""
    return completed(stdout=stdout, stderr=stderr, returncode=exit_code)


def timeout_expired(timeout: int = FIXTURE_TIMEOUT) -> subprocess.TimeoutExpired:
    """The exception ``subprocess.run(timeout=...)`` raises on expiry."""
    return subprocess.TimeoutExpired(FIXTURE_COMMAND.split(), timeout=timeout)


@contextmanager
def slot_acquisition(
    *,
    wait: float = FIXTURE_SLOT_WAIT,
    raises: BaseException | None = None,
    waits: list[float] | None = None,
) -> Iterator[None]:
    """Stub for ``run_tests._test_concurrency_slot``: wait, then yield/raise.

    Simulates queueing for a host test slot **without** touching the shared
    semaphore — nothing contends, so whatever the runner measures around this
    context manager is exactly the simulated wait and nothing else.

    *raises* is raised after the wait, modelling a bounded wait that expired
    (``TestConcurrencyTimeout``). The caller passes the exception it wants, so
    this module needs no import of the runner.

    *waits*, when given, receives the simulated wait so a test can assert that
    the stub — not a real semaphore — supplied the delay.
    """
    if waits is not None:
        waits.append(wait)
    if wait:
        time.sleep(wait)
    if raises is not None:
        raise raises
    yield


def noisy_lines(prefix: str = "stderr-line", count: int = 2000) -> str:
    """Bounded-tail fodder: ``count`` numbered lines.

    A kill or an unparsed failure can print megabytes; the reported tails must
    be bounded, so a test asserts the last line survives and the first does
    not.
    """
    return "\n".join(f"{prefix}-{i}" for i in range(count))
