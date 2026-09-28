#!/usr/bin/env python3
"""Deterministic pi session-log analyser for the STE100 24h experiment.

This module is the measurement source of truth for work item
``SA-0MSMC8KRS00164VD`` ("24h experiment: AGENTS.md STE100 instruction"). It
reads pi session logs (``~/.pi/agent/sessions/<project-slug>/*.jsonl``),
extracts per-session duration and token/cost metrics, filters sessions into a
time window (by *session start* timestamp), and prints either a
human-readable report or machine-readable JSON.

Design goals
------------

* **Deterministic** — the same session-log directory always produces the same
  output. No wall-clock timestamps, no randomness, and every collection is
  sorted before it is emitted.
* **Fail-soft** — malformed JSONL lines, missing ``usage`` blocks and sessions
  without a header never crash the analysis; they degrade to zeros and are
  reported via counters.
* **Single responsibility** — duration/token/cost aggregation only. Comparison
  narrative and the experiment report are produced elsewhere.

Log format consumed (verified during intake)
--------------------------------------------

Each ``*.jsonl`` file contains one JSON object per line:

* ``{"type": "session", "id": ..., "timestamp": ..., "cwd": ...}`` — the
  session header; its ``timestamp`` is the session start.
* ``{"type": "message", "timestamp": ..., "message": {"role": "assistant",
  "model": ..., "usage": {"input": .., "output": .., "cacheRead": ..,
  "cacheWrite": .., "totalTokens": .., "cost": {...}}}}`` — assistant messages
  carry the usage record.
* ``{"type": "model_change", ...}`` and ``{"type": "thinking_level_change",
  ...}`` — metadata events carrying no usage.

Session end is the maximum timestamp observed in the file. A session that
spans a window boundary is attributed to the window containing its **start**
timestamp (documented rule, AC 7).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"

# Token usage keys recorded by pi on every assistant message. ``totalTokens``
# is derived (and recorded) by pi; we keep it separate from the four base keys.
_TOKEN_KEYS = ("input", "output", "cacheRead", "cacheWrite")
_TOTAL_KEY = "totalTokens"
# Cost may be recorded as an object with these keys, or as a bare number.
_COST_KEYS = ("input", "output", "cacheRead", "cacheWrite", "total")


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def parse_timestamp(value: Any) -> float | None:
    """Parse a pi timestamp into epoch seconds (UTC).

    Accepts ISO-8601 strings (with or without a trailing ``Z``) and numeric
    epoch values in seconds or milliseconds. Returns ``None`` for anything
    unparseable so callers can skip the value rather than crash.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        # pi records numeric message timestamps in milliseconds.
        if abs(seconds) > 1e12:
            seconds /= 1000.0
        return seconds
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def format_timestamp(epoch: float | None) -> str | None:
    """Render epoch seconds as an ISO-8601 UTC string (``Z`` suffix)."""
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def project_from_cwd(cwd: str | None) -> str:
    """Derive a stable project label from a session working directory.

    Uses the first path segment below a ``projects`` directory (so a worktree
    under ``<proj>/.worklog/worktrees/...`` still maps to ``<proj>``), falling
    back to the final path component, then to ``"unknown"``.
    """
    if not cwd:
        return "unknown"
    parts = Path(cwd).parts
    if "projects" in parts:
        index = parts.index("projects")
        if index + 1 < len(parts):
            return parts[index + 1]
    return Path(cwd).name or "unknown"


def _empty_tokens() -> dict[str, int]:
    tokens = {key: 0 for key in _TOKEN_KEYS}
    tokens[_TOTAL_KEY] = 0
    return tokens


# ---------------------------------------------------------------------------
# Session data model
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """Per-session metrics extracted from one JSONL log file."""

    id: str
    path: str
    project: str
    cwd: str
    start: float | None
    end: float | None
    duration_minutes: float
    tokens: dict[str, int] = field(default_factory=_empty_tokens)
    cost: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in _COST_KEYS})
    # model name -> token totals attributed to assistant messages from it.
    model_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    assistant_messages: int = 0
    malformed_lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "cwd": self.cwd,
            "path": self.path,
            "start": format_timestamp(self.start),
            "end": format_timestamp(self.end),
            "duration_minutes": round(self.duration_minutes, 4),
            "assistant_messages": self.assistant_messages,
            "malformed_lines": self.malformed_lines,
            "tokens": {k: self.tokens[k] for k in _TOKEN_KEYS + (_TOTAL_KEY,)},
            "cost": {k: round(self.cost[k], 6) for k in _COST_KEYS},
            "models": sorted(self.model_usage),
        }


