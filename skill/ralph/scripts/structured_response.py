"""Structured Pi response parsing for Ralph.

This module extracts human-readable summary text and action instructions from
JSON-only Pi responses when the streaming parser cannot recover plain text.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import shlex
from typing import Any


@dataclass(frozen=True)
class StructuredAction:
    """An actionable instruction extracted from a structured Pi response."""

    command: str
    args: tuple[str, ...] = ()
    raw: dict[str, Any] | None = None

    def render(self) -> str:
        if self.args:
            return f"{self.command} {' '.join(self.args)}"
        return self.command


@dataclass(frozen=True)
class StructuredResponse:
    """Structured response parsed from JSON-only model output."""

    text: str
    summary: str = ""
    actions: tuple[StructuredAction, ...] = ()

    def render(self) -> str:
        """Render the main user-facing text plus any trailing actions."""
        parts: list[str] = []
        if self.text:
            parts.append(self.text)
        elif self.summary:
            parts.append(self.summary)
        if self.actions:
            parts.append("Structured remediation actions:")
            for action in self.actions:
                parts.append(f"- {action.render()}")
        return "\n".join(parts).strip()

    def remediation_hint(self) -> str:
        """Render the concise summary and actions for the next implement pass."""
        parts: list[str] = []
        if self.summary and self.summary != self.text:
            parts.append(self.summary)
        elif self.text and not self.text.lower().startswith("ready to close:"):
            parts.append(self.text)
        if self.actions:
            if parts:
                parts.append("")
            parts.append("Structured remediation actions:")
            for action in self.actions:
                parts.append(f"- {action.render()}")
        return "\n".join(parts).strip()


def _iter_json_documents(raw: str) -> list[Any]:
    documents: list[Any] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            documents.append(json.loads(stripped))
        except (json.JSONDecodeError, ValueError):
            continue
    return documents


def _walk_values(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)


def _normalize_args(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part for part in shlex.split(value) if part)
    if isinstance(value, (list, tuple)):
        args: list[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                args.append(text)
        return tuple(args)
    if isinstance(value, dict):
        items: list[str] = []
        for key, item in value.items():
            if item is None:
                continue
            text = str(item).strip()
            if text:
                items.append(f"{key}={text}")
        return tuple(items)
    text = str(value).strip()
    return (text,) if text else ()


def _coerce_action(item: Any) -> StructuredAction | None:
    if isinstance(item, str):
        command = item.strip()
        if not command:
            return None
        return StructuredAction(command=command)
    if not isinstance(item, dict):
        return None

    command = item.get("command") or item.get("type") or item.get("name") or item.get("tool")
    if not isinstance(command, str) or not command.strip():
        return None

    args_value = item.get("args")
    if args_value is None:
        args_value = item.get("arguments")
    if args_value is None:
        args_value = item.get("argv")
    if args_value is None:
        args_value = item.get("params")

    return StructuredAction(command=command.strip(), args=_normalize_args(args_value), raw=item)


def _is_user_message(node: Any) -> bool:
    """Return True if *node* looks like a user message that should be skipped."""
    if not isinstance(node, dict):
        return False
    return node.get("role") == "user"


def _extract_text_candidates(node: Any) -> list[str]:
    """Extract text/content strings from *node*, skipping user messages."""
    candidates: list[str] = []
    if not isinstance(node, dict):
        return candidates
    if _is_user_message(node):
        return candidates
    for key in ("text", "content"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    return candidates


def _extract_summary_candidates(node: Any) -> list[str]:
    """Extract summary/message strings from *node*, skipping user messages."""
    candidates: list[str] = []
    if not isinstance(node, dict):
        return candidates
    if _is_user_message(node):
        return candidates
    for key in ("summary", "message"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    return candidates


def parse_structured_response(raw: str) -> StructuredResponse | None:
    """Parse a JSON-only Pi response into a summary and actions.

    The parser looks for expected structured fields such as `summary`, `text`,
    and `actions` across top-level and nested JSON objects. It is tolerant of
    streamed JSON lines, message envelopes, and nested assistant payloads.
    """

    documents = _iter_json_documents(raw)
    if not documents:
        return None

    text_candidates: list[str] = []
    summary_candidates: list[str] = []
    actions: list[StructuredAction] = []

    for document in documents:
        for node in _walk_values(document):
            if not isinstance(node, dict):
                continue
            text_candidates.extend(_extract_text_candidates(node))
            summary_candidates.extend(_extract_summary_candidates(node))

            action_values = node.get("actions")
            if action_values is not None:
                if isinstance(action_values, list):
                    for item in action_values:
                        action = _coerce_action(item)
                        if action:
                            actions.append(action)
                else:
                    action = _coerce_action(action_values)
                    if action:
                        actions.append(action)

            single_action = node.get("action")
            if single_action is not None:
                action = _coerce_action(single_action)
                if action:
                    actions.append(action)

    # Deduplicate while preserving order.
    seen_text: set[str] = set()
    deduped_text: list[str] = []
    for candidate in text_candidates:
        if candidate in seen_text:
            continue
        seen_text.add(candidate)
        deduped_text.append(candidate)

    seen_summary: set[str] = set()
    deduped_summary: list[str] = []
    for candidate in summary_candidates:
        if candidate in seen_summary:
            continue
        seen_summary.add(candidate)
        deduped_summary.append(candidate)

    if deduped_text:
        text = deduped_text[0]
    elif deduped_summary:
        text = deduped_summary[0]
    elif actions:
        text = "\n".join(action.render() for action in actions)
    else:
        text = ""

    summary = deduped_summary[0] if deduped_summary else text
    if not summary and actions:
        summary = "\n".join(action.render() for action in actions)

    deduped_actions: list[StructuredAction] = []
    seen_actions: set[tuple[str, tuple[str, ...]]] = set()
    for action in actions:
        key = (action.command, action.args)
        if key in seen_actions:
            continue
        seen_actions.add(key)
        deduped_actions.append(action)

    if not text and not summary and not deduped_actions:
        return None

    return StructuredResponse(text=text, summary=summary, actions=tuple(deduped_actions))
