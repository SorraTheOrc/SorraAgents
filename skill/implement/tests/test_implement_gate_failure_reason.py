"""Gate-failure diagnostics at the implement.py finish boundary.

Covers WL-0MVGUT787000LNDG: when ``implement.py finish``'s test gate fails
**without structured test failures**, the operator must be told *why* — a
runner notice (no free test slot, command-not-found, suite timeout) or the
command output tail for unparsed vitest/node failures — never an empty
``Failures:`` message.

The tests exercise ``_adapt_guarded_result`` (the implement.py ↔ test-skill
boundary) and the shared gate-failure message builder used by
``phase_finish``. They assert observable behaviour of the public contract:
the adapted result shape and the rendered operator message.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

# Deterministic infrastructure-failure simulations shared with the runner
# contract tests (SA-0MVU8XQTU006DP37).
from skill.shared.tests import gate_failure_fixtures as fx

WORK_ITEM = "WL-0MVGUT787000LNDG"
SLOT_NOTICE = (
    "test concurrency slot busy: no free slot within 600s "
    "(TEST_MAX_CONCURRENCY=2)"
)


def _guarded(
    *,
    success: bool = True,
    failures: list | None = None,
    notices: list[str] | None = None,
    notice_codes: list[str] | None = None,
    stdout: str = "",
    stderr: str = "",
    mutation: str | None = None,
    suites: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """A canned ``guarded_run_all`` result (the ``run_all`` shape).

    *extra* carries the infrastructure fact fields a classified run now emits
    (``configured_timeout``, ``elapsed_seconds``, ``slot_wait_seconds``,
    ``command``, ``exit_code``, ``signal``).
    """
    result = {
        "success": success and mutation is None,
        "suites": suites or {},
        "failures": failures or [],
        "notices": notices or [],
        "scope": "full",
        "type": "full",
        "resolved_scopes": ["full"],
    }
    if notice_codes is not None:
        result["notice_codes"] = notice_codes
    if stdout:
        result["stdout"] = stdout
    if stderr:
        result["stderr"] = stderr
    if mutation:
        result["live_repo_mutation"] = mutation
    result.update(extra or {})
    return result


def _adapt(**kwargs) -> dict:
    return implement._adapt_guarded_result(
        _guarded(**kwargs), tooling="suite", scope="changed"
    )


def _message(test_result: dict) -> str:
    return implement._gate_failure_message(
        test_result, attempt=1, max_retry=3, work_item_id=WORK_ITEM
    )


class TestAdaptedResultCarriesDiagnostics:
    """``_adapt_guarded_result`` must not drop the runner's diagnostics."""

    def test_notices_and_codes_are_carried(self) -> None:
        adapted = _adapt(
            success=False,
            notices=[SLOT_NOTICE],
            notice_codes=[implement._NOTICE_SLOT_BUSY],
        )

        assert adapted["notices"] == [SLOT_NOTICE]
        assert adapted["notice_codes"] == [implement._NOTICE_SLOT_BUSY]
        assert adapted["failures"] == []

    def test_aggregate_output_tail_is_carried(self) -> None:
        adapted = _adapt(success=False, stderr="Unknown file extension .ts")

        assert adapted["stderr"] == "Unknown file extension .ts"

    def test_output_falls_back_to_per_suite_output(self) -> None:
        adapted = _adapt(
            success=False,
            suites={
                "all": {
                    "stdout": "vitest exploded while loading config",
                    "stderr": "",
                    "failures": [],
                }
            },
        )

        assert "vitest exploded while loading config" in adapted["stdout"]

    def test_output_is_bounded_to_a_tail(self) -> None:
        noise = "\n".join(f"line-{i}" for i in range(5000))
        adapted = _adapt(success=False, stdout=noise)

        assert "line-4999" in adapted["stdout"]
        assert "line-0" not in adapted["stdout"]
        assert len(adapted["stdout"]) < len(noise)


