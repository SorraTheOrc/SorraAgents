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

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

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
) -> dict:
    """A canned ``guarded_run_all`` result (the ``run_all`` shape)."""
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
