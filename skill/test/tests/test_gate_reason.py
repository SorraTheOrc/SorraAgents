"""Unit tests for the shared gate-failure classifier (SA-0MVSJX5HS002DJHI).

``skill/test/scripts/gate_reason.py`` is the single classifier used by the
pre-push hook and by ``implement.py``'s changed-scope gate. These tests pin:

- the reason vocabulary to the runner's published notice codes and to
  ``implement.GATE_FAILURE_*`` (one vocabulary, no fork);
- the classification precedence (structured failures always win);
- that every reason renders a non-empty, actionable diagnostic;
- that the caller-shape fallbacks (negative exit code → killed, non-zero with
  no parsable failure → unparsed) never land on ``unknown``.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
_SKILL_ROOT = REPO_ROOT / "skill"
if str(_SKILL_ROOT) not in sys.path:
    sys.path.append(str(_SKILL_ROOT))

import pytest

from skill.implement.scripts import implement
from skill.shared.tests import gate_failure_fixtures as fx
from skill.test.scripts import gate_reason, run_tests


def _result(**overrides) -> dict:
    base = {
        "success": False,
        "failures": [],
        "notices": [],
        "notice_codes": [],
        "suites": {},
    }
    base.update(overrides)
    return base


class TestVocabularyIsShared:
    def test_reason_literals_match_the_implement_gate_vocabulary(self) -> None:
        assert gate_reason.GATE_REASON_TEST_FAILURE == implement.GATE_FAILURE_TESTS
        assert (
            gate_reason.GATE_REASON_SLOT_CONTENTION
            == implement.GATE_FAILURE_SLOT_CONTENTION
        )
        assert gate_reason.GATE_REASON_TIMEOUT == implement.GATE_FAILURE_TIMEOUT
        assert gate_reason.GATE_REASON_KILLED == implement.GATE_FAILURE_KILLED
        assert gate_reason.GATE_REASON_UNPARSED == implement.GATE_FAILURE_UNPARSED
        assert (
            gate_reason.GATE_REASON_INFRASTRUCTURE
            == implement.GATE_FAILURE_INFRASTRUCTURE
        )
        assert gate_reason.GATE_REASON_UNKNOWN == implement.GATE_FAILURE_UNKNOWN

    def test_notice_code_mirrors_match_the_runner(self) -> None:
        assert gate_reason._NOTICE_SLOT_BUSY == run_tests.NOTICE_SLOT_BUSY
        assert gate_reason._NOTICE_SUITE_TIMEOUT == run_tests.NOTICE_SUITE_TIMEOUT
        assert gate_reason._NOTICE_KILLED == run_tests.NOTICE_KILLED
        assert (
            gate_reason._NOTICE_UNPARSED_FAILURE
            == run_tests.NOTICE_UNPARSED_FAILURE
        )
        assert (
            gate_reason._NOTICE_COMMAND_NOT_FOUND
            == run_tests.NOTICE_COMMAND_NOT_FOUND
        )
        assert (
            gate_reason._SLOT_BUSY_NOTICE_PREFIX
            == run_tests.SLOT_BUSY_NOTICE_PREFIX
        )


class TestClassification:
    def test_structured_failures_always_win(self) -> None:
        result = _result(
            failures=[{"test_name": "tests/test_x.py::test_y"}],
            notice_codes=[run_tests.NOTICE_KILLED],
            suites={"node": {"returncode": -9}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_TEST_FAILURE
        )

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (run_tests.NOTICE_SLOT_BUSY, gate_reason.GATE_REASON_SLOT_CONTENTION),
            (run_tests.NOTICE_SUITE_TIMEOUT, gate_reason.GATE_REASON_TIMEOUT),
            (run_tests.NOTICE_KILLED, gate_reason.GATE_REASON_KILLED),
            (run_tests.NOTICE_UNPARSED_FAILURE, gate_reason.GATE_REASON_UNPARSED),
            (
                run_tests.NOTICE_COMMAND_NOT_FOUND,
                gate_reason.GATE_REASON_INFRASTRUCTURE,
            ),
        ],
    )
    def test_notice_codes_map_to_their_reason(self, code: str, expected: str) -> None:
        assert gate_reason.classify_gate_result(_result(notice_codes=[code])) == expected

    def test_legacy_slot_busy_wording_is_slot_contention(self) -> None:
        result = _result(
            notices=[f"{run_tests.SLOT_BUSY_NOTICE_PREFIX} no free slot within 600s"]
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_SLOT_CONTENTION
        )

    def test_similar_wording_is_not_slot_contention(self) -> None:
        result = _result(notices=["test concurrency slot released after 2s"])
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_INFRASTRUCTURE
        )

    def test_live_repo_mutation_is_infrastructure(self) -> None:
        result = _result(live_repo_mutation="live-repo mutation detected: refs/x")
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_INFRASTRUCTURE
        )

    def test_negative_exit_code_without_a_notice_is_killed(self) -> None:
        result = _result(suites={"node": {"returncode": -9}})
        assert gate_reason.classify_gate_result(result) == gate_reason.GATE_REASON_KILLED

    def test_non_zero_exit_without_a_notice_is_unparsed(self) -> None:
        result = _result(suites={"all": {"returncode": 3}})
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_UNPARSED
        )

    def test_resolved_exit_code_shape_is_honoured(self) -> None:
        result = _result(exit_code=3, exit_code_reported=True)
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_UNPARSED
        )

    def test_nothing_attributable_is_unknown(self) -> None:
        assert gate_reason.classify_gate_result(_result()) == (
            gate_reason.GATE_REASON_UNKNOWN
        )

    def test_non_dict_result_is_unknown(self) -> None:
        assert gate_reason.classify_gate_result(None) == (
            gate_reason.GATE_REASON_UNKNOWN
        )


class TestInodeExhaustionSignature:
    """Regression pin (SA-0MW3ENVLU0097JRD): the captured inode-exhaustion
    signature must classify as infrastructure (``unparsed``), never a code
    ``test_failure``.

    The incident (``/tmp`` tmpfs at 100 % of its inode budget) surfaced as
    ``ENOSPC`` inside ``PlaywrightBrowserProvider.createContext`` — a
    non-zero exit with no parsable failure — and the gate correctly reported
    ``reason: unparsed`` (SA-0MW2QED9M005FWOK). A future classification
    change that starts treating that output as a code failure would make a
    green change un-pushable on a sick host.
    """

    def test_captured_signature_is_unparsed_never_test_failure(self) -> None:
        result = fx.inode_exhaustion_gate_result()
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_UNPARSED
        )
        assert (
            gate_reason.classify_gate_result(result)
            != gate_reason.GATE_REASON_TEST_FAILURE
        )

    def test_signature_without_a_notice_still_is_unparsed(self) -> None:
        """Even without the runner's notice code (e.g. a cache-served result
        from an older runner shape), the non-zero exit with no parsable
        failure must classify as ``unparsed``, never ``test_failure``."""
        result = fx.inode_exhaustion_gate_result()
        result["notice_codes"] = []
        result["notices"] = []
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_UNPARSED
        )

    def test_the_enospc_output_itself_contains_no_failed_line(self) -> None:
        """The captured text carries no parsable ``FAILED`` marker — the
        pre-condition that keeps the runner's ``unparsed_failure``
        classification correct. If the fixture ever gains a ``FAILED`` line,
        the signature no longer represents the incident."""
        output = fx.ENOSPC_IN_PLAYWRIGHT_STDERR
        assert "ENOSPC" in output
        assert "PlaywrightBrowserProvider.createContext" in output
        assert "FAILED" not in output


class TestDiagnostics:
    @pytest.mark.parametrize("reason", gate_reason.GATE_REASONS)
    def test_every_reason_renders_a_non_empty_actionable_diagnostic(
        self, reason: str
    ) -> None:
        diagnostic = gate_reason.render_gate_diagnostic(reason)
        assert diagnostic.startswith(f"gate_reason={reason}\n")
        assert len(diagnostic.splitlines()) >= 2
        assert gate_reason.GATE_REMEDIATION.get(reason), (
            f"reason {reason!r} has no remediation"
        )

    def test_red_suite_message_keeps_its_substance(self) -> None:
        message = gate_reason.gate_reason_message(
            gate_reason.GATE_REASON_TEST_FAILURE
        )
        assert "Full test suite failed" in message

    def test_slot_contention_message_names_the_remedies(self) -> None:
        message = gate_reason.gate_reason_message(
            gate_reason.GATE_REASON_SLOT_CONTENTION
        )
        assert "TEST_LOCK_TIMEOUT" in message
        assert "infrastructure" in message


class TestVitestFrameFallback:
    """Framed vitest tails are never classified ``unparsed``
    (SA-0MW0NL3EW0009R4G AC6 / SA-0MWDLPC3F005S9S2).

    A non-zero vitest run whose bounded output tail carries a
    ``❯ <file>:<line>:<col>`` frame is a genuine code failure even when the
    runner built no structured failure record (empty ``failures`` list — the
    gate's output window may have stripped the ``FAIL`` header and error
    line). The frame is reported as the failing location. The fallback
    replaces ONLY the ``unparsed`` outcome: signal kill, timeout,
    slot contention and infrastructure reasons still win.
    """

    FRAME_UNIT = "tests/unit/score.test.ts:14:20"
    FRAME_ONLY_RESULT = _result(
        stdout=f" ❯ {FRAME_UNIT}\n"
        "    13|     const total = score(4, 2);\n"
        "    14|   expect(total).toBe(3);\n",
        suites={"all": {"returncode": 1}},
    )

    def test_frame_regex_mirrors_the_runner(self) -> None:
        assert gate_reason._VITEST_FRAME_RE.pattern == (
            run_tests._VITEST_FRAME_RE.pattern
        ), (
            "the shared classifier's frame regex must match the runner's -- "
            "a drift would let the gate and the pre-parse disagree"
        )

    def test_framed_non_zero_result_without_structured_failures_is_test_failure(
        self,
    ) -> None:
        result = _result(
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"all": {"returncode": 1}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_TEST_FAILURE
        )

    def test_captured_tail_reports_the_frame_as_the_location(self) -> None:
        result = self.FRAME_ONLY_RESULT
        assert (
            gate_reason.vitest_frame_location(result) == self.FRAME_UNIT
        ), "the operator must be told WHERE the failure is"

    def test_captured_tail_with_test_name_prefix_parses(self) -> None:
        """Vitest frames may carry a function name before the location:
        `` ❯ waitForCondition core/tests/helpers/waitForCondition.ts:165:8``."""
        result = _result(
            stdout=(
                " ❯ waitForCondition core/tests/helpers/waitForCondition.ts:165:8\n"
                "    163|   if (predicate()) return;\n"
            ),
            suites={"all": {"returncode": 1}},
        )
        assert gate_reason.vitest_frame_location(result) == (
            "core/tests/helpers/waitForCondition.ts:165:8"
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_TEST_FAILURE
        )

    def test_frame_in_stderr_is_found(self) -> None:
        result = _result(
            stderr=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"all": {"returncode": 1}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_TEST_FAILURE
        )

    def test_unparsed_failure_notice_with_a_frame_is_still_test_failure(
        self,
    ) -> None:
        """A cache-served/older-runner shape carries the notice while the
        tail still holds the frame — the fallback must beat ``unparsed``."""
        result = _result(
            notice_codes=[run_tests.NOTICE_UNPARSED_FAILURE],
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"all": {"returncode": 1}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_TEST_FAILURE
        )

    def test_resolved_exit_code_shape_is_honoured_with_a_frame(self) -> None:
        result = _result(
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            exit_code=1,
            exit_code_reported=True,
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_TEST_FAILURE
        )

    def test_frame_less_non_zero_result_stays_unparsed(self) -> None:
        """The frame-less control (arrow + prose, no ``path:line:col``) keeps
        today's ``unparsed`` classification — the regex must not over-match."""
        result = _result(
            stdout=fx.VITEST_FRAME_LESS_OUTPUT,
            suites={"all": {"returncode": 1}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_UNPARSED
        )

    def test_enospc_control_stays_unparsed(self) -> None:
        """The inode-exhaustion signature carries no frame and must keep its
        infrastructure-class ``unparsed`` verdict (SA-0MW3ENVLU0097JRD)."""
        result = _result(
            stderr=fx.ENOSPC_IN_PLAYWRIGHT_STDERR,
            suites={"all": {"returncode": 1}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_UNPARSED
        )

    def test_signal_killed_run_with_a_frame_stays_killed(self) -> None:
        result = _result(
            notice_codes=[run_tests.NOTICE_KILLED],
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"node": {"returncode": -9}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_KILLED
        )

    def test_negative_exit_code_with_a_frame_stays_killed(self) -> None:
        """Without the notice the shape alone (negative exit) still wins."""
        result = _result(
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"node": {"returncode": -9}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_KILLED
        )

    def test_timeout_with_a_frame_stays_timeout(self) -> None:
        result = _result(
            notice_codes=[run_tests.NOTICE_SUITE_TIMEOUT],
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"all": {"returncode": 124}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_TIMEOUT
        )

    def test_slot_contention_with_a_frame_stays_slot_contention(self) -> None:
        result = _result(
            notice_codes=[run_tests.NOTICE_SLOT_BUSY],
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_SLOT_CONTENTION
        )

    def test_command_not_found_with_a_frame_stays_infrastructure(self) -> None:
        result = _result(
            notice_codes=[run_tests.NOTICE_COMMAND_NOT_FOUND],
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"all": {"returncode": 127}},
        )
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_INFRASTRUCTURE
        )

    def test_frame_without_an_executed_command_is_not_fabricated(self) -> None:
        """No exit code, no notice: a leftover frame in the tail must not turn
        a run that never executed into a code failure (no command attributed)."""
        result = _result(stdout=fx.VITEST_FRAME_ONLY_TAIL)
        assert (
            gate_reason.classify_gate_result(result)
            == gate_reason.GATE_REASON_UNKNOWN
        )


