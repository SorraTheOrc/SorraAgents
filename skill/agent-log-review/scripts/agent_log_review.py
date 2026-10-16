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
import subprocess
from collections import Counter
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


# ══════════════════════════════════════════════════════════════════════════
# Layer 2 — heuristic confusion-pattern extraction
# (SA-0MV6UDSAF0064B85)
# ══════════════════════════════════════════════════════════════════════════

THINKING_KEYWORDS: tuple[str, ...] = (
    "error", "fail", "retry", "incorrect", "sorry", "wrong", "fix",
    "correct", "issue", "problem", "confused", "wait", "actually",
    "oh", "hmm", "ah",
)
"""Keywords signalling confusion in thinking content (configurable)."""

EXCERPT_BYTES_PER_SESSION = 2048
"""Maximum bytes of thinking-context excerpt kept per session (prompt budget)."""

CONTEXT_RADIUS = 120
"""Characters of context kept either side of a keyword hit (mitigates false
positives by giving the LLM evidence to confirm/deny)."""

REPEATED_CALL_MIN_COUNT = 2
"""Identical tool call occurrences within a session that count as a retry."""

ERROR_MODIFY_WINDOW = 3
"""Events after an error result to scan for a same-tool modified call."""

TOOL_ERROR_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("traceback", re.compile(r"traceback|exception|error", re.IGNORECASE)),
    ("permission_denied", re.compile(r"permission denied", re.IGNORECASE)),
    ("command_not_found", re.compile(r"command not found|no such file|\bnot found\b", re.IGNORECASE)),
    ("shell_expansion", re.compile(r"unexpected token|bad substitution|ambiguous redirect|syntax error near", re.IGNORECASE)),
    ("import_error", re.compile(r"importerror|modulenotfounderror|no module named", re.IGNORECASE)),
    ("python_error", re.compile(r"nameerror|keyerror|typeerror|valueerror|attributeerror|indentationerror", re.IGNORECASE)),
)
"""(kind, regex) pairs used to classify tool-result error content."""


def _truncate_utf8(text: str, cap_bytes: int) -> str:
    """Truncate *text* (plus an ellipsis marker) to fit in *cap_bytes* bytes."""
    marker = "…"
    if len(text.encode("utf-8")) + len(marker.encode("utf-8")) <= cap_bytes:
        return text
    target = cap_bytes - len(marker.encode("utf-8"))
    while text and len(text.encode("utf-8")) > target:
        text = text[:-1]
    return text + marker


def _context_around(text: str, index: int, radius: int = CONTEXT_RADIUS) -> str:
    """Return a centred context window around *index* in *text*."""
    start = max(0, index - radius)
    end = min(len(text), index + radius)
    return text[start:end].replace("\n", " ")


def _canonical_arguments(arguments: Any) -> str:
    """Deterministic string form of tool-call arguments (dict keys sorted)."""
    if isinstance(arguments, dict):
        return json.dumps(arguments, sort_keys=True, default=str)
    if arguments is None:
        return "null"
    return str(arguments)


def _tool_error_kind(content: str) -> str | None:
    """First matching error-pattern kind, or None when no pattern matches."""
    for kind, pattern in TOOL_ERROR_PATTERNS:
        if pattern.search(content or ""):
            return kind
    return None


def _session_has_error(session: ParsedSession) -> bool:
    """True when the session contains any error-flagged or error-pattern result."""
    for tr in session.tool_results:
        if tr.is_error or _tool_error_kind(tr.content) is not None:
            return True
    return False


def extract_tool_distribution(
    sessions: list[ParsedSession],
) -> dict[str, Any]:
    """Count tool calls across *sessions* and per-tool error rates.

    Returns ``{"total_calls", "total_error_results", "by_tool": {tool: {"calls",
    "error_results", "error_rate"}}}``. A result counts as an error when the
    ``toolResult`` has ``isError`` set OR its content matches a
    :data:`TOOL_ERROR_PATTERNS` pattern. Deterministic: ``by_tool`` is sorted
    by tool name.
    """
    calls: Counter = Counter()
    errors: Counter = Counter()
    for session in sessions:
        for tc in session.tool_calls:
            calls[tc.name] += 1
        for tr in session.tool_results:
            if tr.is_error or _tool_error_kind(tr.content) is not None:
                errors[tr.tool_name] += 1
    by_tool: dict[str, dict[str, Any]] = {}
    for name in sorted(calls):
        call_count = calls[name]
        error_count = errors[name]
        by_tool[name] = {
            "calls": call_count,
            "error_results": error_count,
            "error_rate": round(error_count / call_count, 3) if call_count else 0.0,
        }
    return {
        "total_calls": sum(calls.values()),
        "total_error_results": sum(errors.values()),
        "by_tool": by_tool,
    }


