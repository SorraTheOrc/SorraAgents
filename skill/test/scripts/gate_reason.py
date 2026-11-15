"""Classify *why* a full-suite gate failed (SA-0MVSJX5HS002DJHI AC3).

The pre-push hook must tell an operator (and ``implement.py finish``) whether a
red gate means *fix the code*, *wait for a test slot*, *raise the timeout*, or
*the host killed the suite* — instead of collapsing every non-zero exit into
"the test suite failed".

This module is the **single** classifier shared by:

- ``.githooks/pre-push`` — invoked as a standalone module (its directory is on
  ``sys.path``) to print the stable ``gate_reason=<code>`` line; and
- ``skill/implement/scripts/implement.py`` — ``_gate_failure_kind`` delegates
  here, so the changed-scope gate and the dev-push gate can never drift apart.

The input is the runner's machine-readable aggregate result (``run_tests.py
--json``). The classification uses the runner's **published notice codes**
(owned by ``skill/test/scripts/run_tests.py``) plus the result's shape; it
never parses human-readable notice wording except for the documented legacy
``slot_busy`` prefix. Structured test failures always win: a genuine failure is
never reclassified as infrastructure. When the runner built no structured
failure record but the bounded output tail still carries a Vitest frame
(``❯ <file>:<line>:<col>``) — the gate's output window can strip the ``FAIL``
header and error line — the frame is reported as the failing location and the
run classifies ``test_failure``, never ``unparsed`` (SA-0MWDLPC3F005S9S2).

Reason vocabulary
-----------------
- ``test_failure`` — real, parsable failing tests. Fix the code.
- ``slot_contention`` — no free test slot within the bounded wait. Wait then
  retry, or raise ``TEST_LOCK_TIMEOUT``; do not "fix" a test.
- ``timeout`` — the suite exceeded ``timeoutPerCommand``. Raise the timeout.
- ``killed`` — the child was signal-terminated with no result (OOM/crash).
- ``unparsed`` — a non-zero exit with no parsable test failure (bad command or
  configuration, not a test).
- ``infrastructure`` — explained by a notice/refusal but not attributable to a
  test (includes a live-repo mutation or a gate-subject refusal).
- ``unknown`` — last resort: no failure, no notice and no executed command.
"""

from __future__ import annotations

import re
from typing import Any

#: Machine-readable reason codes. These are the strings emitted as
#: ``gate_reason=<code>`` and reported by ``implement.py finish``. They are
#: intentionally the same values as ``implement.GATE_FAILURE_*`` (a consistency
#: test pins the two vocabularies together).
GATE_REASON_TEST_FAILURE = "test_failure"
GATE_REASON_SLOT_CONTENTION = "slot_contention"
GATE_REASON_TIMEOUT = "timeout"
GATE_REASON_KILLED = "killed"
GATE_REASON_UNPARSED = "unparsed"
GATE_REASON_INFRASTRUCTURE = "infrastructure"
GATE_REASON_UNKNOWN = "unknown"

GATE_REASONS: tuple[str, ...] = (
    GATE_REASON_TEST_FAILURE,
    GATE_REASON_SLOT_CONTENTION,
    GATE_REASON_TIMEOUT,
    GATE_REASON_KILLED,
    GATE_REASON_UNPARSED,
    GATE_REASON_INFRASTRUCTURE,
    GATE_REASON_UNKNOWN,
)

#: Runner notice codes (owned by ``run_tests.py``). Mirrored as literals so
#: this module stays importable by the shell hook without importing the whole
#: runner; ``skill/test/tests/test_gate_reason.py`` pins them to the runner's
#: constants so a rename can never silently desynchronise them.
_NOTICE_SLOT_BUSY = "slot_busy"
_NOTICE_SUITE_TIMEOUT = "suite_timeout"
_NOTICE_KILLED = "killed"
_NOTICE_UNPARSED_FAILURE = "unparsed_failure"
_NOTICE_COMMAND_NOT_FOUND = "command_not_found"
_NOTICE_STATE_CHANGED_DURING_RUN = "state_changed_during_run"
_SLOT_BUSY_NOTICE_PREFIX = "test concurrency slot busy:"

