"""Tests for input echo detection in ralph_loop.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import (
    PiInputEchoError,
    _detect_input_echo,
    _detect_raw_skill_content,
    _normalize_text_for_comparison,
    _validate_pi_output,
    _MIN_VALID_OUTPUT_LENGTH,
)


class TestNormalizeTextForComparison:
    """Tests for the _normalize_text_for_comparison function."""

    def test_normalizes_whitespace(self):
        assert _normalize_text_for_comparison("  hello   world  ") == "hello world"

    def test_lowercases(self):
        assert _normalize_text_for_comparison("Hello World") == "hello world"

    def test_empty_string(self):
        assert _normalize_text_for_comparison("") == ""


class TestDetectInputEcho:
    """Tests for the _detect_input_echo function."""

    def test_exact_match(self):
        input_text = "implement SA-123\nContinue until done."
        output_text = "implement SA-123\nContinue until done."
        assert _detect_input_echo(input_text, output_text) is True

    def test_whitespace_only_difference(self):
        input_text = "implement SA-123"
        output_text = "  implement SA-123  "
        assert _detect_input_echo(input_text, output_text) is True

    def test_case_only_difference(self):
        input_text = "Implement SA-123"
        output_text = "implement sa-123"
        assert _detect_input_echo(input_text, output_text) is True

    def test_different_content(self):
        input_text = "implement SA-123"
        output_text = "I will implement the work item by creating tests and code."
        assert _detect_input_echo(input_text, output_text) is False

    def test_truncated_echo(self):
        input_text = "implement SA-123\nContinue until the work item and all dependencies are completed, but do not merge."
        output_text = "implement SA-123\nContinue until"
        assert _detect_input_echo(input_text, output_text) is True

    def test_echo_with_minimal_addition(self):
        input_text = "implement SA-123"
        output_text = "implement SA-123\n"
        # This should not be detected as echo since output is very similar
        assert _detect_input_echo(input_text, output_text) is True

    def test_empty_input(self):
        assert _detect_input_echo("", "some output") is False

    def test_empty_output(self):
        assert _detect_input_echo("some input", "") is False

    def test_both_empty(self):
        assert _detect_input_echo("", "") is False


class TestDetectRawSkillContent:
    """Tests for the _detect_raw_skill_content function."""

    def test_raw_skill_xml(self):
        output = '<skill name="audit" location="/path/to/SKILL.md">\n# Audit\n'
        assert _detect_raw_skill_content(output) is True

    def test_skill_with_references(self):
        output = "References are relative to...\n\n# Audit\n## Overview\n"
        assert _detect_raw_skill_content(output) is True

    def test_audit_header(self):
        output = "# Audit\n## Overview\nThis skill audits work items."
        assert _detect_raw_skill_content(output) is True

    def test_normal_output(self):
        output = "Ready to close: Yes\n| 1 | Tests pass | met | All tests pass |"
        assert _detect_raw_skill_content(output) is False

    def test_empty_output(self):
        assert _detect_raw_skill_content("") is False

    def test_none_output(self):
        assert _detect_raw_skill_content(None) is False


class TestValidatePiOutput:
    """Tests for the _validate_pi_output function."""

    def test_valid_implementation_output(self):
        input_text = "implement SA-123"
        output_text = "I will implement the work item by creating tests first, then writing code to pass them. Let me start by examining the requirements."
        is_valid, reason = _validate_pi_output(input_text, output_text, "implementation")
        assert is_valid is True
        assert reason == ""

    def test_valid_audit_output(self):
        input_text = "/skill:audit SA-123"
        output_text = "Ready to close: Yes\n| 1 | Tests pass | met | All tests pass |"
        is_valid, reason = _validate_pi_output(input_text, output_text, "audit")
        assert is_valid is True
        assert reason == ""

    def test_echo_detected(self):
        input_text = "implement SA-123\nContinue until done."
        output_text = "implement SA-123\nContinue until done."
        is_valid, reason = _validate_pi_output(input_text, output_text, "implementation")
        assert is_valid is False
        assert "echo" in reason.lower()

    def test_raw_skill_content_detected(self):
        input_text = "/skill:audit SA-123"
        output_text = '<skill name="audit" location="/path/to/SKILL.md">\n# Audit\n'
        is_valid, reason = _validate_pi_output(input_text, output_text, "audit")
        assert is_valid is False
        assert "skill" in reason.lower() or "raw" in reason.lower()

    def test_short_implementation_with_no_actions(self):
        from skill.ralph.scripts.structured_response import StructuredResponse
        input_text = "implement SA-123"
        output_text = "OK"
        # Create a structured response with no actions
        structured = StructuredResponse(actions=[], summary="OK", text="OK")
        is_valid, reason = _validate_pi_output(input_text, output_text, "implementation", structured)
        assert is_valid is False
        assert "short" in reason.lower()

    def test_audit_missing_markers(self):
        input_text = "/skill:audit SA-123"
        output_text = "I will audit the work item."
        is_valid, reason = _validate_pi_output(input_text, output_text, "audit")
        assert is_valid is False
        assert "marker" in reason.lower() or "ready to close" in reason.lower()

    def test_long_output_without_markers_is_valid(self):
        # Long output without markers should be considered valid (may be partial audit)
        input_text = "/skill:audit SA-123"
        output_text = "A" * 200  # Long enough to pass length check
        is_valid, reason = _validate_pi_output(input_text, output_text, "audit")
        assert is_valid is True


class TestPiInputEchoError:
    """Tests for the PiInputEchoError class."""

    def test_stores_input_and_output(self):
        error = PiInputEchoError(
            "Output is an echo",
            input_text="implement SA-123",
            output_text="implement SA-123",
        )
        assert str(error) == "Output is an echo"
        assert error.input_text == "implement SA-123"
        assert error.output_text == "implement SA-123"

    def test_is_ralph_error(self):
        from skill.ralph.scripts.ralph_loop import RalphError

        error = PiInputEchoError("test", "input", "output")
        assert isinstance(error, RalphError)
