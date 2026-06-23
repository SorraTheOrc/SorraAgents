"""Tests for skill/scripts/pi_utils.py — shared Pi JSON-stream parsing."""

from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.scripts.pi_utils import extract_pi_text, parse_pi_json_line  # noqa: E402


# ---------------------------------------------------------------------------
# Tests: parse_pi_json_line
# ---------------------------------------------------------------------------

class TestParsePiJsonLine:
    """Verify single-line JSON-stream parsing."""

    def test_non_json_line(self):
        """Non-JSON lines return (None, False, None)."""
        assert parse_pi_json_line("not json") == (None, False, None)

    def test_empty_line(self):
        """Empty or whitespace-only lines still go through json.loads."""
        assert parse_pi_json_line("") == (None, False, None)

    def test_not_a_dict(self):
        """JSON array lines return (None, False, None)."""
        assert parse_pi_json_line('[1, 2, 3]') == (None, False, None)

    def test_text_delta(self):
        """text_delta returns the delta as stream_text."""
        line = '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"Hello "}}'
        result = parse_pi_json_line(line)
        assert result[0] == "Hello "
        assert result[1] is True

    def test_text_end(self):
        """text_end returns content as complete_text."""
        line = '{"type":"message_update","assistantMessageEvent":{"type":"text_end","content":"Hello world"}}'
        result = parse_pi_json_line(line)
        assert result[0] == ""
        assert result[2] == "Hello world"

    def test_event_type_ignored(self):
        """Session, agent_start, turn_start events return empty strings."""
        for event_type in ("session", "agent_start", "turn_start"):
            line = f'{{"type":"{event_type}"}}'
            result = parse_pi_json_line(line)
            assert result == ("", False, None), f"Unexpected result for {event_type}"

    def test_agent_end(self):
        """agent_end extracts the last assistant message text."""
        line = (
            '{"type":"agent_end","messages":['
            '{"role":"user","content":"hi"},'
            '{"role":"assistant","content":[{"type":"text","text":"Final output"}]}'
            ']}'
        )
        result = parse_pi_json_line(line)
        assert result[0] == ""
        assert result[2] == "Final output"

    def test_fallback_content_key(self):
        """Fallback to 'content' key for unrecognized event types."""
        line = '{"type":"unknown","content":"fallback text"}'
        result = parse_pi_json_line(line)
        assert result[0] == "fallback text"
        assert result[1] is True


# ---------------------------------------------------------------------------
# Tests: extract_pi_text
# ---------------------------------------------------------------------------

class TestExtractPiText:
    """Verify multi-line JSON-stream text extraction."""

    def test_extract_from_text_deltas(self):
        """Multiple text_delta lines are concatenated."""
        lines = [
            '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"Hello "}}',
            '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"world"}}',
        ]
        result = extract_pi_text("\n".join(lines))
        assert result == "Hello world"

    def test_extract_from_agent_end(self):
        """agent_end complete block is preferred over deltas."""
        lines = [
            '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"ignored"}}',
            '{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"Final answer"}]}]}',
        ]
        result = extract_pi_text("\n".join(lines))
        assert result == "Final answer"

    def test_extract_from_text_end(self):
        """text_end complete block is returned."""
        lines = [
            '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"intermediate "}}',
            '{"type":"message_update","assistantMessageEvent":{"type":"text_end","content":"Complete output"}}',
        ]
        result = extract_pi_text("\n".join(lines))
        assert result == "Complete output"

    def test_empty_input(self):
        """Empty input returns empty string."""
        assert extract_pi_text("") == ""

    def test_non_json_lines_skipped(self):
        """Lines that are not valid JSON are silently skipped."""
        lines = [
            '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"Hello"}}',
            "not json",
            "also not json",
        ]
        result = extract_pi_text("\n".join(lines))
        assert result == "Hello"

    def test_no_matching_events(self):
        """When no events match, return empty string."""
        lines = [
            '{"type":"session","sessionId":"abc"}',
            '{"type":"turn_start"}',
        ]
        result = extract_pi_text("\n".join(lines))
        assert result == ""

    def test_realistic_audit_output(self):
        """Simulate a realistic multi-line audit response."""
        lines = [
            '{"type":"session","sessionId":"audit-1"}',
            '{"type":"agent_start"}',
            '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"Audit Report"}}',
            '{"type":"message_update","assistantMessageEvent":{"type":"text_end","content":"--- AUDIT REPORT START ---\\nReady to close: Yes\\n--- AUDIT REPORT END ---"}}',
            '{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"--- AUDIT REPORT START ---\\nReady to close: Yes\\n--- AUDIT REPORT END ---"}]}]}',
        ]
        result = extract_pi_text("\n".join(lines))
        # Should prefer the agent_end (last complete block)
        assert "Ready to close: Yes" in result
        assert "AUDIT REPORT START" in result
