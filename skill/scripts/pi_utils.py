#!/usr/bin/env python3
"""Shared Pi JSON-stream parsing utilities.

Provides functions for extracting user-facing text from the JSON-stream
output produced by ``pi -p --mode json``. This matches the pattern used
in ``skill/audit/scripts/audit_runner.py``, ``skill/intakeall/scripts/intakeall.py``,
and other skills that invoke Pi non-interactively.

Usage:
    from skill.scripts.pi_utils import extract_pi_text

    raw = subprocess.run(..., capture_output=True).stdout
    text = extract_pi_text(raw)
"""

from __future__ import annotations

import json
from typing import Optional


__all__ = ["extract_pi_text", "parse_pi_json_line"]


def parse_pi_json_line(line: str):
    """Parse a single JSON line from ``pi --mode json`` output.

    Returns a tuple ``(stream_text, should_print, complete_text)``:
      - ``stream_text``: accumulated text delta content, if any
      - ``should_print``: whether ``stream_text`` should be printed
      - ``complete_text``: final complete content from a ``text_end`` or
        ``agent_end`` event, if any

    Any value may be ``None`` if the line was not valid JSON or did not
    contain a recognized event type.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None, False, None
    if not isinstance(obj, dict):
        return None, False, None

    event_type = obj.get("type", "")

    if event_type == "message_update":
        assistant = obj.get("assistantMessageEvent")
        if isinstance(assistant, dict):
            inner = assistant.get("type", "")
            if inner == "text_delta":
                delta = assistant.get("delta", "")
                return (delta, bool(delta), None) if delta else ("", False, None)
            if inner == "text_end":
                content = assistant.get("content", "")
                return ("", False, content) if content else ("", False, None)
            if inner in ("thinking_start", "thinking_delta", "thinking_end",
                         "toolcall_start", "toolcall_delta", "toolcall_end",
                         "text_start"):
                return "", False, None
            content_text = _extract_text_from_content(assistant.get("content"))
            if content_text:
                return "", False, content_text
            return "", False, None
        return "", False, None

    if event_type in ("message_start", "message_end", "turn_end"):
        message = obj.get("message")
        text = _extract_text_from_assistant_message(message)
        if text:
            return "", False, text
        return "", False, None

    if event_type == "agent_end":
        text = _extract_last_assistant_message_text(obj.get("messages"))
        if text:
            return "", False, text
        return "", False, None

    if event_type in ("session", "agent_start", "turn_start",
                       "tool_execution_start", "tool_execution_update",
                       "tool_execution_end"):
        return "", False, None

    # Fallback
    for key in ("content", "text", "delta"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val, True, None
    return "", False, None


def extract_pi_text(raw: str) -> str:
    """Extract user-facing text from ``pi --mode json`` output.

    Parses a JSON-stream (one JSON object per line) and assembles the
    final text content from delta events and complete blocks. Prefers
    complete blocks (from ``text_end``, ``agent_end``) over accumulated
    text deltas.
    """
    delta_parts: list[str] = []
    complete_blocks: list[str] = []

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stream_text, _, complete_text = parse_pi_json_line(stripped)
        if stream_text is None and complete_text is None:
            continue  # not valid JSON
        if complete_text is not None:
            complete_blocks.append(complete_text)
        elif stream_text:
            delta_parts.append(stream_text)

    # Prefer complete blocks (agent_end, text_end) over accumulated deltas
    if complete_blocks:
        return complete_blocks[-1]
    return "".join(delta_parts)


def _extract_text_from_content(content) -> Optional[str]:
    """Extract text from a content field which may be a string or list."""
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text", "")
                if t:
                    parts.append(t)
        return "".join(parts) if parts else None
    return None


def _extract_text_from_assistant_message(message) -> Optional[str]:
    """Extract text from an assistant message dict."""
    if not isinstance(message, dict):
        return None
    return _extract_text_from_content(message.get("content"))


def _extract_last_assistant_message_text(messages) -> Optional[str]:
    """Extract text from the last assistant message in a list of messages."""
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            text = _extract_text_from_content(msg.get("content"))
            if text:
                return text
    return None
