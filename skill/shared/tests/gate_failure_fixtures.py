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

#: Captured inode-exhaustion signature (SA-0MW2QED9M005FWOK): the suite child
#: raised ``ENOSPC`` inside ``PlaywrightBrowserProvider.createContext`` while
#: the ``/tmp`` tmpfs inode budget was exhausted, exited non-zero and produced
#: no parsable test failure — so the gate reported ``unparsed_failure`` →
#: ``unparsed`` (infrastructure-class), never a code ``test_failure``. The
#: operator diagnosis was ``df -i /tmp`` → ``1048576 1048576 0 100%``
#: (``IUse% 100`` / ``IFree 0``) while the byte budget was only ~24 % used.
ENOSPC_IN_PLAYWRIGHT_STDERR = (
    "Error: ENOSPC: no space left on device, mkdir "
    "'/tmp/pytest-of-rgardler/pytest-0/browser-profile-0' at\n"
    "    at Object.mkdir (node:internal/fs/promises:1122:9)\n"
    "    at PlaywrightBrowserProvider.createContext "
    "(/ws/node_modules/@vitest/browser/dist/index.js:2031:7)\n"
    "    at async BrowserServer.start (/ws/node_modules/@vitest/browser/dist/index.js:2204:11)\n"
)


#: Captured Vitest **browser-mode** failure output (MS-0MW3UR8SX009YZWS). The
#: text is the real default-reporter shape recorded by the serial browser soak
#: (``results/soak-browser-waits-20261102-160011-run-1-*.log``): a per-file
#: tree header (``❯ |browser (chromium)| <file> (20 tests | 2 failed)``), the
#: ``× <suite> > <test> <duration>`` markers with their ``→ <message>`` lines,
#: the ``Failed Tests`` blocks (``FAIL  |browser (chromium)| <file> > <suite>
#: > <test>`` plus the error, code frame and stack), and the ``Test Files`` /
#: ``Tests`` summary lines. Every line here is evidence, not invention: the
#: middle of the run is elided only where unaffected passing tests were.
VITEST_BROWSER_FAILURE_OUTPUT = """
 RUN  v3.2.7 /ws

 ❯ |browser (chromium)| tests/main-street/MainStreetScene.browser.test.ts (20 tests | 2 failed) 208769ms
   ✓ MainStreetScene browser tests > renders the street as a 2x5 grid  4142ms
   ✓ MainStreetScene browser tests > loads placeholder texture and renders it without squashing  5036ms
   × MainStreetScene browser tests > only materializes purchased cards at destination after transfer animation completes (desktop + narrow viewports) 29146ms
     → Timed out waiting for business transfer completion after 6000ms
   ✓ MainStreetScene browser tests > buy transfer animations end exactly at the rendered hand position (desktop + narrow viewports)  36947ms
   × MainStreetScene browser tests > allows pressing Enter to end the turn when legal 49575ms
     → Timed out waiting for Turn Summary dialog after end-of-turn presentation after 40000ms (cap 16000ms)
   ✓ MainStreetScene browser tests > shows the banked action count in the HUD action counter  3673ms

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯

 FAIL  |browser (chromium)| tests/main-street/MainStreetScene.browser.test.ts > MainStreetScene browser tests > only materializes purchased cards at destination after transfer animation completes (desktop + narrow viewports)
Error: Timed out waiting for business transfer completion after 6000ms
 ❯ waitForConditionLegacy tests/main-street/MainStreetScene.browser.test.ts:106:8
    104|   }
    105| 
    106|   throw new Error(`Timed out waiting for ${label} after ${timeoutMs}ms…
       |        ^
 ❯ tests/main-street/MainStreetScene.browser.test.ts:465:6

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/2]⎯

 FAIL  |browser (chromium)| tests/main-street/MainStreetScene.browser.test.ts > MainStreetScene browser tests > allows pressing Enter to end the turn when legal
Error: Timed out waiting for Turn Summary dialog after end-of-turn presentation after 40000ms (cap 16000ms)
 ❯ waitForCondition core/tests/helpers/waitForCondition.ts:165:8
    163|   if (predicate()) return;
    164| 
    165|   throw new Error(
       |        ^
 ❯ tests/main-street/MainStreetScene.browser.test.ts:657:4

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[2/2]⎯


 Test Files  1 failed (1)
      Tests  2 failed | 18 passed (20)
   Start at  16:00:18
   Duration  221.06s (transform 371ms, setup 0ms, collect 2.46s, tests 208.77s, environment 0ms, prepare 9.00s)

[vitest-runner] attempts=1 status=1 outcome=failed args="--project browser tests/main-street/MainStreetScene.browser.test.ts" expected=unset
"""

