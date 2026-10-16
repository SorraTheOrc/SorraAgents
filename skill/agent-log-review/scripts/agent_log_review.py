"""Agent log review — parse and analyse pi session logs (skill implementation).

This module is the implementation of the ``agent-log-review`` skill. It is
built in layers (epic: SA-0MUEKTJRD0010CGC):

1. **Parser** — read pi v3 session-log JSONL files and extract structured
   data (tool calls, tool results, assistant thinking/text, usage).
2. **Heuristics** — deterministic confusion-pattern extraction.
3. **LLM classification** — a single distilled-signal ``pi`` call.
4. **Report rendering** — deterministic Markdown/JSON output.
5. **Worklog integration** — file recommendations as comments + child items.

Pi v3 session format (consumed here): a JSONL file under
``~/.pi/agent/sessions/<project-slug>/`` whose lines are one of:

- ``{"type": "session", "version": 3, "id": ..., "timestamp": ..., "cwd": ...}``
- ``{"type": "model_change", ..., "provider": ..., "modelId": ...}``
- ``{"type": "thinking_level_change", ..., "thinkingLevel": ...}``
- ``{"type": "message", ..., "message": {"role": ..., "content": [...parts...]}}``

``content`` is a list of parts: ``{"type": "thinking", "thinking": str}``,
``{"type": "toolCall", "id", "name", "arguments"}`` or
``{"type": "text", "text": str}``; ``toolResult`` messages carry
``toolCallId``/``toolName``/``isError`` plus a text-part ``content``.

Privacy: log content is read but never persisted outside ``.worklog/tmp/``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_SESSION_DIR = "PI_CODING_AGENT_SESSION_DIR"
"""Environment variable that overrides the default Pi session directory
root (mirrors the audit skill's ``audit_runner`` helper)."""

DEFAULT_SESSION_ROOT = Path.home() / ".pi" / "agent" / "sessions"
"""Default session store root when the env override is absent."""

DEFAULT_MAX_SESSIONS = 20
"""Default number of most-recent session logs to analyse (parent AC 1)."""


def project_slug(cwd: str | Path) -> str:
    """Derive the session-store subdirectory slug for *cwd*.

    Mirrors pi's ``getDefaultSessionDirPath`` encoder: strip the leading path
    separator, replace every ``/`` (or ``\\`` / ``:``) with ``-``, and wrap the
    result in ``--``. ``/home/operator/project`` →
    ``--home-operator-project--``. Deterministic: the same *cwd* always
    produces the same slug.
    """
    resolved = os.path.abspath(os.fspath(cwd))
    safe = re.sub(r"^[/\\]", "", resolved)
    safe = re.sub(r"[/\\:]", "-", safe)
    return f"--{safe}--"


def get_session_root() -> Path:
    """Return the session-store root directory.

    Honours ``PI_CODING_AGENT_SESSION_DIR`` when present; otherwise defaults
    to ``~/.pi/agent/sessions/``.
    """
    override = os.environ.get(ENV_SESSION_DIR)
    if override:
        return Path(override)
    return DEFAULT_SESSION_ROOT


def session_dir_for(cwd: str | Path, session_root: Path | None = None) -> Path:
    """Return the session directory for a project *cwd* under *session_root*."""
    root = Path(session_root) if session_root is not None else get_session_root()
    return root / project_slug(cwd)


def find_session_files(
    session_dir: Path | str,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> list[Path]:
    """Return the *max_sessions* most-recent ``*.jsonl`` files in *session_dir*.

    Files are sorted by modification time (most recent first); files sharing
    an mtime are tie-broken by name descending so the result is fully
    deterministic. A missing directory returns an empty list.
    """
    d = Path(session_dir)
    if not d.is_dir():
        return []
    files = [p for p in d.glob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    if max_sessions and max_sessions > 0:
        files = files[:max_sessions]
    return files


@dataclass
class ToolCall:
    """A ``toolCall`` part of an assistant message.

    Attributes:
        filename: The session file this call came from.
        seq: Position in the file (shared counter with results/text).
        id: The tool call id (links to the corresponding toolResult).
        name: The tool invoked (e.g. ``bash`` or ``read``).
        arguments: Raw arguments as stored (usually a dict or string).
        timestamp: Message timestamp (ISO string), may be None.
    """

    filename: str
    seq: int
    id: str = ""
    name: str = ""
    arguments: Any = None
    timestamp: str | None = None


@dataclass
class ToolResult:
    """A ``toolResult`` message following one or more tool calls.

    Attributes:
        filename: The session file this result came from.
        seq: Position in the file (shared counter with calls/text).
        tool_call_id: The toolCall id this result answers.
        tool_name: The tool that produced this result.
        content: Concatenated text-part content of the result.
        is_error: Whether the tool marked the result as an error.
        timestamp: Message timestamp (ISO string), may be None.
    """

    filename: str
    seq: int
    tool_call_id: str = ""
    tool_name: str = ""
    content: str = ""
    is_error: bool = False
    timestamp: str | None = None


@dataclass
class ThinkingBlock:
    """A ``thinking`` part of an assistant message (private chain-of-thought)."""

    filename: str
    seq: int
    text: str = ""
    timestamp: str | None = None


@dataclass
class TextBlock:
    """A ``text`` part of an assistant message (visible answer text)."""

    filename: str
    seq: int
    text: str = ""
    timestamp: str | None = None


@dataclass
class ParsedSession:
    """Structured view of one parsed session file.

    Attribute ``events`` holds tool calls and results **in file order**
    (needed by retry/self-correction heuristics); ``tool_calls`` /
    ``tool_results`` filter that list for convenience.
    """

    filename: str
    path: str
    session_id: str | None
    cwd: str | None
    timestamp: str | None
    version: int | None
    model: dict[str, Any] | None
    messages: int = 0
    thinking: list[ThinkingBlock] = field(default_factory=list)
    text: list[TextBlock] = field(default_factory=list)
    events: list[ToolCall | ToolResult] = field(default_factory=list)
    malformed_lines: int = 0
    usage: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "output": 0, "totalTokens": 0}
    )
    has_usage: bool = False

    @property
    def tool_calls(self) -> list[ToolCall]:
        """All tool calls in this session, in file order."""
        return [e for e in self.events if isinstance(e, ToolCall)]

    @property
    def tool_results(self) -> list[ToolResult]:
        """All tool results in this session, in file order."""
        return [e for e in self.events if isinstance(e, ToolResult)]


@dataclass
class SessionScan:
    """Result of analysing a project's session directory."""

    cwd: str
    session_dir: str
    files_found: int
    files_analysed: int
    malformed_lines: int
    missing_usage: int
    sessions: list[ParsedSession] = field(default_factory=list)


def _content_to_text(content: Any) -> str:
    """Flatten a message ``content`` (parts list or plain string) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
        return "\n".join(pieces)
    return ""


def parse_session(path: Path | str) -> ParsedSession:
    """Parse a single v3 session JSONL file into a :class:`ParsedSession`.

    Fail-soft behaviour:

    - A line that is not valid JSON is skipped and counts towards
      ``malformed_lines`` (the parse continues).
    - A file with no ``session`` header line yields empty ids/cwd (never
      crashes).
    - Assistant messages without a ``usage`` block keep the zeroed usage
      dict and leave ``has_usage`` False (degradation counter is maintained
      by :func:`read_sessions`).
    """
    path = Path(path)
    filename = path.name
    seq = 0
    malformed = 0
    messages = 0
    session_id = None
    cwd = None
    timestamp = None
    session_version = None
    model: dict[str, Any] | None = None
    thinking: list[ThinkingBlock] = []
    text: list[TextBlock] = []
    events: list[ToolCall | ToolResult] = []
    usage_totals = {"input": 0, "output": 0, "totalTokens": 0}
    has_usage = False

    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        # Unreadable file degrades to a zeroed session + one malformed line.
        return ParsedSession(
            filename=filename,
            path=str(path),
            session_id=None,
            cwd=None,
            timestamp=None,
            version=None,
            model=None,
            malformed_lines=1,
            usage=usage_totals,
            has_usage=False,
        )

    with fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            if not isinstance(entry, dict):
                malformed += 1
                continue

            etype = entry.get("type")
            if etype == "session":
                session_id = entry.get("id") or None
                cwd = entry.get("cwd") or None
                timestamp = entry.get("timestamp") or None
                session_version = entry.get("version")
            elif etype == "model_change":
                model = {
                    "provider": entry.get("provider"),
                    "modelId": entry.get("modelId"),
                }
            elif etype == "message":
                messages += 1
                msg = entry.get("message") or {}
                if not isinstance(msg, dict):
                    malformed += 1
                    continue
                role = msg.get("role")
                ts = msg.get("timestamp") or entry.get("timestamp")

                if role == "assistant":
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        has_usage = True
                        for key in ("input", "output", "totalTokens"):
                            value = usage.get(key)
                            if isinstance(value, (int, float)):
                                usage_totals[key] += int(value)
                    content = msg.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if not isinstance(part, dict):
                                continue
                            ptype = part.get("type")
                            if ptype == "thinking":
                                seq += 1
                                thinking.append(
                                    ThinkingBlock(
                                        filename=filename,
                                        seq=seq,
                                        text=part.get("thinking", "") or "",
                                        timestamp=ts,
                                    )
                                )
                            elif ptype == "toolCall":
                                seq += 1
                                events.append(
                                    ToolCall(
                                        filename=filename,
                                        seq=seq,
                                        id=part.get("id", "") or "",
                                        name=part.get("name", "") or "",
                                        arguments=part.get("arguments"),
                                        timestamp=ts,
                                    )
                                )
                            elif ptype == "text":
                                seq += 1
                                text.append(
                                    TextBlock(
                                        filename=filename,
                                        seq=seq,
                                        text=part.get("text", "") or "",
                                        timestamp=ts,
                                    )
                                )
                elif role == "toolResult":
                    seq += 1
                    events.append(
                        ToolResult(
                            filename=filename,
                            seq=seq,
                            tool_call_id=msg.get("toolCallId", "") or "",
                            tool_name=msg.get("toolName", "") or "",
                            content=_content_to_text(msg.get("content")),
                            is_error=bool(msg.get("isError")),
                            timestamp=ts,
                        )
                    )

    return ParsedSession(
        filename=filename,
        path=str(path),
        session_id=session_id,
        cwd=cwd,
        timestamp=timestamp,
        version=session_version,
        model=model,
        messages=messages,
        thinking=thinking,
        text=text,
        events=events,
        malformed_lines=malformed,
        usage=usage_totals,
        has_usage=has_usage,
    )


def read_sessions(
    cwd: str | Path | None = None,
    session_root: Path | None = None,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> SessionScan:
    """Analyse the *max_sessions* most-recent session logs for a project.

    Args:
        cwd: Project working directory used to derive the session-store slug
            (defaults to the current working directory).
        session_root: Session store root (defaults to
            :func:`get_session_root`).
        max_sessions: How many of the most-recent session files to parse.

    Returns:
        A :class:`SessionScan` with per-session structured data plus fail-soft
        counters (``malformed_lines`` global tally and ``missing_usage`` —
        the number of sessions whose assistant messages carried no usage
        block).
    """
    cwd_str = os.path.abspath(os.fspath(cwd) if cwd is not None else os.getcwd())
    sdir = session_dir_for(cwd_str, session_root)
    files = find_session_files(sdir, max_sessions=max_sessions)

    sessions: list[ParsedSession] = []
    malformed_total = 0
    missing_usage = 0
    for f in files:
        parsed = parse_session(f)
        malformed_total += parsed.malformed_lines
        if not parsed.has_usage:
            missing_usage += 1
        sessions.append(parsed)

    return SessionScan(
        cwd=cwd_str,
        session_dir=str(sdir),
        files_found=len(files),
        files_analysed=len(sessions),
        malformed_lines=malformed_total,
        missing_usage=missing_usage,
        sessions=sessions,
    )