class TestVitestFrameFallbackDiagnostics:
    """The operator-facing diagnostic names the frame as the failing location
    (SA-0MWDLPC3F005S9S2 AC2)."""

    def test_render_gate_diagnostic_names_the_frame_for_test_failure(
        self,
    ) -> None:
        result = _result(
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"all": {"returncode": 1}},
        )
        diagnostic = gate_reason.render_gate_diagnostic(
            gate_reason.GATE_REASON_TEST_FAILURE, result
        )
        assert "Failing location:" in diagnostic
        assert "tests/the-rising/the-rising-docs.test.ts:2129:20" in diagnostic
        assert diagnostic.startswith("gate_reason=test_failure\n")

    def test_diagnostic_without_a_frame_is_unchanged(self) -> None:
        diagnostic = gate_reason.render_gate_diagnostic(
            gate_reason.GATE_REASON_TEST_FAILURE, _result()
        )
        assert "Failing location" not in diagnostic

    def test_diagnostic_never_names_a_frame_for_other_reasons(self) -> None:
        result = _result(
            stdout=fx.VITEST_FRAME_ONLY_TAIL,
            suites={"node": {"returncode": -9}},
        )
        diagnostic = gate_reason.render_gate_diagnostic(
            gate_reason.GATE_REASON_KILLED, result
        )
        assert "Failing location" not in diagnostic