def extract_thinking_signals(
    sessions: list[ParsedSession],
    keywords: tuple[str, ...] = THINKING_KEYWORDS,
    excerpt_cap: int = EXCERPT_BYTES_PER_SESSION,
) -> dict[str, Any]:
    """Extract keyword-based confusion signals from assistant thinking content.

    For each (session, keyword) pair a hit count and a capped context-window
    excerpt are produced. The context window (see :data:`CONTEXT_RADIUS`)
    keeps legitimate uses (e.g. "error handling") visible for the LLM to
    confirm, and the per-session excerpt is capped at *excerpt_cap* bytes to
    bound the LLM prompt (parent AC 2 false-positive mitigation).

    Deterministic: hits are ordered by (session, keyword).
    """
    total_hits = 0
    hits: list[dict[str, Any]] = []
    for session in sessions:
        counts: Counter = Counter()
        per_keyword: dict[str, list[str]] = {
            kw: [] for kw in keywords
        }
        for block in session.thinking:
            lowered = (block.text or "").lower()
            for kw in keywords:
                needle = kw.lower()
                pos = 0
                while True:
                    idx = lowered.find(needle, pos)
                    if idx < 0:
                        break
                    counts[kw] += 1
                    per_keyword[kw].append(_context_around(block.text, idx))
                    pos = idx + len(needle)
        total_hits += sum(counts.values())
        budget = excerpt_cap
        for kw in sorted(counts):
            joined = " | ".join(per_keyword[kw])
            size = len(joined.encode("utf-8"))
            if size > budget:
                joined = _truncate_utf8(joined, budget)
                size = budget
            budget = max(0, budget - size)
            hits.append({
                "session": session.filename,
                "keyword": kw,
                "count": counts[kw],
                "context": joined,
            })
    hits.sort(key=lambda h: (h["session"], h["keyword"]))
    return {"total_hits": total_hits, "hits": hits}


def extract_tool_errors(
    sessions: list[ParsedSession],
    max_excerpt_bytes: int = EXCERPT_BYTES_PER_SESSION,
) -> dict[str, Any]:
    """Extract error signals from ``toolResult`` messages.

    A result is an error signal when its ``isError`` flag is set OR its
    content matches a :data:`TOOL_ERROR_PATTERNS` pattern (e.g. tracebacks,
    "permission denied", "command not found", shell-expansion errors). Each
    entry carries a capped excerpt and the matched ``kind`` ("unclassified"
    when only the flag fired). Deterministic: sorted by (session, tool,
    excerpt).
    """
    errors: list[dict[str, Any]] = []
    for session in sessions:
        for tr in session.tool_results:
            kind = _tool_error_kind(tr.content) if not tr.is_error else (
                _tool_error_kind(tr.content) or "unclassified"
            )
            if kind is None:
                continue
            excerpt = tr.content.replace("\n", " ").strip()
            if len(excerpt.encode("utf-8")) > max_excerpt_bytes:
                excerpt = _truncate_utf8(excerpt, max_excerpt_bytes)
            errors.append({
                "session": session.filename,
                "tool": tr.tool_name,
                "kind": kind,
                "is_error": tr.is_error,
                "excerpt": excerpt,
            })
    errors.sort(key=lambda e: (e["session"], e["tool"], e["excerpt"]))
    return {"total": len(errors), "errors": errors}