#: Remediation an operator can act on, keyed by reason. Used by the hook's
#: human diagnostic and by ``implement.py finish``'s push-failure comment.
GATE_REMEDIATION: dict[str, str] = {
    GATE_REASON_TEST_FAILURE: (
        "Fix the failing tests, then re-run: "
        "python3 skill/test/scripts/run_tests.py --scope full"
    ),
    GATE_REASON_SLOT_CONTENTION: (
        "No free test slot within the bounded wait — an infrastructure "
        "condition, not a code failure. Wait a staggered delay before "
        "retrying (do not re-run immediately; concurrent waiters re-enter "
        "the pool in lockstep otherwise), or raise TEST_LOCK_TIMEOUT "
        "(e.g. TEST_LOCK_TIMEOUT=1800) to wait longer."
    ),
    GATE_REASON_TIMEOUT: (
        "The suite exceeded timeoutPerCommand and was stopped. Raise "
        "timeoutPerCommand in .pi/test-config.json with headroom over a "
        "green run's elapsed time, then re-run."
    ),
    GATE_REASON_KILLED: (
        "The suite child was signal-terminated without reporting a result "
        "(an OOM kill or a worker/supervisor crash). Treat this as host "
        "infrastructure; inspect the output tail and re-run — do not "
        "'fix' a test."
    ),
    GATE_REASON_UNPARSED: (
        "The suite exited non-zero and produced no parsable test failures: "
        "the command or its configuration is the problem, not a test. Read "
        "the command and output tail, fix the command/config, then re-run."
    ),
    GATE_REASON_INFRASTRUCTURE: (
        "The gate failed for an infrastructure reason (not a code failure). "
        "Read the runner notices and output tail, resolve the condition, "
        "then re-run."
    ),
    GATE_REASON_UNKNOWN: (
        "The gate failed for an unclassified reason: no structured failure, "
        "notice or executed command could be attributed. Re-run the suite "
        "with the test skill to obtain diagnostics."
    ),
}


def _suite_exit_code(result: dict[str, Any]) -> int | None:
    """A non-zero exit code reported by any suite, or None when none ran.

    The aggregate result carries per-suite ``returncode`` values; the
    implemented ``implement.py`` boundary additionally reports a resolved
    ``exit_code`` (guarded by ``exit_code_reported``). Both shapes are read, and
    the fallback values used only to keep a report's shape are ignored — a
    missing exit code must never be read as a genuine non-zero exit.
    """
    suites = result.get("suites") or {}
    if isinstance(suites, dict):
        for suite in suites.values():
            if not isinstance(suite, dict):
                continue
            returncode = suite.get("returncode")
            if isinstance(returncode, int) and returncode != 0:
                return returncode
    if result.get("exit_code_reported"):
        exit_code = result.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return exit_code
    return None


#: `` ❯ [<fn>] <file>:<line>:<col>`` — a Vitest stack frame
#: (RISE-0MW5JFQCU001YYQK, SA-0MWDLPF320060T0H). Mirrored from the runner's
#: ``run_tests._VITEST_FRAME_RE`` (pinned by a consistency test) so this
#: module stays importable by the shell hook without importing the runner.
_VITEST_FRAME_RE = re.compile(
    r"^\s*(?:❯|>)\s+(?:\S+\s+)?(?P<loc>\S+:\d+:\d+)\s*$",
    re.MULTILINE,
)


def vitest_frame_location(result: dict[str, Any] | None) -> str | None:
    """The first ``❯ <file>:<line>:<col>`` Vitest frame in the output tails.

    Consults the already-carried bounded ``stderr``/``stdout`` tails (the
    runner bounds each at ``NOTICE_OUTPUT_TAIL_CHARS``; ``implement.py``
    re-bounds at ``GATE_OUTPUT_TAIL_CHARS``), stderr first — the stream the
    gate's diagnostic tail prefers. ``None`` when neither tail carries a
    frame, so a genuinely unparsable run (ENOSPC, missing executable) is
    never fabricated into a code failure by an over-broad arrow match (the
    frame-less control `` ❯ the run ended with no test failure markers to
    attribute.`` deliberately does not match).
    """
    if not isinstance(result, dict):
        return None
    for key in ("stderr", "stdout"):
        tail = result.get(key)
        if not isinstance(tail, str):
            continue
        match = _VITEST_FRAME_RE.search(tail)
        if match:
            return match.group("loc")
    return None


def classify_gate_result(result: dict[str, Any] | None) -> str:
    """Return the stable reason code for a failed gate result.

    Genuine structured failures always win. Classification then proceeds on
    stable machine-readable notice codes (never on notice wording), with a
    narrow legacy fallback on the slot-busy notice prefix for results produced
    by an older test skill.
    """
    if not isinstance(result, dict):
        return GATE_REASON_UNKNOWN

    # A checkout that moved while the suite executed makes every failure
    # record a mixed-tree artefact (SA-0MW3FF18I003KGZE AC4): the reason is
    # infrastructure, and this check runs BEFORE the structured-failure test
    # so stale/upstream failure records can never reclassify a flagged run as a
    # code failure.
    if result.get("state_changed_during_run"):
        return GATE_REASON_INFRASTRUCTURE

    if result.get("failures"):
        return GATE_REASON_TEST_FAILURE

    frame_location = vitest_frame_location(result)

    notice_codes = [str(c) for c in (result.get("notice_codes") or [])]
    if _NOTICE_SLOT_BUSY in notice_codes:
        return GATE_REASON_SLOT_CONTENTION
    for notice in result.get("notices") or []:
        if str(notice).startswith(_SLOT_BUSY_NOTICE_PREFIX):
            return GATE_REASON_SLOT_CONTENTION

    if result.get("live_repo_mutation"):
        return GATE_REASON_INFRASTRUCTURE
    if _NOTICE_SUITE_TIMEOUT in notice_codes:
        return GATE_REASON_TIMEOUT
    if _NOTICE_KILLED in notice_codes:
        return GATE_REASON_KILLED
    if _NOTICE_COMMAND_NOT_FOUND in notice_codes:
        return GATE_REASON_INFRASTRUCTURE

    exit_code = _suite_exit_code(result)
    # Frame fallback (SA-0MWDLPC3F005S9S2): a non-zero run whose (bounded)
    # output tail carries a Vitest ``❯ <file>:<line>:<col>`` frame is a
    # genuine code failure even when no structured failure record could be
    # built — the gate's output window may have stripped the ``FAIL`` header
    # and error line, leaving the frame as the failure's surviving signal.
    # The fallback replaces ONLY the ``unparsed`` outcome: the timeout,
    # signal-kill, slot-contention and infrastructure notices above still
    # win, so none of them can be reclassified by a frame that merely
    # happened to survive in the tail.
    if exit_code is not None and exit_code > 0 and frame_location is not None:
        return GATE_REASON_TEST_FAILURE
    if _NOTICE_UNPARSED_FAILURE in notice_codes:
        return GATE_REASON_UNPARSED
    if exit_code is not None:
        return GATE_REASON_KILLED if exit_code < 0 else GATE_REASON_UNPARSED
    if notice_codes or result.get("notices"):
        return GATE_REASON_INFRASTRUCTURE
    return GATE_REASON_UNKNOWN


