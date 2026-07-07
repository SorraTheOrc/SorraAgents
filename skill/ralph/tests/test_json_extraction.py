"""Tests for assistant-only Pi JSON extraction — SA-0MPLR89R6002EQBS.

Verify that _parse_pi_json_line() and _extract_text_from_json_output()
never extract content from role=user messages.
"""

from __future__ import annotations

import json
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import (  # noqa: E402
    RalphLoop,
    _extract_text_from_assistant_message,
    _extract_text_from_content,
    _extract_text_from_json_output,
    _extract_last_assistant_message_text,
    _parse_pi_json_line,
)  # noqa: E402


# ---------------------------------------------------------------------------
# _extract_text_from_content
# ---------------------------------------------------------------------------

class TestExtractTextFromContent:
    def test_string(self):
        assert _extract_text_from_content("hello") == "hello"

    def test_empty_string(self):
        assert _extract_text_from_content("") is None

    def test_list_of_strings(self):
        assert _extract_text_from_content(["a", "b"]) == "a\nb"

    def test_dict_with_text_type(self):
        assert _extract_text_from_content({"type": "text", "text": "hi"}) == "hi"

    def test_dict_with_nested_text(self):
        assert _extract_text_from_content({"content": {"text": "nested"}}) == "nested"

    def test_none(self):
        assert _extract_text_from_content(None) is None

    def test_empty_list(self):
        assert _extract_text_from_content([]) is None

    def test_empty_dict(self):
        assert _extract_text_from_content({}) is None

    def test_dict_with_delta(self):
        assert _extract_text_from_content({"delta": "streaming text"}) == "streaming text"


# ---------------------------------------------------------------------------
# _extract_text_from_assistant_message
# ---------------------------------------------------------------------------

class TestExtractTextFromAssistantMessage:
    def test_assistant_with_string_content(self):
        msg = {"role": "assistant", "content": "assistant reply"}
        assert _extract_text_from_assistant_message(msg) == "assistant reply"

    def test_assistant_with_content_list(self):
        msg = {"role": "assistant", "content": [{"type": "text", "text": "block"}]}
        assert _extract_text_from_assistant_message(msg) == "block"

    def test_user_message_returns_none(self):
        msg = {"role": "user", "content": "user prompt text"}
        assert _extract_text_from_assistant_message(msg) is None

    def test_system_message_returns_none(self):
        msg = {"role": "system", "content": "system prompt"}
        assert _extract_text_from_assistant_message(msg) is None

    def test_assistant_empty_content(self):
        msg = {"role": "assistant", "content": []}
        assert _extract_text_from_assistant_message(msg) is None

    def test_non_dict_returns_none(self):
        assert _extract_text_from_assistant_message("not a dict") is None
        assert _extract_text_from_assistant_message(None) is None
        assert _extract_text_from_assistant_message([]) is None


# ---------------------------------------------------------------------------
# _extract_last_assistant_message_text
# ---------------------------------------------------------------------------

class TestExtractLastAssistantMessageText:
    def test_returns_last_assistant(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "reply one"},
            {"role": "user", "content": "follow up"},
            {"role": "assistant", "content": "reply two"},
        ]
        assert _extract_last_assistant_message_text(messages) == "reply two"

    def test_ignores_user_messages(self):
        messages = [
            {"role": "user", "content": "this is the only message"},
        ]
        assert _extract_last_assistant_message_text(messages) is None

    def test_no_assistant_returns_none(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "world"},
        ]
        assert _extract_last_assistant_message_text(messages) is None

    def test_empty_list(self):
        assert _extract_last_assistant_message_text([]) is None

    def test_non_list_input(self):
        assert _extract_last_assistant_message_text("not a list") is None

    def test_mixed_content_types(self):
        messages = [
            {"role": "assistant", "content": []},  # empty
            {"role": "user", "content": "echoed user prompt - should be ignored"},
            {"role": "assistant", "content": "real assistant response"},
        ]
        result = _extract_last_assistant_message_text(messages)
        assert result == "real assistant response"


# ---------------------------------------------------------------------------
# _parse_pi_json_line — user message filtering
# ---------------------------------------------------------------------------