def extract_retry_patterns(
    sessions: list[ParsedSession],
) -> dict[str, Any]:
    """Detect retry / self-correction patterns per session.

    Two kinds:

    - ``repeated_call`` — the same tool was called with identical
      (canonicalised) arguments at least :data:`REPEATED_CALL_MIN_COUNT`
      times.
    - ``call_error_modify`` — a tool call returned an error result and the
      same tool was called again with *different* arguments within
      :data:`ERROR_MODIFY_WINDOW` events.

    Deterministic: patterns are sorted by (session, tool, arguments, count).
    """
    patterns: list[dict[str, Any]] = []
    for session in sessions:
        # Repeated identical calls.
        per_key: Counter = Counter()
        for tc in session.tool_calls:
            per_key[(tc.name, _canonical_arguments(tc.arguments))] += 1
        for (name, args), count in sorted(per_key.items()):
            if count >= REPEATED_CALL_MIN_COUNT:
                patterns.append({
                    "session": session.filename,
                    "kind": "repeated_call",
                    "tool": name,
                    "arguments": args,
                    "count": count,
                })
        # Call → error → modified call.
        events: list[ToolCall | ToolResult] = session.events
        last_call: dict[str, ToolCall] = {}
        for i, event in enumerate(events):
            if isinstance(event, ToolCall):
                last_call[event.name] = event
            elif (
                isinstance(event, ToolResult)
                and event.is_error
                and event.tool_name in last_call
            ):
                original = last_call[event.tool_name]
                for j in range(i + 1, min(i + 1 + ERROR_MODIFY_WINDOW, len(events))):
                    nxt = events[j]
                    if isinstance(nxt, ToolCall) and nxt.name == original.name:
                        if _canonical_arguments(nxt.arguments) != _canonical_arguments(
                            original.arguments
                        ):
                            patterns.append({
                                "session": session.filename,
                                "kind": "call_error_modify",
                                "tool": original.name,
                                "original_arguments": _canonical_arguments(original.arguments),
                                "modified_arguments": _canonical_arguments(nxt.arguments),
                            })
                        break
    patterns.sort(
        key=lambda p: (
            p["session"],
            p["tool"],
            p.get("original_arguments") or p.get("arguments", ""),
            p.get("count", 0),
        )
    )
    return {"total": len(patterns), "patterns": patterns}


def extract_signals(
    sessions: list[ParsedSession],
    max_sessions: int | None = None,
    errors_only: bool = False,
) -> dict[str, Any]:
    """Bundle all heuristic extractions into one deterministic signals dict.

    This is the distilled input handed to the LLM classification layer
    (Layer 3). Flags (deterministic, for CLI mapping):

    - ``max_sessions`` — keep only the first N sessions (already ordered
      newest-first by :func:`read_sessions`).
    - ``errors_only`` — keep only sessions containing at least one error
      result (``isError`` or error-pattern content).
    """
    result_sessions = list(sessions)
    if max_sessions is not None and max_sessions >= 0:
        result_sessions = result_sessions[:max_sessions]
    if errors_only:
        result_sessions = [s for s in result_sessions if _session_has_error(s)]
    return {
        "session_count": len(result_sessions),
        "session_files": sorted(s.filename for s in result_sessions),
        "tool_distribution": extract_tool_distribution(result_sessions),
        "thinking_signals": extract_thinking_signals(result_sessions),
        "tool_errors": extract_tool_errors(result_sessions),
        "retry_patterns": extract_retry_patterns(result_sessions),
    }


# ══════════════════════════════════════════════════════════════════════════
# Layer 3 — LLM-assisted pattern classification
# (SA-0MV6UETWV002CF5T)
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_CLASSIFIER_MODEL = os.environ.get(
    "AGENT_LOG_REVIEW_MODEL", "Local Proxy/code"
)
"""Model used for classification (configurable via env or ``--model``)."""

PROMPT_BUDGET_BYTES = 32 * 1024
"""Documented upper bound for the distilled-signal prompt (parent AC 3)."""

PI_CALL_TIMEOUT = 600
"""Per-call timeout for the classification ``pi`` subprocess."""

SECRET_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "<API_KEY>"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE), "Bearer <TOKEN>"),
    (re.compile(r"\.ssh[/\\][^\s\"']*"), ".ssh/ <redacted>"),
    (re.compile(r"\.env(?:\.local)?\b", re.IGNORECASE), ".env<redacted>"),
)
"""Redaction rules applied to every excerpt/prompt before an LLM call."""


class PiCallError(RuntimeError):
    """Raised when the classification ``pi`` subprocess exits non-zero."""


class ClassificationError(RuntimeError):
    """Raised for prompt-budget overflow (hard error, not a parse failure)."""


