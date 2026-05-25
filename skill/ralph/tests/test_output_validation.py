"""Tests for Pi output validation and echo detection — SA-0MPLR8A0K0004RHJ.

Verify that empty responses, echoed input, and raw skill content are
detected and cause clear RalphError failures.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import (
    _detect_input_echo,
    _detect_raw_skill_content,
    _normalize_text_for_comparison,
    _validate_pi_output,
)
from skill.ralph.scripts.structured_response import (
    StructuredResponse,
    parse_structured_response,
)


# ---------------------------------------------------------------------------
# _normalize_text_for_comparison
# ---------------------------------------------------------------------------

class TestNormalizeTextForComparison:
    def test_normalizes_whitespace(self):
        assert _normalize_text_for_comparison("  hello   world  ") == "hello world"

    def test_lowercases(self):
        assert _normalize_text_for_comparison("Hello World") == "hello world"

    def test_empty_string(self):
        assert _normalize_text_for_comparison("") == ""

    def test_strips_newlines(self):
        assert _normalize_text_for_comparison("line1\nline2") == "line1 line2"


# ---------------------------------------------------------------------------
# _detect_input_echo
# ---------------------------------------------------------------------------

class TestDetectInputEcho:
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
        input_text = "implement SA-123\nContinue until the work item is complete."
        output_text = "implement SA-123\nContinue until"
        assert _detect_input_echo(input_text, output_text) is True

    def test_echo_with_minimal_addition(self):
        input_text = "implement SA-123"
        output_text = "implement SA-123\n"
        assert _detect_input_echo(input_text, output_text) is True

    def test_empty_input(self):
        assert _detect_input_echo("", "some output") is False

    def test_empty_output(self):
        assert _detect_input_echo("some input", "") is False

    def test_both_empty(self):
        assert _detect_input_echo("", "") is False


# ---------------------------------------------------------------------------
# _detect_raw_skill_content
# ---------------------------------------------------------------------------

class TestDetectRawSkillContent:
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


# ---------------------------------------------------------------------------
# _validate_pi_output
# ---------------------------------------------------------------------------

class TestValidatePiOutput:
    def test_valid_audit_output(self):
        is_valid, reason = _validate_pi_output(
            "implement SA-123",
            "Ready to close: Yes\n| 1 | Tests pass | met | All tests pass |",
            "audit",
        )
        assert is_valid is True
        assert reason == ""

    def test_valid_implementation_output(self):
        is_valid, reason = _validate_pi_output(
            "implement SA-123",
            "I created tests in tests/test_file.py and implemented the fix in src/module.py. All tests pass.",
            "implementation",
        )
        assert is_valid is True

    def test_empty_output_fails(self):
        is_valid, reason = _validate_pi_output(
            "implement SA-123",
            "",
            "implementation",
        )
        assert is_valid is False
        assert "no output" in reason.lower()

    def test_echoed_input_fails(self):
        is_valid, reason = _validate_pi_output(
            "implement SA-123\nContinue until the work item is complete.",
            "implement SA-123\nContinue until the work item is complete.",
            "implementation",
        )
        assert is_valid is False
        assert "echo" in reason.lower()

    def test_raw_skill_content_fails(self):
        is_valid, reason = _validate_pi_output(
            "implement SA-123",
            "# Audit\n## Overview\nThis skill audits work items.",
            "audit",
        )
        assert is_valid is False
        assert "raw skill" in reason.lower()

    def test_short_output_with_no_actions_fails_for_implementation(self):
        structured = StructuredResponse(text="ok", summary="ok", actions=())
        is_valid, reason = _validate_pi_output(
            "implement SA-123",
            "ok",
            "implementation",
            structured,
        )
        assert is_valid is False
        assert "too short" in reason.lower()

    def test_audit_missing_markers_fails(self):
        is_valid, reason = _validate_pi_output(
            "implement SA-123",
            "This is some random text about auditing.",
            "audit",
        )
        assert is_valid is False
        assert "missing" in reason.lower()

    def test_valid_implementation_with_actions(self):
        from skill.ralph.scripts.structured_response import StructuredAction
        structured = StructuredResponse(
            text="short",
            summary="short",
            actions=(StructuredAction(command="pytest", args=("-q",)),),
        )
        is_valid, reason = _validate_pi_output(
            "implement SA-123",
            "short",
            "implementation",
            structured,
        )
        assert is_valid is True


# ---------------------------------------------------------------------------
# parse_structured_response — user message filtering
# ---------------------------------------------------------------------------

class TestParseStructuredResponseUserFiltering:
    """Verify parse_structured_response skips user message content."""

    def test_user_content_not_extracted_from_agent_end(self):
        raw = '{"type": "agent_end", "messages": [{"role": "user", "content": "implement SA-123"}, {"role": "assistant", "content": []}]}'
        result = parse_structured_response(raw)
        # Should return None since only user content is present
        assert result is None

    def test_assistant_content_still_extracted(self):
        raw = '{"type": "agent_end", "messages": [{"role": "user", "content": "implement SA-123"}, {"role": "assistant", "content": "Ready to close: Yes"}]}'
        result = parse_structured_response(raw)
        assert result is not None
        assert "Ready to close: Yes" in result.text

    def test_mixed_user_assistant_extracts_only_assistant(self):
        raw = '{"type": "message_end", "message": {"role": "user", "content": "implement SA-123"}}\n{"type": "message_end", "message": {"role": "assistant", "summary": "Task complete", "actions": [{"command": "pytest"}]}}'
        result = parse_structured_response(raw)
        assert result is not None
        assert "implement SA-123" not in result.text
        assert "Task complete" == result.summary

    def test_user_content_not_extracted_from_message_start(self):
        raw = '{"type": "message_start", "message": {"role": "user", "content": "implement SA-123\nDo not ask questions"}}'
        result = parse_structured_response(raw)
        assert result is None

    def test_user_content_text_field_skipped(self):
        raw = '{"type": "message_update", "message": {"role": "user", "text": "This is user text content"}}'
        result = parse_structured_response(raw)
        assert result is None
