"""Vitest default-reporter failure parsing (MS-0MW3UR8SX009YZWS).

Vitest's default reporter names its failures in a format the runner could not
parse — a per-file tree (``❯ |browser (chromium)| <file> (N tests | M failed)``
with ``× <suite> > <test>`` markers) and a ``Failed Tests`` block
(``FAIL  |<project>| <file> > <suite> > <test>`` followed by the error, code
frame and stack). With no records parsed, a genuine assertion failure was
classified ``unparsed_failure`` → gate ``reason: unparsed``, telling the
operator the *command or configuration* was wrong when the *code* was at fault,
and triage fell back to a ``<suite exited 1>: …`` placeholder instead of the
failing test's name.

The evidence replayed here is the captured browser-mode output in
``skill.shared.tests.gate_failure_fixtures.VITEST_BROWSER_FAILURE_OUTPUT``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
#: ``skill/`` — makes ``test.scripts``/``audit.scripts`` importable.
_SKILLS_ROOT = _SCRIPT_DIR.parents[1]
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))
#: The repository root — makes ``skill.shared`` importable.
_PROJECT_ROOT = _SCRIPT_DIR.parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import run_tests
from run_tests import parse_failures, parse_vitest_failures, run_all, run_suite
from test.scripts.gate_reason import (
    GATE_REASON_TEST_FAILURE,
    GATE_REASON_UNPARSED,
    classify_gate_result,
)
from test.scripts.run_tests import NOTICE_UNPARSED_FAILURE
from test_runner import is_vitest_command

# Deterministic infrastructure-failure simulations, shared with the finish-gate
# contract tests (SA-0MVU8XQTU006DP37).
from skill.shared.tests import gate_failure_fixtures as fx

_BROWSER_FILE = "tests/main-street/MainStreetScene.browser.test.ts"

_TREE_ONLY_OUTPUT = """\
 ❯ |browser (chromium)| tests/main-street/MainStreetScene.browser.test.ts (20 tests | 1 failed) 208769ms
   ✓ MainStreetScene browser tests > renders the street as a 2x5 grid  4142ms
   × MainStreetScene browser tests > allows pressing Enter to end the turn when legal 49575ms
     → Timed out waiting for Turn Summary dialog after end-of-turn presentation after 40000ms (cap 16000ms)
"""

_UNIT_OUTPUT = """\
 ❯ |unit| tests/unit/score.test.ts (3 tests | 1 failed) 412ms
   ✓ score > adds points  3ms
   × score > subtracts points  8ms
     → expected 2 to be 3 // Object.is equality

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  |unit| tests/unit/score.test.ts > score > subtracts points
AssertionError: expected 2 to be 3 // Object.is equality
 ❯ tests/unit/score.test.ts:14:20
     12|   const total = score(4, 2);
     13|
     14|   expect(total).toBe(3);
       |                 ^

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯

 Test Files  1 failed (1)
      Tests  1 failed | 2 passed (3)
"""

_SUMMARY_ONLY_OUTPUT = """\

 Test Files  1 failed (1)
      Tests  2 failed | 18 passed (20)
   Start at  16:00:18
   Duration  221.06s (transform 371ms, setup 0ms, collect 2.46s, tests 208.77s)
"""

_TAP_OUTPUT = """\
TAP version 13
not ok 1 - failing test
  ---
  error: |-
    Error: 1 !== 2
  stack: |-
    at demo.test.mjs:3:1
  ...