def state_changed_detail(flag: dict[str, Any] | None) -> str:
    """Operator-facing movement detail: checkout, commits, required re-run.

    Rendered for a flagged result (SA-0MW3FF18I003KGZE AC4) so the gate
    message names the checkout that moved, both commits, and the required
    re-run instead of an empty ``Failures:`` block.
    """
    if not isinstance(flag, dict):
        flag = {}
    checkout = flag.get("checkout") or "<unknown checkout>"
    before = flag.get("before_commit") or "<none>"
    after = flag.get("after_commit") or "<none>"
    return (
        f"checkout {checkout} moved while the suite executed "
        f"(HEAD {before} -> {after}): the mixed tree exists at no single "
        f"commit, so the verdict is discarded \u2014 re-run the suite from a "
        f"stable checkout."
    )


def gate_reason_message(reason: str) -> str:
    """The operator-facing human diagnostic for *reason* (never empty)."""
    if reason == GATE_REASON_TEST_FAILURE:
        headline = (
            "ERROR: Full test suite failed — pushes to dev/main require a "
            "green suite."
        )
    elif reason == GATE_REASON_SLOT_CONTENTION:
        headline = (
            "ERROR: No free test slot — the full-suite gate never started "
            "(infrastructure condition, not a code failure)."
        )
    elif reason == GATE_REASON_TIMEOUT:
        headline = (
            "ERROR: Full-suite gate timed out — the suite exceeded "
            "timeoutPerCommand and was stopped (infrastructure condition, "
            "not a code failure)."
        )
    elif reason == GATE_REASON_KILLED:
        headline = (
            "ERROR: Full-suite gate was killed — the suite child was "
            "signal-terminated without reporting a result (infrastructure "
            "condition, not a code failure)."
        )
    elif reason == GATE_REASON_UNPARSED:
        headline = (
            "ERROR: Full-suite gate failed before any test result — the "
            "suite command exited non-zero without parsable failures "
            "(infrastructure condition, not a code failure)."
        )
    elif reason == GATE_REASON_INFRASTRUCTURE:
        headline = (
            "ERROR: Full-suite gate failed for an infrastructure reason "
            "(not a code failure)."
        )
    else:
        headline = (
            "ERROR: Full-suite gate failed for an unclassified reason."
        )
    remedy = GATE_REMEDIATION.get(reason, GATE_REMEDIATION[GATE_REASON_UNKNOWN])
    return f"{headline}\n  {remedy}"


def render_gate_diagnostic(
    reason: str, result: dict[str, Any] | None = None
) -> str:
    """The stable machine-readable line plus the human diagnostic.

    *result* (the runner's aggregate) is optional so an existing caller keeps
    working; when it carries a moved-checkout flag the diagnostic additionally
    names the checkout, both commits and the required re-run
    (SA-0MW3FF18I003KGZE AC4) instead of only the generic infrastructure
    remedy. A ``test_failure`` whose output tail carries a Vitest frame (no
    structured failure record) additionally names the frame as the failing
    location (SA-0MWDLPC3F005S9S2).
    """
    diagnostic = f"gate_reason={reason}\n{gate_reason_message(reason)}"
    flag = (result or {}).get("state_changed_during_run")
    if flag:
        if isinstance(flag, dict):
            flag = [flag]
        details = [
            state_changed_detail(f)
            for f in flag
            if isinstance(f, dict)
        ]
        if details:
            diagnostic += "\n" + "\n".join("  " + d for d in details)
            diagnostic += (
                "\n  Re-run the suite from a stable checkout: "
                "python3 skill/test/scripts/run_tests.py --scope full"
            )
    elif reason == GATE_REASON_TEST_FAILURE:
        frame = vitest_frame_location(result)
        if frame:
            diagnostic += "\n  Failing location: " + frame
    return diagnostic