def _accumulate_usage(
    tokens: dict[str, int],
    cost: dict[str, float],
    usage: dict[str, Any],
) -> None:
    """Add one assistant ``usage`` record into *tokens* and *cost* accumulators."""
    base_total = 0
    for key in _TOKEN_KEYS:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = 0
        value = int(value)
        tokens[key] += value
        base_total += value

    reported_total = usage.get(_TOTAL_KEY)
    if isinstance(reported_total, bool) or not isinstance(reported_total, (int, float)):
        reported_total = base_total
    tokens[_TOTAL_KEY] += int(reported_total)

    recorded_cost = usage.get("cost")
    if isinstance(recorded_cost, dict):
        recorded_base = 0.0
        for key in _TOKEN_KEYS:
            value = recorded_cost.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cost[key] += float(value)
                recorded_base += float(value)
        total = recorded_cost.get("total")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            cost["total"] += float(total)
        else:
            cost["total"] += recorded_base
    elif isinstance(recorded_cost, (int, float)) and not isinstance(recorded_cost, bool):
        cost["total"] += float(recorded_cost)


def parse_session_file(path: Path) -> Session:
    """Parse one pi session JSONL file into a :class:`Session`."""
    header: dict[str, Any] | None = None
    first_ts: float | None = None
    last_ts: float | None = None
    tokens = _empty_tokens()
    cost: dict[str, float] = {k: 0.0 for k in _COST_KEYS}
    model_usage: dict[str, dict[str, int]] = {}
    assistant_messages = 0
    malformed_lines = 0
    current_model = "unknown"

    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                malformed_lines += 1
                continue
            if not isinstance(event, dict):
                malformed_lines += 1
                continue

            timestamp = parse_timestamp(event.get("timestamp"))
            if timestamp is not None:
                if first_ts is None or timestamp < first_ts:
                    first_ts = timestamp
                if last_ts is None or timestamp > last_ts:
                    last_ts = timestamp

            event_type = event.get("type")
            if event_type == "session" and header is None:
                header = event
            elif event_type == "model_change":
                model_id = event.get("modelId")
                if isinstance(model_id, str) and model_id:
                    current_model = model_id
            elif event_type == "message":
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                if message.get("role") != "assistant":
                    continue
                assistant_messages += 1
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                _accumulate_usage(tokens, cost, usage)
                model = message.get("model")
                if not isinstance(model, str) or not model:
                    model = current_model
                bucket = model_usage.setdefault(model, _empty_tokens())
                for key in _TOKEN_KEYS + (_TOTAL_KEY,):
                    value = usage.get(key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    bucket[key] += int(value)

    start = parse_timestamp((header or {}).get("timestamp"))
    if start is None:
        start = first_ts
    end = last_ts
    duration_minutes = 0.0
    if start is not None and end is not None and end > start:
        duration_minutes = (end - start) / 60.0

    session_id = ""
    cwd = ""
    if header:
        if isinstance(header.get("id"), str):
            session_id = header["id"]
        if isinstance(header.get("cwd"), str):
            cwd = header["cwd"]
    if not session_id:
        session_id = path.stem

    return Session(
        id=session_id,
        path=str(path),
        project=project_from_cwd(cwd),
        cwd=cwd,
        start=start,
        end=end,
        duration_minutes=duration_minutes,
        tokens=tokens,
        cost=cost,
        model_usage=model_usage,
        assistant_messages=assistant_messages,
        malformed_lines=malformed_lines,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def number_stats(values: Iterable[float]) -> dict[str, Any]:
    """Return count/mean/median/min/max/sum for a numeric series.

    Empty input yields ``None`` for the location statistics and ``0`` for the
    sum, so empty windows are representable without special-casing callers.
    """
    series = [value for value in values]
    if not series:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "sum": 0,
        }
    return {
        "count": len(series),
        "mean": round(statistics.mean(series), 4),
        "median": round(statistics.median(series), 4),
        "min": min(series),
        "max": max(series),
        "sum": sum(series),
    }


def aggregate_sessions(sessions: list[Session]) -> dict[str, Any]:
    """Aggregate a list of sessions into the metric block used in reports."""
    return {
        "session_count": len(sessions),
        "duration_minutes": number_stats(s.duration_minutes for s in sessions),
        "total_tokens": number_stats(s.tokens[_TOTAL_KEY] for s in sessions),
        "tokens": {
            key: number_stats(s.tokens[key] for s in sessions)
            for key in _TOKEN_KEYS
        },
        "cost": {
            key: round(sum(s.cost[key] for s in sessions), 6)
            for key in _COST_KEYS
        },
    }


def aggregate_by_project(sessions: list[Session]) -> dict[str, Any]:
    """Per-project metric blocks, keyed by project label (sorted)."""
    projects: dict[str, list[Session]] = {}
    for session in sessions:
        projects.setdefault(session.project, []).append(session)
    return {
        name: aggregate_sessions(projects[name])
        for name in sorted(projects)
    }


def aggregate_by_model(sessions: list[Session]) -> dict[str, Any]:
    """Per-model token blocks, aggregated from assistant-message usage.

    A session that used several models contributes to each model's block; the
    block's ``session_count`` counts distinct sessions that used it.
    """
    totals: dict[str, dict[str, int]] = {}
    session_counts: dict[str, int] = {}
    for session in sessions:
        for model, usage in session.model_usage.items():
            bucket = totals.setdefault(model, _empty_tokens())
            for key in _TOKEN_KEYS + (_TOTAL_KEY,):
                bucket[key] += usage.get(key, 0)
            session_counts[model] = session_counts.get(model, 0) + 1

    models: dict[str, Any] = {}
    for model in sorted(totals):
        bucket = totals[model]
        count = session_counts[model]
        models[model] = {
            "session_count": count,
            "tokens": {key: bucket[key] for key in _TOKEN_KEYS + (_TOTAL_KEY,)},
            "mean_total_tokens": round(bucket[_TOTAL_KEY] / count, 4) if count else None,
        }
    return models


def _session_sort_key(session: Session) -> tuple[Any, ...]:
    """Deterministic ordering: unknown starts first, then by start, then id."""
    if session.start is None:
        return (0, 0.0, session.id)
    return (1, session.start, session.id)


def _within_window(session: Session, window_start: float | None, window_end: float | None) -> bool:
    """Whether a session is attributed to ``[window_start, window_end)``.

    Attribution is by session **start** (AC 7). A session with no start
    timestamp is only counted when the window is unbounded.
    """
    if window_start is None and window_end is None:
        return True
    if session.start is None:
        return False
    if window_start is not None and session.start < window_start:
        return False
    return window_end is None or session.start < window_end


def analyze(
    sessions_dir: Path,
    window_start: float | None = None,
    window_end: float | None = None,
) -> dict[str, Any]:
    """Analyse every session log under *sessions_dir* for the given window."""
    files = sorted(Path(sessions_dir).rglob("*.jsonl"))
    sessions: list[Session] = []
    for path in files:
        try:
            session = parse_session_file(path)
        except OSError:
            continue
        if _within_window(session, window_start, window_end):
            sessions.append(session)
    sessions.sort(key=_session_sort_key)

    report: dict[str, Any] = {
        "sessions_dir": str(Path(sessions_dir)),
        "window": {
            "start": format_timestamp(window_start),
            "end": format_timestamp(window_end),
            "attribution": "session_start",
        },
    }
    report.update(aggregate_sessions(sessions))
    report["projects"] = aggregate_by_project(sessions)
    report["models"] = aggregate_by_model(sessions)
    report["sessions"] = [session.to_dict() for session in sessions]
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    if value is None:
        return "n/a"
    return str(value)


def render_text(report: dict[str, Any]) -> str:
    """Render a human-readable report from an analysis result."""
    window = report.get("window", {})
    lines: list[str] = []
    lines.append("Session log analysis")
    lines.append("=" * 60)
    lines.append(f"Sessions directory : {report.get('sessions_dir')}")
    lines.append(
        "Window             : "
        f"[{window.get('start') or '-inf'}, {window.get('end') or '+inf'}) "
        f"(by {window.get('attribution', 'session_start')})"
    )
    lines.append(f"Sessions analysed  : {report.get('session_count', 0)}")

    duration = report.get("duration_minutes", {})
    lines.append("")
    lines.append("Duration (minutes)")
    lines.append(
        f"  mean={_format_number(duration.get('mean'))}"
        f"  median={_format_number(duration.get('median'))}"
        f"  min={_format_number(duration.get('min'))}"
        f"  max={_format_number(duration.get('max'))}"
        f"  total={_format_number(duration.get('sum'))}"
    )

    lines.append("")
    lines.append("Tokens")
    total = report.get("total_tokens", {})
    lines.append(
        f"  total      mean={_format_number(total.get('mean'))}"
        f"  median={_format_number(total.get('median'))}"
        f"  sum={_format_number(total.get('sum'))}"
    )
    for key, label in (
        ("input", "input     "),
        ("output", "output    "),
        ("cacheRead", "cacheRead "),
        ("cacheWrite", "cacheWrite"),
    ):
        stats = report.get("tokens", {}).get(key, {})
        lines.append(
            f"  {label} mean={_format_number(stats.get('mean'))}"
            f"  median={_format_number(stats.get('median'))}"
            f"  sum={_format_number(stats.get('sum'))}"
        )

    cost = report.get("cost", {})
    lines.append("")
    lines.append(
        "Cost: "
        f"total={_format_number(cost.get('total'))}  "
        f"input={_format_number(cost.get('input'))}  "
        f"output={_format_number(cost.get('output'))}  "
        f"cacheRead={_format_number(cost.get('cacheRead'))}  "
        f"cacheWrite={_format_number(cost.get('cacheWrite'))}"
    )

    lines.append("")
    lines.append("By project")
    projects = report.get("projects", {})
    if not projects:
        lines.append("  (none)")
    for name in sorted(projects):
        block = projects[name]
        lines.append(
            f"  {name}: sessions={block.get('session_count')}"
            f"  mean_duration={_format_number(block.get('duration_minutes', {}).get('mean'))} min"
            f"  sum_total_tokens={_format_number(block.get('total_tokens', {}).get('sum'))}"
            f"  cost={_format_number(block.get('cost', {}).get('total'))}"
        )

    lines.append("")
    lines.append("By model")
    models = report.get("models", {})
    if not models:
        lines.append("  (none)")
    for name in sorted(models):
        block = models[name]
        lines.append(
            f"  {name}: sessions={block.get('session_count')}"
            f"  sum_total_tokens={_format_number(block.get('tokens', {}).get('totalTokens'))}"
            f"  mean_total_tokens={_format_number(block.get('mean_total_tokens'))}"
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic pi session-log analyser (STE100 24h experiment).",
    )
    parser.add_argument(
        "--sessions-dir",
        default=str(DEFAULT_SESSIONS_DIR),
        help="Directory containing pi session logs (recursive). Default: %(default)s",
    )
    parser.add_argument(
        "--window-start",
        default=None,
        help="ISO-8601 start (inclusive) of the attribution window.",
    )
    parser.add_argument(
        "--window-end",
        default=None,
        help="ISO-8601 end (exclusive) of the attribution window.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Shorthand for --format json.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Also write the rendered report to this file path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    sessions_dir = Path(args.sessions_dir).expanduser()
    if not sessions_dir.is_dir():
        print(
            f"error: sessions directory not found: {sessions_dir}",
            file=sys.stderr,
        )
        return 2

    window_start = parse_timestamp(args.window_start) if args.window_start else None
    window_end = parse_timestamp(args.window_end) if args.window_end else None
    if args.window_start and window_start is None:
        print(f"error: invalid --window-start: {args.window_start}", file=sys.stderr)
        return 2
    if args.window_end and window_end is None:
        print(f"error: invalid --window-end: {args.window_end}", file=sys.stderr)
        return 2

    report = analyze(sessions_dir, window_start, window_end)

    output_format = "json" if args.json else args.format
    if output_format == "json":
        rendered = json.dumps(report, indent=2, sort_keys=False) + "\n"
    else:
        rendered = render_text(report)

    print(rendered, end="")
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    raise SystemExit(main())