#: The command the captured browser output came from.
VITEST_BROWSER_FAILURE_COMMAND = (
    "npx vitest run --project browser "
    "tests/main-street/MainStreetScene.browser.test.ts"
)


#: Captured Vitest **frame-only** tail (RISE-0MW5JFQCU001YYQK, fed to the
#: gate by RISE-0MW4XICXH006AEVS / SA-0MW0NL3EW0009R4G). This is the *exact*
#: last-1200-character gate tail of the ``tce-the-rising`` unit run that
#: ``implement.py::_tail_text`` kept (``GATE_OUTPUT_TAIL_CHARS``): the suite
#: output's ``FAIL |unit| …`` header, ``AssertionError:`` line and ``|project|``
#: pipes all fell outside the window, leaving only the tail of the
#: ``+ Received:`` README string, the ``❯ <file>:<line>:<col>`` frame, its
#: source-excerpt lines and the ``⎯⎯[1/1]⎯`` separator. The pre-parse vitest
#: parser (MS-0MW3UR8SX009YZWS) needs a ``|project|`` pipe or a ``×`` tree
#: marker, so this tail parsed to ``failures: []`` → gate ``reason: unparsed``
#: — wrong for a genuine assertion failure in the docs test. Recorded from the
#: runner's cached stderr
#: (``tce-the-rising/.worklog/cache/b4dcb0f7cff13a55d1a059e47351e445/stderr.txt``)
#: with the 1200-char ``_tail_text`` rule applied — every line is evidence.
VITEST_FRAME_ONLY_OUTPUT = r"""ed a tie a player win. the report also carries its machine-readable twin (`rising-sweep-1000-progressivefaceoff-tie-neutral.json`). the candidate table (`w = 0…6`) is flat: player win rate 36.8–37.3% and the vp spread −3.44…−3.37, both inside the batch's ±1.5-point win-rate standard error, so no candidate is distinguishable and **`w = 3` is retained** rather than moved to chase noise. the material shift in this report is the rule change itself (a drawn duel now removes the player's card too, and 16.1% of resolved face-offs tie), which belongs to the balance pass (rise-0mvp9zbrv0054kc4), not to a weight tune in this item. reproduce it with: ```bash npx tsx scripts/rising-cadence-report.ts --seeds 1000 \\ --slot-zero-weights 0,1,2,3,4,5,6 \\ --write docs/balance/rising-sweep-1000-progressivefaceoff-tie-neutral.md ``` "

 ❯ tests/the-rising/the-rising-docs.test.ts:2129:20
    2127|     // explicitly labelled history rather than a description of today'…
    2128|     expect(readme).toMatch(/rising-sweep-1000-progressivefaceoff-tie-n…
    2129|     expect(readme).toMatch(/not the parity target/);
       |                    ^
    2130|   });
    2131| });

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯"""

#: The command of the captured ``tce-the-rising`` unit run. ``npm --silent
#: test`` is the project's unit ``suiteCommands`` entry (``.pi/test-config.json``
#: → ``vitest run --project unit``), so the fixture also exercises the
#: TAP-fallback routing path in ``parse_failures`` ("npm --silent test" is not
#: itself a vitest command line).
VITEST_FRAME_ONLY_COMMAND = "npm --silent test"

#: Supplementary hand-built frame-only tail (SA-0MWDLPF320060T0H): the same
#: shape as :data:`VITEST_FRAME_ONLY_OUTPUT` — a README-diff tail, the
#: ``❯ <file>:<line>:<col>`` frame and its source excerpt, no ``FAIL`` header
#: and no ``|project|`` pipes — used by the parser slice's own unit tests so
#: the frame fallback is exercised on a minimal, readable fixture as well as
#: on the captured tail.
VITEST_FRAME_ONLY_TAIL = (
    "  diff --git a/README.md b/README.md\n"
    "  index 0f4a2c1..b7e3d5a 100644\n"
    "  --- a/README.md\n"
    "  +++ b/README.md\n"
    "  @@ -2125,6 +2125,7 @@ rising-sweep-1000-progressivefaceoff-tie-n…\n"
    "  -the README states the parity target at progressive-faceoff\n"
    "  +README parity target updated to progressive-faceoff-tie\n"
    "  …\n"
    " ❯ tests/the-rising/the-rising-docs.test.ts:2129:20\n"
    "    2128|     expect(readme).toMatch(/rising-sweep-1000-progressivefaceoff-tie-n…/)\n"
    "    2129|     expect(readme).toMatch(/not the parity target/);\n"
    "       |                    ^\n"
)