class TestGateFailureClassification:
    """Classification must be narrow: genuine failures are never contended."""

    def test_slot_notice_code_is_slot_contention(self) -> None:
        adapted = _adapt(
            success=False,
            notices=[SLOT_NOTICE],
            notice_codes=[implement._NOTICE_SLOT_BUSY],
        )

        assert implement._gate_failure_kind(adapted) == (
            implement.GATE_FAILURE_SLOT_CONTENTION
        )

    def test_legacy_notice_text_without_code_is_slot_contention(self) -> None:
        adapted = _adapt(success=False, notices=[SLOT_NOTICE])

        assert implement._gate_failure_kind(adapted) == (
            implement.GATE_FAILURE_SLOT_CONTENTION
        )

    def test_structured_failures_always_win(self) -> None:
        adapted = _adapt(
            success=False,
            failures=[{"test_name": "tests/test_x.py::test_y"}],
            notices=[SLOT_NOTICE],
            notice_codes=[implement._NOTICE_SLOT_BUSY],
        )

        assert implement._gate_failure_kind(adapted) == (
            implement.GATE_FAILURE_TESTS
        )

    def test_similar_wording_is_not_slot_contention(self) -> None:
        adapted = _adapt(
            success=False,
            notices=["test concurrency slot released after 2s"],
        )

        assert implement._gate_failure_kind(adapted) == (
            implement.GATE_FAILURE_INFRASTRUCTURE
        )


class TestGateFailureMessage:
    """The operator message must always explain why the gate failed."""

    def test_slot_contention_reports_notice_and_is_not_a_code_failure(self) -> None:
        adapted = _adapt(
            success=False,
            notices=[SLOT_NOTICE],
            notice_codes=[implement._NOTICE_SLOT_BUSY],
        )

        message = _message(adapted)

        assert SLOT_NOTICE in message
        assert "infrastructure" in message
        assert "not a code failure" in message
        assert "TEST_LOCK_TIMEOUT" in message

    def test_unparsed_output_reports_command_output_tail(self) -> None:
        adapted = _adapt(
            success=False,
            stderr=(
                "Error: Failed to load vitest.config.ts\n"
                "Caused by: Unknown file extension \".ts\""
            ),
        )

        message = _message(adapted)

        assert "Unknown file extension" in message
        assert "Command output (tail)" in message

    def test_structured_failures_are_listed(self) -> None:
        adapted = _adapt(
            success=False,
            failures=["tests/test_x.py::test_y"],
        )

        message = _message(adapted)

        assert "tests/test_x.py::test_y" in message
        assert "TEST_LOCK_TIMEOUT" not in message

    def test_message_is_never_an_empty_failures_block(self) -> None:
        message = _message(_adapt(success=False))

        assert "Failures:\n\n" not in message
        assert "no failures, notices or command output" in message
        # The retry instruction is always actionable.
        assert f"implement.py finish {WORK_ITEM}" in message

    def test_live_repo_mutation_is_reported(self) -> None:
        adapted = _adapt(
            success=False,
            mutation="live-repo mutation detected: refs/heads/intruder",
        )

        message = _message(adapted)

        assert "refs/heads/intruder" in message
        assert "infrastructure" in message


# ---------------------------------------------------------------------------
# Timeout / kill / unparsed-output classification (SA-0MVU8XQTU006DP37)
#
# The runner reports *facts*; the finish gate turns them into a *deciding
# reason*. These tests pin that mapping, plus the rule that matters most to an
# operator woken by a red gate: no timeout, kill or unparsable non-zero exit
# may land on ``unknown`` with an empty ``failures`` list, because "unknown,
# no failures" is indistinguishable from a genuine code failure and leads to a
# blind retry.
#
# Red until the finish-gate slice SA-0MVU8XRTE008VDO2 lands the reasons; the
# precedence/regression tests that pass today are unmarked on purpose.
# ---------------------------------------------------------------------------

_GATE_XFAIL = pytest.mark.xfail(
    strict=True,
    reason=(
        "Contract pinned test-first by SA-0MVU8XQTU006DP37; the finish-gate "
        "slice SA-0MVU8XRTE008VDO2 adds the timeout/killed/unparsed reasons "
        "and the fact fields. Remove this marker in that slice (strict xfail "
        "fails on XPASS)."
    ),
)