class TestParsePiJsonLineUserMessageFiltering:
    """Verify user messages are never extracted as response text."""

    def _event(self, event_type, **extra):
        return json.dumps({"type": event_type, **extra})

    def test_message_start_user_role_returns_none(self):
        """message_start with role=user must not return text."""
        line = self._event("message_start", message={
            "role": "user",
            "content": "This is the user prompt that should NEVER be extracted",
        })
        stream_text, should_print, complete_text = _parse_pi_json_line(line)
        assert complete_text is None, (
            f"User message content must not be extracted, got: {complete_text!r}"
        )

    def test_message_end_user_role_returns_none(self):
        """message_end with role=user must not return text."""
        line = self._event("message_end", message={
            "role": "user",
            "content": [
                {"type": "text", "text": "Full skill content of the user prompt"}
            ],
        })
        stream_text, should_print, complete_text = _parse_pi_json_line(line)
        assert complete_text is None, (
            f"User message content must not be extracted, got: {complete_text!r}"
        )

    def test_turn_end_user_role_returns_none(self):
        """turn_end with role=user must not return text."""
        line = self._event("turn_end", message={
            "role": "user",
            "content": "Turn-end user message",
        })
        stream_text, should_print, complete_text = _parse_pi_json_line(line)
        assert complete_text is None

    def test_agent_end_mixed_roles_extracts_only_assistant(self):
        """agent_end with user and assistant messages extracts only assistant text."""
        line = self._event("agent_end", messages=[
            {"role": "user", "content": "Implement SA-123. Do not ask questions."},
            {"role": "assistant", "content": []},  # empty assistant
            {"role": "user", "content": "Another user message"},
            {"role": "assistant", "content": "Audit report: Ready to close: Yes"},
        ])
        stream_text, should_print, complete_text = _parse_pi_json_line(line)
        assert complete_text == "Audit report: Ready to close: Yes", (
            f"Expected assistant text only, got: {complete_text!r}"
        )

    def test_agent_end_only_user_messages_returns_none(self):
        """agent_end with only user messages must not return text."""
        line = self._event("agent_end", messages=[
            {"role": "user", "content": "Implement SA-123"},
            {"role": "user", "content": "Follow up question"},
        ])
        stream_text, should_print, complete_text = _parse_pi_json_line(line)
        assert complete_text is None, (
            f"No text should be extracted when only user messages exist, got: {complete_text!r}"
        )

    def test_agent_end_empty_assistant_returns_none(self):
        """agent_end with assistant having empty content returns None."""
        line = self._event("agent_end", messages=[
            {"role": "user", "content": "implement SA-123"},
            {"role": "assistant", "content": []},
        ])
        stream_text, should_print, complete_text = _parse_pi_json_line(line)
        assert complete_text is None

    def test_assistant_message_text_is_extracted(self):
        """Assistant messages should still be extracted normally."""
        line = self._event("message_end", message={
            "role": "assistant",
            "content": "This is a valid assistant response",
        })
        stream_text, should_print, complete_text = _parse_pi_json_line(line)
        assert complete_text == "This is a valid assistant response"

    def test_message_update_user_role_in_assistant_event_suppressed(self):
        """A message_update with user-role content in assistantMessageEvent must not leak."""
        # This tests the case where the assistantMessageEvent itself contains user content
        line = json.dumps({
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_end",
                "content": "This should only come from assistant events",
            },
        })
        stream_text, should_print, complete_text = _parse_pi_json_line(line)
        # This IS from assistantMessageEvent so it IS extracted
        # (The filtering is at the role level, not at the event level)
        assert complete_text == "This should only come from assistant events"


# ---------------------------------------------------------------------------
# _extract_text_from_json_output — user message filtering
# ---------------------------------------------------------------------------