1..1
"""

_PYTEST_OUTPUT = """\
FAILED tests/test_demo.py::test_fail - AssertionError: boom
"""


def _repo(tmp_path: Path, command: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        json.dumps({"suiteCommands": [command]}), encoding="utf-8"
    )
    return repo


class TestBrowserModeFailureParsing:
    """The captured browser-mode failure parses instead of going unparsed."""

    def test_captured_browser_failure_yields_one_record_per_failing_test(
        self,
    ) -> None:
        records = parse_vitest_failures(fx.VITEST_BROWSER_FAILURE_OUTPUT)

        assert len(records) == 2, (
            "both failing tests must be named (the tree marker and the Failed "
            f"Tests block report the same two); got {[r['test_name'] for r in records]}"
        )
        names = [record["test_name"] for record in records]
        assert names[0] == (
            f"{_BROWSER_FILE} > MainStreetScene browser tests > only materializes "
            "purchased cards at destination after transfer animation completes "
            "(desktop + narrow viewports)"
        )
        assert names[1] == (
            f"{_BROWSER_FILE} > MainStreetScene browser tests > allows pressing "
            "Enter to end the turn when legal"
        )

    def test_captured_browser_failure_carries_the_cause_and_stack(
        self,
    ) -> None:
        records = parse_vitest_failures(fx.VITEST_BROWSER_FAILURE_OUTPUT)

        first = records[0]
        assert "Timed out waiting for business transfer completion after 6000ms" in (
            first["stdout_excerpt"]
        ), "the operator must see the assertion cause, not just the test name"
        assert "waitForConditionLegacy" in first["stack_trace"], (
            "triage needs the stack to point at the failing frame"
        )
        assert len(first["stdout_excerpt"]) <= 1000, (
            "the excerpt bound is a consumer contract (≤1000 chars)"
        )
        second = records[1]
        assert "Turn Summary dialog" in second["stdout_excerpt"]
        assert "core/tests/helpers/waitForCondition.ts" in second["stack_trace"]

    def test_records_carry_the_keys_triage_consumes(self) -> None:
        records = parse_vitest_failures(fx.VITEST_BROWSER_FAILURE_OUTPUT)

        payload = json.loads(json.dumps(records[0]))
        assert set(payload) == {"test_name", "stdout_excerpt", "stack_trace"}
        assert all(payload[key] for key in payload), "no empty record fields"

    def test_tree_only_output_names_the_file_and_test(self) -> None:
        """A truncated log (no Failed Tests block) still attributes the test."""
        records = parse_vitest_failures(_TREE_ONLY_OUTPUT)

        assert len(records) == 1
        assert records[0]["test_name"] == (
            f"{_BROWSER_FILE} > MainStreetScene browser tests > allows pressing "
            "Enter to end the turn when legal"
        )
        assert "Turn Summary dialog" in records[0]["stdout_excerpt"]

    def test_ansi_coloured_output_parses(self) -> None:
        ansi = "\x1b[1m\x1b[31m"
        coloured = (
            f"{ansi} FAIL  \x1b[0m|browser (chromium)| {_BROWSER_FILE} > suite > boom\n"
            "Error: boom\n"
            " ⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯\n"
        )
        records = parse_vitest_failures(coloured)

        assert [r["test_name"] for r in records] == [f"{_BROWSER_FILE} > suite > boom"]
        assert "boom" in records[0]["stdout_excerpt"]

    def test_ascii_transliterated_markers_parse(self) -> None:
        """A captured log may transliterate ``×``/``❯`` to ASCII."""
        ascii_output = (
            f" > |browser (chromium)| {_BROWSER_FILE} (20 tests | 1 failed) 100ms\n"
            "   x MainStreetScene browser tests > allows pressing Enter to end the turn when legal 49575ms\n"
            "     -> Timed out waiting for Turn Summary dialog after 40000ms\n"
        )
        records = parse_vitest_failures(ascii_output)

        assert len(records) == 1
        assert "allows pressing Enter to end the turn when legal" in records[0]["test_name"]
        assert "Turn Summary dialog" in records[0]["stdout_excerpt"]


class TestUnitProjectFailureParsing:
    """The same parser covers the non-browser Vitest projects."""

    def test_unit_project_failure_parses_once(self) -> None:
        records = parse_vitest_failures(_UNIT_OUTPUT)

        assert len(records) == 1, "the tree marker and FAIL block must de-duplicate"
        record = records[0]
        assert record["test_name"] == "tests/unit/score.test.ts > score > subtracts points"
        assert "expected 2 to be 3" in record["stdout_excerpt"]
        assert "tests/unit/score.test.ts:14:20" in record["stack_trace"]

    def test_passing_markers_are_never_records(self) -> None:
        """Only failures become records — a green run yields none."""
        green = (
            " ✓ |unit| tests/unit/score.test.ts (3 tests) 412ms\n"
            "   ✓ score > adds points  3ms\n"
            "\n Test Files  1 passed (1)\n      Tests  3 passed (3)\n"
        )
        assert parse_vitest_failures(green) == []


class TestSummaryLinesAreNeverFailures:
    """An over-broad regex would fabricate failures from the summary block."""

    def test_summary_only_output_yields_no_records(self) -> None:
        assert parse_vitest_failures(_SUMMARY_ONLY_OUTPUT) == []

    def test_summary_lines_are_excluded_from_the_captured_output(self) -> None:
        records = parse_vitest_failures(fx.VITEST_BROWSER_FAILURE_OUTPUT)

        names = " ".join(record["test_name"] for record in records)
        assert "Test Files" not in names
        assert "passed (" not in names
        assert all("Duration" not in name for name in names)


class TestUnrecognisedOutputStaysUnparsed:
    """No masking: absent Vitest markers must not become a code failure."""

    def test_enospc_infrastructure_output_yields_no_records(self) -> None:
        records = parse_vitest_failures(fx.ENOSPC_IN_PLAYWRIGHT_STDERR)

        assert records == [], (
            "an infrastructure failure without Vitest markers must stay "
            "unparsed — never presented as a failing test"
        )

    def test_tap_output_is_not_claimed_by_the_vitest_parser(self) -> None:
        assert parse_vitest_failures(_TAP_OUTPUT) == []


class TestCommandRouting:
    """``is_vitest_command``/``parse_failures`` route without breaking TAP."""

    def test_vitest_commands_are_recognised(self) -> None:
        for command in (
            "npx vitest run --project browser tests/x.browser.test.ts",
            "vitest run",
            "npm exec -- vitest run --project unit",
            "/ws/node_modules/.bin/vitest run",
        ):
            assert is_vitest_command(command), command

    def test_non_vitest_commands_are_not_recognised(self) -> None:
        for command in (
            "pytest -q -r a",
            "node --test tests/node",
            "npm --silent run lint",
            "npx playwright test",
        ):
            assert not is_vitest_command(command), command

    def test_vitest_command_routes_to_the_vitest_parser(self) -> None:
        records = parse_failures(fx.VITEST_BROWSER_FAILURE_COMMAND, fx.VITEST_BROWSER_FAILURE_OUTPUT)

        assert len(records) == 2

    def test_npm_test_wrapper_still_parses_vitest_output(self) -> None:
        """``npm test`` wraps vitest, so the command alone cannot identify it."""
        records = parse_failures("npm --silent test", _UNIT_OUTPUT)

        assert [r["test_name"] for r in records] == [
            "tests/unit/score.test.ts > score > subtracts points"
        ]

    def test_pytest_routing_is_unchanged(self) -> None:
        records = parse_failures("pytest -q -r a", _PYTEST_OUTPUT)

        assert [r["test_name"] for r in records] == ["tests/test_demo.py::test_fail"]

    def test_tap_routing_is_unchanged(self) -> None:
        records = parse_failures("node --test tests/node", _TAP_OUTPUT)

        assert [r["test_name"] for r in records] == ["failing test"]

    def test_unparsable_non_pytest_output_stays_empty(self) -> None:
        assert parse_failures("npm --silent test", "error TS5023: bad option") == []


class TestRunnerAndGateClassification:
    """A parsed Vitest failure reaches the gate as ``test_failure``."""

    def test_run_suite_reports_vitest_failures_without_a_notice_code(
        self, tmp_path: Path
    ) -> None:
        proc = fx.vitest_browser_failure()
        with mock.patch.object(run_tests, "_run_cmd", return_value=proc), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite(
                "all",
                cwd=_repo(tmp_path, fx.VITEST_BROWSER_FAILURE_COMMAND),
                use_cache=False,
            )

        assert result["success"] is False
        assert len(result["failures"]) == 2
        assert result["notice_code"] == "", (
            "a parsed failure must never also carry an infrastructure notice"
        )
        assert result["notice"] == ""

    def test_vitest_failure_classifies_as_test_failure_end_to_end(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        proc = fx.vitest_browser_failure()
        monkeypatch.setenv("TEST_SCOPE_SKIP", "1")
        with mock.patch.object(run_tests, "_run_cmd", return_value=proc), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            aggregate = run_all(
                ("all",),
                cwd=_repo(tmp_path, fx.VITEST_BROWSER_FAILURE_COMMAND),
                use_cache=False,
            )

        assert aggregate["notice_codes"] == []
        assert len(aggregate["failures"]) == 2
        assert classify_gate_result(aggregate) == GATE_REASON_TEST_FAILURE, (
            "the operator must be told to fix the code, not the command"
        )

    def test_infrastructure_failure_still_classifies_as_unparsed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The ENOSPC incident keeps its current classification (no masking)."""
        monkeypatch.setenv("TEST_SCOPE_SKIP", "1")
        with mock.patch.object(
            run_tests, "_run_cmd", return_value=fx.enospc_in_playwright()
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            aggregate = run_all(
                ("all",),
                cwd=_repo(tmp_path, fx.VITEST_BROWSER_FAILURE_COMMAND),
                use_cache=False,
            )

        assert aggregate["failures"] == []
        assert NOTICE_UNPARSED_FAILURE in aggregate["notice_codes"]
        assert classify_gate_result(aggregate) == GATE_REASON_UNPARSED


class TestAuditRunnerSharesTheParser:
    """The audit's suite-failure path must not use TAP-only parsing."""

    def test_audit_uses_the_shared_routing_function(self) -> None:
        from audit.scripts import audit_runner
        from test.scripts.run_tests import parse_failures as packaged_parse_failures

        assert audit_runner.parse_failures is packaged_parse_failures, (
            "the audit must reuse run_tests.parse_failures so the two "
            "consumers can never drift apart"
        )

    def test_audit_routing_parses_the_captured_browser_failure(self) -> None:
        from audit.scripts import audit_runner

        records = audit_runner.parse_failures(
            fx.VITEST_BROWSER_FAILURE_COMMAND, fx.VITEST_BROWSER_FAILURE_OUTPUT
        )

        assert len(records) == 2, (
            "an audit of a failing browser suite must name the test, not emit "
            "a '<suite exited 1>' placeholder"
        )

    def test_audit_runner_source_routes_every_command_through_parse_failures(
        self,
    ) -> None:
        """No TAP-only branch may survive in the audit's suite loop."""
        source = (
            Path(audit_runner_path()).read_text(encoding="utf-8")
        )

        assert "parse_failures(command, output)" in source
        assert "cmd_failures = parse_node_failures(output)" not in source


def audit_runner_path() -> Path:
    return _SKILLS_ROOT / "audit" / "scripts" / "audit_runner.py"