#: Fact fields a classified runner emits (see
#: ``gate_failure_fixtures.FACT_FIELDS`` for the units).
_TIMEOUT_FACTS = {
    "configured_timeout": fx.FIXTURE_TIMEOUT,
    "elapsed_seconds": 300.5,
    "slot_wait_seconds": 42.0,
}

_KILLED_FACTS = {
    "command": fx.FIXTURE_COMMAND,
    "signal": 9,
    "elapsed_seconds": 12.5,
    "slot_wait_seconds": 0.0,
}

_UNPARSED_FACTS = {
    "command": fx.FIXTURE_COMMAND,
    "elapsed_seconds": 4.0,
    "slot_wait_seconds": 0.0,
}

#: A distinctive marker only present in the simulated output tails, so the
#: message assertions prove the tail was carried, not merely the word "error".
_TAIL_MARKER = "stderr-tail-marker"


def _timeout_shape() -> dict:
    """A suite that exceeded ``timeoutPerCommand`` (runner ``suite_timeout``)."""
    return _adapt(
        success=False,
        notice_codes=[fx.NOTICE_SUITE_TIMEOUT],
        notices=["suite timed out after 600s: all"],
        extra=dict(_TIMEOUT_FACTS),
    )


def _killed_shape() -> dict:
    """A killed suite: negative exit code, signal reported, no parsable failure."""
    return _adapt(
        success=False,
        suites={"node": {"returncode": -9, "failures": []}},
        notice_codes=[fx.NOTICE_KILLED],
        notices=["suite killed by signal 9: npm test"],
        stderr=(
            "FATAL ERROR: Reached heap limit Allocation failed\n"
            f"{_TAIL_MARKER}"
        ),
        extra=dict(_KILLED_FACTS),
    )


def _unparsed_shape() -> dict:
    """A non-zero suite whose output carries no parsable test failure."""
    return _adapt(
        success=False,
        suites={"all": {"returncode": 3, "failures": []}},
        notice_codes=[fx.NOTICE_UNPARSED_FAILURE],
        notices=["suite exited 3 with no parsable failures: all"],
        stderr=(
            "error TS5023: unknown compiler option\n"
            f"{_TAIL_MARKER}"
        ),
        extra=dict(_UNPARSED_FACTS),
    )


def _slot_busy_shape() -> dict:
    """A run that queued for a test slot and gave up at the bound."""
    return _adapt(
        success=False,
        notice_codes=[implement._NOTICE_SLOT_BUSY],
        notices=[SLOT_NOTICE],
        extra={"slot_wait_seconds": 240.0, "elapsed_seconds": 0.0},
    )


class TestGateReasonVocabulary:
    """The reasons are published, machine-readable strings."""

    @_GATE_XFAIL
    def test_reason_strings_are_named_and_stable(self) -> None:
        assert implement.GATE_FAILURE_TIMEOUT == fx.REASON_TIMEOUT
        assert implement.GATE_FAILURE_KILLED == fx.REASON_KILLED
        assert implement.GATE_FAILURE_UNPARSED == fx.REASON_UNPARSED


class TestGateReasonClassification:
    """Each infrastructure condition maps to its own reason."""

    @_GATE_XFAIL
    def test_timeout_code_is_timeout(self) -> None:
        assert implement._gate_failure_kind(_timeout_shape()) == (
            implement.GATE_FAILURE_TIMEOUT
        )

    @_GATE_XFAIL
    def test_killed_code_is_killed(self) -> None:
        assert implement._gate_failure_kind(_killed_shape()) == (
            implement.GATE_FAILURE_KILLED
        )

    @_GATE_XFAIL
    def test_unparsed_failure_code_is_unparsed(self) -> None:
        assert implement._gate_failure_kind(_unparsed_shape()) == (
            implement.GATE_FAILURE_UNPARSED
        )

    @_GATE_XFAIL
    def test_negative_exit_code_without_a_notice_is_still_killed(self) -> None:
        """The gate classifies the shape itself, not only a runner code."""
        adapted = _adapt(
            success=False,
            suites={"node": {"returncode": -9, "failures": []}},
        )

        assert implement._gate_failure_kind(adapted) == (
            implement.GATE_FAILURE_KILLED
        )

    @_GATE_XFAIL
    def test_non_zero_exit_without_a_notice_is_still_unparsed(self) -> None:
        adapted = _adapt(
            success=False,
            suites={"all": {"returncode": 3, "failures": []}},
        )

        assert implement._gate_failure_kind(adapted) == (
            implement.GATE_FAILURE_UNPARSED
        )

    def test_structured_failures_still_win_over_a_runner_code(self) -> None:
        """Precedence guard - a real failure is never called infrastructure."""
        adapted = _adapt(
            success=False,
            failures=[{"test_name": "tests/test_x.py::test_y"}],
            suites={"node": {"returncode": -9, "failures": []}},
            notice_codes=[fx.NOTICE_KILLED],
            extra=dict(_KILLED_FACTS),
        )

        assert implement._gate_failure_kind(adapted) == (
            implement.GATE_FAILURE_TESTS
        )