class TestExtractTextFromJsonOutputUserFiltering:
    """Integration tests for _extract_text_from_json_output."""

    def test_user_echo_only_returns_empty(self):
        """When Pi returns only user messages, no text should be extracted."""
        lines = [
            json.dumps({"type": "session", "id": "abc123"}),
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "turn_start"}),
            json.dumps({
                "type": "message_end",
                "message": {"role": "user", "content": "implement SA-123"},
            }),
            json.dumps({
                "type": "agent_end",
                "messages": [
                    {"role": "user", "content": "implement SA-123"},
                    {"role": "assistant", "content": []},
                ],
            }),
        ]
        raw = "\n".join(lines)
        result = _extract_text_from_json_output(raw)
        assert result == "", (
            f"Expected empty string when only user messages present, got: {result!r}"
        )

    def test_valid_assistant_response_extracted(self):
        """Valid assistant response should be extracted."""
        lines = [
            json.dumps({"type": "session", "id": "abc123"}),
            json.dumps({"type": "agent_end", "messages": [
                {"role": "user", "content": "implement SA-123"},
                {"role": "assistant", "content": "Ready to close: Yes\n| AC1 | Tests pass | met | ... |"},
            ]}),
        ]
        raw = "\n".join(lines)
        result = _extract_text_from_json_output(raw)
        assert "Ready to close: Yes" in result

    def test_text_delta_from_assistant_extracted(self):
        """text_delta events should still work for streaming."""
        lines = [
            json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "Hello",
                },
            }),
            json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": " world",
                },
            }),
        ]
        raw = "\n".join(lines)
        result = _extract_text_from_json_output(raw)
        assert result == "Hello world"

    def test_text_end_from_assistant_extracted(self):
        """text_end events should provide complete blocks."""
        lines = [
            json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_end",
                    "content": "Complete response block",
                },
            }),
        ]
        raw = "\n".join(lines)
        result = _extract_text_from_json_output(raw)
        assert result == "Complete response block"

    def test_mixed_user_and_assistant_agent_end(self):
        """agent_end with mixed messages extracts only assistant."""
        lines = [
            json.dumps({"type": "agent_end", "messages": [
                {"role": "user", "content": "implement SA-123\nDo not ask questions"},
                {"role": "assistant", "content": []},
                {"role": "user", "content": "Another user message with lots of text"},
                {"role": "assistant", "content": "Final assistant answer"},
            ]}),
        ]
        raw = "\n".join(lines)
        result = _extract_text_from_json_output(raw)
        assert result == "Final assistant answer", (
            f"Expected assistant answer, got: {result!r}"
        )

    def test_non_json_passthrough(self):
        """Non-JSON lines should be returned as-is (fallback)."""
        raw = "Some non-JSON output"
        result = _extract_text_from_json_output(raw)
        assert result == "Some non-JSON output"


# ---------------------------------------------------------------------------
# _stream_pi integration: user messages not returned
# ---------------------------------------------------------------------------

class TestStreamPiUserMessageFiltering:
    """Test that _stream_pi does not return user message content."""

    def _fake_process(self, lines, returncode=0):
        proc = MagicMock()
        proc.returncode = returncode
        proc.poll.return_value = returncode
        proc.wait.return_value = returncode

        class FakeStream:
            def __init__(self, items):
                self._items = list(items)
                self._idx = 0
            def readline(self, size=-1):
                if self._idx < len(self._items):
                    line = self._items[self._idx]
                    self._idx += 1
                    return line
                return ""
            def read(self, size=-1):
                return ""

        proc.stdout = FakeStream(lines)
        proc.stderr = FakeStream([])
        return proc

    def test_stream_pi_raises_error_on_user_message_only(self):
        """When Pi returns user messages only (no assistant text), RalphError is raised."""
        import json as _json
        lines = [
            _json.dumps({"type": "session", "id": "abc"}) + "\n",
            _json.dumps({"type": "agent_start"}) + "\n",
            _json.dumps({
                "type": "agent_end",
                "messages": [
                    {"role": "user", "content": "implement SA-123"},
                    {"role": "assistant", "content": []},
                ],
            }) + "\n",
        ]
        proc = self._fake_process(lines)
        loop = RalphLoop(pi_bin="pi", stream=True, verbose=False)

        with (
            patch("subprocess.Popen", return_value=proc),
            redirect_stdout(io.StringIO()),
        ):
            with pytest.raises(Exception) as exc_info:
                loop._stream_pi(
                    ["pi", "-p", "--mode", "json", "implement SA-123"],
                    "implement SA-123",
                )

        assert "invalid output" in str(exc_info.value).lower(), (
            f"Expected RalphError about invalid output, got: {exc_info.value}"
        )

    def test_stream_pi_returns_assistant_text(self):
        """When Pi returns assistant messages, they should be returned."""
        import json as _json
        lines = [
            _json.dumps({"type": "session", "id": "abc"}) + "\n",
            _json.dumps({
                "type": "agent_end",
                "messages": [
                    {"role": "user", "content": "implement SA-123"},
                    {"role": "assistant", "content": "Ready to close: Yes"},
                ],
            }) + "\n",
        ]
        proc = self._fake_process(lines)
        loop = RalphLoop(pi_bin="pi", stream=True, verbose=False)

        with (
            patch("subprocess.Popen", return_value=proc),
            redirect_stdout(io.StringIO()),
        ):
            result = loop._stream_pi(
                ["pi", "-p", "--mode", "json", "implement SA-123"],
                "implement SA-123",
            )

        assert result == "Ready to close: Yes"