def redact_secrets(text: str) -> str:
    """Replace secret-like substrings in *text* with placeholders.

    Covers API-key patterns (``sk-...``), ``Bearer`` tokens, and paths
    mentioning ``.ssh`` / ``.env`` (privacy rule: excerpts never persist raw
    secrets). Deterministic per input.
    """
    if not text:
        return text
    redacted = text
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _default_pi_runner(cmd: list[str]) -> object:
    """Run the classification ``pi`` call, capturing stdout/stderr."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=PI_CALL_TIMEOUT, check=False,
    )


def _call_pi(
    prompt: str,
    model: str = DEFAULT_CLASSIFIER_MODEL,
    runner: Any = None,
    pi_bin: str = "pi",
) -> str:
    """Make a single non-interactive ``pi --print --no-tools`` call.

    Follows the audit skill's subprocess pattern with full context reduction
    (``--no-context-files --no-skills --no-extensions``) so the classifier
    returns plain text and never inherits repo/skill context. *runner* is
    injectable for deterministic tests; it must accept the command list and
    return an object with ``returncode``/``stdout``/``stderr``.
    """
    cmd = [
        pi_bin, "-p", "--print", "--model", model,
        "--no-tools", "--no-context-files", "--no-skills", "--no-extensions",
        prompt,
    ]
    runner_fn = runner if runner is not None else _default_pi_runner
    proc = runner_fn(cmd)
    if getattr(proc, "returncode", 1) != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip()
        raise PiCallError(f"pi classification call failed (rc={proc.returncode}): {stderr[:300]}")
    return proc.stdout or ""


def _build_classification_prompt(signals: dict[str, Any]) -> str:
    """Compose the distilled-signal prompt for the LLM classifier.

    Only counts, distributions and capped excerpts are included — never raw
    session log content. Session file names (redacted-then-sorted) are listed
    up front so model citations map back to sessions. The signals JSON is
    passed through :func:`redact_secrets` before inclusion.
    """
    session_files = sorted(signals.get("session_files") or [])
    count = int(signals.get("session_count", len(session_files)))
    signals_redacted = redact_secrets(json.dumps(signals, indent=1))
    return (
        "You are analysing pi coding-agent session logs for repeated confusion patterns.\n"
        f"Below are distilled heuristic signals from {count} session(s):\n"
        f"Sessions analysed: {', '.join(session_files) if session_files else '(none)'.strip()}\n\n"
        "Classify the signals into confusion-pattern categories and actionable "
        "skill improvements. Respond with ONLY a JSON object of exactly this shape:\n"
        '{"categories": [{"name": str, "confidence": float between 0 and 1, '
        '"evidence_sessions": [str], "summary": str}], '
        '"recommendations": [{"title": str, "issue_type": "chore" or "feature", '
        '"target_skill": str, "rationale": str, "evidence_sessions": [str]}]}\n'
        '"evidence_sessions" entries must be session file names from the '
        "Sessions analysed list above (or [] if none).\n\n"
        "SIGNALS (redacted):\n"
        f"{signals_redacted}\n"
    )


def _parse_classification_response(stdout: str) -> dict[str, Any]:
    """Parse the model's JSON response, tolerating surrounding text/fences."""
    text = (stdout or "").strip()
    if not text:
        raise ClassificationError("model returned an empty response")
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClassificationError(f"model returned malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ClassificationError("model response is not a JSON object")
    return parsed


def classify_patterns(
    signals: dict[str, Any],
    model: str = DEFAULT_CLASSIFIER_MODEL,
    runner: Any = None,
    prompt_budget: int = PROMPT_BUDGET_BYTES,
) -> dict[str, Any]:
    """Classify distilled *signals* into categories and recommendations.

    One batched ``pi --print --no-tools`` call, hybrid design: heuristics
    extracted the signals, the LLM only classifies them. Fail-soft:
    - prompt over *prompt_budget* → raises :class:`ClassificationError`
      (hard error: the signals are too large to send safely);
    - pi subprocess failure or malformed model output → returned as a
      structured ``error`` diagnostic (never a crash), with empty
      categories/recommendations.

    Returns: ``{"error": str|None, "categories": [...],
    "recommendations": [...], "prompt_bytes": int}`` where
    ``categories`` is a list of ``{name, confidence, evidence_sessions,
    summary}`` and ``recommendations`` a list of ``{title, issue_type,
    target_skill, rationale, evidence_sessions}``.
    """
    prompt = _build_classification_prompt(signals)
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > prompt_budget:
        raise ClassificationError(
            f"classification prompt of {prompt_bytes} bytes exceeds the "
            f"{prompt_budget}-byte budget; reduce max_sessions or excerpt size"
        )
    try:
        stdout = _call_pi(prompt, model=model, runner=runner)
        parsed = _parse_classification_response(stdout)
    except PiCallError as exc:
        return {
            "error": str(exc), "categories": [], "recommendations": [],
            "prompt_bytes": prompt_bytes,
        }
    except ClassificationError as exc:
        return {
            "error": str(exc), "categories": [], "recommendations": [],
            "prompt_bytes": prompt_bytes,
        }
    categories = parsed.get("categories") or []
    recommendations = parsed.get("recommendations") or []
    if not isinstance(categories, list) or not isinstance(recommendations, list):
        return {
            "error": "model response missing categories/recommendations lists",
            "categories": [], "recommendations": [], "prompt_bytes": prompt_bytes,
        }
    return {
        "error": None,
        "categories": categories,
        "recommendations": recommendations,
        "prompt_bytes": prompt_bytes,
    }