#: Control for the frame-only contract: a **frame-less, marker-less** non-zero
#: vitest-adjacent output. It carries no ``FAIL |project|`` header, no
#: ``|project|`` pipes, no ``×``/``✓`` tree markers and no
#: ``❯ <path>:<line>:<col>`` frame — so it must keep today's
#: ``unparsed_failure`` → gate ``unparsed`` classification after the frame
#: parser lands (the frame regex must not over-match an arrow + prose).
VITEST_FRAME_LESS_OUTPUT = (
    "node:internal/modules/run_main:142\n"
    "  (node:12345) UnhandledPromiseRejectionWarning: boom\n"
    "Vitest caught 1 unhandled error during the test run.\n"
    " ❯ the run ended with no test failure markers to attribute.\n"
    "Tests failed with exit code 1.\n"
)


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


def enospc_in_playwright(
    *, exit_code: int = 1, stdout: str = ""
) -> subprocess.CompletedProcess:
    """The captured inode-exhaustion signature (SA-0MW2QED9M005FWOK).

    ``ENOSPC`` raised inside ``PlaywrightBrowserProvider.createContext`` — a
    host-infrastructure condition (the ``/tmp`` tmpfs inode budget exhausted),
    not a code failure. The output carries no parsable ``FAILED`` line, so the
    runner classifies it ``unparsed_failure`` and the gate ``unparsed``.
    """
    return completed(
        stdout=stdout, stderr=ENOSPC_IN_PLAYWRIGHT_STDERR, returncode=exit_code
    )


def inode_exhaustion_gate_result() -> dict:
    """The runner-aggregate shape the gate classified in the incident.

    ``notice_codes: ["unparsed_failure"]``, no structured ``failures``, and a
    non-zero per-suite exit code — the exact shape an operator sees in a gate
    report for an inode-starved suite (SA-0MW2QED9M005FWOK). A regression pin
    feeds this to ``gate_reason.classify_gate_result`` and asserts the result
    is infrastructure-class (``unparsed``), never ``test_failure``.
    """
    return {
        "success": False,
        "failures": [],
        "notices": ["suite exited 1 with no parsable failures: all"],
        "notice_codes": [NOTICE_UNPARSED_FAILURE],
        "suites": {"all": {"returncode": 1}},
    }


def vitest_browser_failure(
    *, exit_code: int = 1, output: str = VITEST_BROWSER_FAILURE_OUTPUT
) -> subprocess.CompletedProcess:
    """The captured Vitest browser-mode failure (MS-0MW3UR8SX009YZWS).

    A genuine *code* failure: the default reporter names the failing file,
    suite and test, so the runner parses structured records and the gate
    reports ``test_failure`` — never ``unparsed``. Replaying the captured text
    keeps the classification regression tied to evidence rather than to a
    hand-written approximation of the reporter.
    """
    return completed(stdout=output, returncode=exit_code)


def vitest_frame_only_failure(
    *, exit_code: int = 1, output: str = VITEST_FRAME_ONLY_OUTPUT
) -> subprocess.CompletedProcess:
    """The captured frame-only Vitest tail (RISE-0MW5JFQCU001YYQK).

    A genuine *code* failure whose ``FAIL`` header was trimmed away by the
    gate's 1200-character tail window: only the ``❯ <file>:<line>:<col>``
    frame and its source excerpt survive. The runner must still parse one
    structured record so the gate reports ``test_failure`` — never
    ``unparsed`` — for a tail the operator actually sees.
    """
    return completed(stdout=output, returncode=exit_code)


def vitest_frame_less_failure(
    *, exit_code: int = 1, output: str = VITEST_FRAME_LESS_OUTPUT
) -> subprocess.CompletedProcess:
    """A non-zero Vitest run with no failure markers at all.

    The frame parser's control: output with an arrow + prose but no
    ``❯ <path>:<line>:<col>`` frame, no ``|project|`` pipes and no tree
    markers must keep today's ``unparsed_failure`` → ``unparsed``
    classification (SA-0MW0NL3EW0009R4G AC: no regression to the
    infrastructure vocabulary).
    """
    return completed(stdout=output, returncode=exit_code)



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