class TestNoInfrastructureConditionFallsBackToUnknown:
    """AC4: never ``unknown`` with an empty ``failures`` list."""

    def test_timeout_and_slot_contention_are_never_unknown(self) -> None:
        """Already true today; the new reasons must not regress it."""
        for shape in (_timeout_shape(), _slot_busy_shape()):
            kind = implement._gate_failure_kind(shape)
            assert kind != implement.GATE_FAILURE_UNKNOWN, (
                f"{kind!r} would send the operator into a blind code fix"
            )
            assert implement._gate_failure_detail(shape).strip(), (
                "a classified failure must carry a non-empty detail"
            )

    @_GATE_XFAIL
    def test_kill_and_unparsed_output_are_never_unknown(self) -> None:
        for shape in (
            _killed_shape(),
            _unparsed_shape(),
            _adapt(success=False, suites={"node": {"returncode": -9}}),
            _adapt(success=False, suites={"all": {"returncode": 3}}),
        ):
            kind = implement._gate_failure_kind(shape)
            assert kind != implement.GATE_FAILURE_UNKNOWN, (
                f"{kind!r} with failures=[] is indistinguishable from a "
                "genuine code failure"
            )
            detail = implement._gate_failure_detail(shape)
            assert detail.strip()
            assert "Failures:\n\n" not in detail


class TestGateFactFieldsAreCarried:
    """The implement.py boundary must not drop the facts it was handed."""

    @_GATE_XFAIL
    def test_timeout_facts_survive_adaptation(self) -> None:
        adapted = _timeout_shape()

        assert adapted["configured_timeout"] == fx.FIXTURE_TIMEOUT
        assert adapted["elapsed_seconds"] == pytest.approx(300.5)
        assert adapted["slot_wait_seconds"] == pytest.approx(42.0)

    @_GATE_XFAIL
    def test_killed_facts_survive_adaptation(self) -> None:
        adapted = _killed_shape()

        assert adapted["signal"] == 9
        assert adapted["exit_code"] == -9
        assert adapted["command"] == fx.FIXTURE_COMMAND

    @_GATE_XFAIL
    def test_unparsed_facts_survive_adaptation(self) -> None:
        adapted = _unparsed_shape()

        assert adapted["exit_code"] == 3
        assert adapted["command"] == fx.FIXTURE_COMMAND


class TestGateFailureMessageRendersFacts:
    """The message must state the facts that decide the remedy."""

    @_GATE_XFAIL
    def test_timeout_message_names_configured_timeout_and_elapsed(self) -> None:
        message = _message(_timeout_shape())

        assert str(fx.FIXTURE_TIMEOUT) in message, (
            "the operator needs the timeoutPerCommand that was exceeded"
        )
        assert "300" in message, "and how long the suite had been running"
        assert "not a code failure" in message

    @_GATE_XFAIL
    def test_killed_message_reports_command_signal_and_output_tail(self) -> None:
        message = _message(_killed_shape())

        assert fx.FIXTURE_COMMAND in message
        assert "signal" in message.lower()
        assert "9" in message
        assert _TAIL_MARKER in message

    @_GATE_XFAIL
    def test_unparsed_message_reports_command_exit_code_and_output_tail(self) -> None:
        message = _message(_unparsed_shape())

        assert fx.FIXTURE_COMMAND in message
        assert "3" in message
        assert _TAIL_MARKER in message
