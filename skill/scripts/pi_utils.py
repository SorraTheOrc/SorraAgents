#!/usr/bin/env python3
"""Shared Pi JSON-stream parsing utilities.

Provides functions for extracting user-facing text from the JSON-stream
output produced by ``pi -p --mode json``. This matches the pattern used
in ``skill/audit/scripts/audit_runner.py`` and other skills that invoke Pi
non-interactively.

Usage:
    from scripts.pi_utils import extract_pi_text, extract_pi_text_with_fallback

    raw = subprocess.run(..., capture_output=True).stdout
    text = extract_pi_text(raw)

``extract_pi_text_with_fallback`` runs the canonical extractor first and only
on an empty result scans the raw stream for user-facing text payloads in
alternative shapes (e.g. a string-encoded ``assistantMessageEvent``), so
callers can distinguish "the answer was delivered in a shape the parser
missed" from "the model genuinely produced no answer".
"""  # noqa: EXE001

from __future__ import annotations

import json

__all__ = [
    "extract_pi_text",
    "extract_pi_text_with_fallback",
    "parse_pi_json_line",
]


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
        # Only extract text from assistant messages. The user prompt echo
        # (message_start with role=user) must never be treated as model
        # output — it caused false parse failures when the model errored out
        # mid-stream and the prompt was the last complete text block.
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return "", False, None
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


def extract_pi_text_with_fallback(raw: str) -> str:
    """Extract user-facing text from ``pi --mode json`` output, with a recovery
    pass for alternative stream shapes.

    Runs the canonical :func:`extract_pi_text` first; its result is returned
    unchanged whenever it is non-empty, so parsing for every known pi output
    format is preserved byte-for-byte. Only when the primary extractor finds
    nothing does the recovery pass scan the raw stream for user-facing text
    payloads the canonical parser may have missed:

    - ``assistantMessageEvent`` delivered as a JSON-encoded **string** instead
      of a nested object;
    - assistant ``message.content`` / ``agent_end.messages[].content`` parts the
      primary parser skipped;
    - unrecognised events whose direct ``content`` / ``text`` / ``delta`` field
      holds non-empty assistant-visible text.

    The recovery only collects text from assistant-generated events (never the
    ``session`` preamble, never user/system messages) and returns the LAST
    non-empty candidate, mirroring the complete-blocks-wins preference of
    :func:`extract_pi_text`. It never raises and returns ``''`` when no text can
    be recovered, so callers keep their existing empty-output fallback
    behaviour unchanged (a call that genuinely produced no answer still records
    its empty result and lets the caller's retry/diagnostic path take over).
    """
    primary = extract_pi_text(raw)
    if primary:
        return primary
    if not raw:
        return ""
    candidates: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        _collect_pi_stream_candidates(obj, candidates)
    return candidates[-1] if candidates else ""


def _collect_pi_stream_candidates(obj: dict, out: list[str]) -> None:
    """Append user-facing text candidates from one pi JSON-stream line.

    Mirrors the event whitelist of :func:`parse_pi_json_line` but tolerates
    alternative shapes (notably a string-encoded ``assistantMessageEvent``) and
    extracts from ``content`` on the same assistant-only events, so the
    recovery pass can never surface the ``session`` preamble, tool scaffolding
    or user/system text.
    """
    event_type = obj.get("type", "")
    if event_type in ("session", "agent_start", "turn_start",
                      "tool_execution_start", "tool_execution_update",
                      "tool_execution_end"):
        return

    if event_type == "message_update":
        assistant = obj.get("assistantMessageEvent")
        if isinstance(assistant, dict):
            inner = assistant.get("type", "")
            if inner in ("thinking_start", "thinking_delta", "thinking_end",
                         "toolcall_start", "toolcall_delta", "toolcall_end",
                         "text_start"):
                return
            if inner == "text_delta":
                delta = assistant.get("delta", "")
                if isinstance(delta, str) and delta:
                    out.append(delta)
                return
            _append_text_content(assistant.get("content"), out)
        elif isinstance(assistant, str):
            try:
                parsed = json.loads(assistant)
            except (json.JSONDecodeError, ValueError):
                return
            if isinstance(parsed, dict):
                _collect_pi_stream_candidates(
                    {"type": event_type, "assistantMessageEvent": parsed},
                    out,
                )
        return

    if event_type in ("message_start", "message_end", "turn_end"):
        message = obj.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            _append_text_content(message.get("content"), out)
        return

    if event_type == "agent_end":
        messages = obj.get("messages")
        if isinstance(messages, list):
            for msg in reversed(messages):
                if (isinstance(msg, dict)
                        and msg.get("role") == "assistant"
                        and _append_text_content(msg.get("content"), out)):
                    return
        return

    # Fallback for unrecognised event types carrying a direct text field.
    for key in ("content", "text", "delta"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            out.append(val)
            return


def _append_text_content(content, out: list[str]) -> bool:
    """Append text extracted from a content field. Returns True when appended."""
    if isinstance(content, str) and content:
        out.append(content)
        return True
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text:
                    parts.append(text)
        if parts:
            out.append("".join(parts))
            return True
    return False


def _extract_text_from_content(content) -> str | None:
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


def _extract_text_from_assistant_message(message) -> str | None:
    """Extract text from an assistant message dict."""
    if not isinstance(message, dict):
        return None
    return _extract_text_from_content(message.get("content"))


def _extract_last_assistant_message_text(messages) -> str | None:
    """Extract text from the last assistant message in a list of messages."""
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            text = _extract_text_from_content(msg.get("content"))
            if text:
                return text
    return None
