"""CSV and Markdown report generation, plus the end-to-end analysis runner.

Outputs (per acceptance criteria):

- ``daytime_sessions.csv`` — one row per daytime session (10:00-23:59, 6 slots
  per the configured schedule) covering ALL sessions in the window.
- ``nighttime_sessions.csv`` — one row per nighttime session (00:00-09:59).
- ``report.md`` — the aggregate Markdown report with highlighted,
  data-backed recommendations.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from itertools import chain
from pathlib import Path


import aggregation
import bucketing
import config_loader
import log_parser
import recommendations
from aggregation import AnalysisResult, SessionStats

CSV_COLUMNS = [
    "session_id",
    "start_time",
    "end_time",
    "duration_seconds",
    "messages",
    "start_context_size",
    "avg_context_size",
    "max_context_size",
    "avg_response_size",
    "max_response_size",
    "initial_provider",
    "initial_model",
    "remote_move_time",
    "fallback_reason",
    "bucket",
    "slots",
    "local_requests",
    "remote_requests",
    "dispatch_denied",
]

TS_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class AnalysisRun:
    summary: AnalysisResult
    files: list[Path] = field(default_factory=list)


def _fmt_ts(ts: datetime | None) -> str:
    return ts.strftime(TS_FMT) if ts else ""


def _session_row(s: SessionStats) -> dict:
    return {
        "session_id": s.session_id,
        "start_time": _fmt_ts(s.start),
        "end_time": _fmt_ts(s.end),
        "duration_seconds": f"{s.duration_seconds:.1f}",
        "messages": str(s.messages),
        "start_context_size": str(s.start_context_size) if s.start_context_size is not None else "",
        "avg_context_size": f"{s.avg_context_size:.1f}" if s.avg_context_size is not None else "",
        "max_context_size": str(s.max_context_size) if s.max_context_size is not None else "",
        "avg_response_size": f"{s.avg_response_size:.1f}" if s.avg_response_size is not None else "",
        "max_response_size": str(s.max_response_size) if s.max_response_size is not None else "",
        "initial_provider": s.initial_provider or "",
        "initial_model": s.initial_model or "",
        "remote_move_time": _fmt_ts(s.remote_move_time),
        "fallback_reason": s.fallback_reason or "",
        "bucket": s.bucket or "",
        "slots": str(s.slots) if s.slots else "",
        "local_requests": str(s.local_requests),
        "remote_requests": str(s.remote_requests),
        "dispatch_denied": str(s.dispatch_denied),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_csvs(summary: AnalysisResult, out_dir: Path) -> tuple[Path, Path]:
    """Write the daytime and nighttime session CSVs; returns their paths."""
    day_rows, night_rows = [], []
    for s in sorted(summary.sessions.values(), key=lambda x: x.start):
        row = _session_row(s)
        (day_rows if s.bucket != "night" else night_rows).append(row)
    day_path = out_dir / "daytime_sessions.csv"
    night_path = out_dir / "nighttime_sessions.csv"
    _write_csv(day_path, day_rows)
    _write_csv(night_path, night_rows)
    return day_path, night_path


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_report(summary: AnalysisResult, config: dict | None) -> str:
    recs = recommendations.generate_recommendations(summary, config)
    hours = (summary.window_end - summary.window_start).total_seconds() / 3600.0
    sessions = list(summary.sessions.values())
    total = summary.total_requests

    local_only = [s for s in sessions if s.remote_requests == 0]
    fell_back = [s for s in sessions if s.fell_back]
    remote_only = [s for s in sessions if s.local_requests == 0 and s.remote_requests > 0]

    fallback_rate = (len(summary.fallback_events) / total) if total else 0.0

    lines: list[str] = []
    ap = lines.append
    ap("# Proxy Usage Analysis Report")
    ap("")
    ap(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    ap(f"- Window: {_fmt_dt(summary.window_start)} → {_fmt_dt(summary.window_end)} ({hours:.1f}h)")
    ap(f"- Sessions in window: **{len(sessions)}** | Requests: **{total}** "
       f"(local {summary.local_requests} / remote {summary.remote_requests})")
    ap(f"- Fallback events: **{len(summary.fallback_events)}** "
       f"({fallback_rate * 100:.1f}% of requests) | dispatch denied: {summary.dispatch_denied_count}")
    ap(f"- Unattributed stream events (no session UUID): {summary.unattributed_events}")
    ap(f"- Lines parsed: {summary.total_lines} | lines skipped: {summary.lines_skipped}")

    ap("")
    ap("## Session classification")
    ap("")
    ap("| Class | Sessions | % of sessions |")
    ap("|---|---|---|")
    ap(f"| Local-only | {len(local_only)} | {_pct(len(local_only), len(sessions)):.1f}% |")
    ap(f"| Fell back (local → remote) | {len(fell_back)} | {_pct(len(fell_back), len(sessions)):.1f}% |")
    ap(f"| Remote-only (never used local) | {len(remote_only)} | {_pct(len(remote_only), len(sessions)):.1f}% |")

    ap("")
    ap("## Local vs remote")
    ap("")
    ap("| Metric | Value |")
    ap("|---|---|")
    ap(f"| Local requests | {summary.local_requests} ({_pct(summary.local_requests, total):.1f}%) |")
    ap(f"| Remote requests | {summary.remote_requests} ({_pct(summary.remote_requests, total):.1f}%) |")
    ap(f"| Local-only sessions | {len(local_only)} |")
    ap(f"| Sessions that fell back | {len(fell_back)} |")

    if summary.fallback_reason_counts:
        ap("")
        ap("## Fallback reasons")
        ap("")
        ap("| Reason | Events | % of fallbacks |")
        ap("|---|---|---|")
        for reason, count in summary.fallback_reason_counts.most_common():
            ap(f"| {reason} | {count} | {_pct(count, len(summary.fallback_events)):.1f}% |")

    if summary.routing_skip_reason_counts:
        ap("")
        ap("## routing_skip_local reasons")
        ap("")
        ap("| Reason | Events | % of skips |")
        ap("|---|---|---|")
        for reason, count in summary.routing_skip_reason_counts.most_common():
            ap(f"| {reason} | {count} | {_pct(count, len(summary.routing_skip_events)):.1f}% |")

    initial = Counter((s.initial_provider, s.initial_model) for s in sessions)
    ap("")
    ap("## Per-model breakdown (initial assignment)")
    ap("")
    ap("| Provider | Model | Sessions | Requests | Fell back |")
    ap("|---|---|---|---|---|")
    for (provider, model), count in initial.most_common():
        s_list = [s for s in sessions if s.initial_provider == provider and s.initial_model == model]
        reqs = sum(s.messages for s in s_list)
        fb = sum(1 for s in s_list if s.fell_back)
        ap(f"| {provider} | {model} | {count} | {reqs} | {fb} |")

    fell_back_rows = sorted((s for s in sessions if s.fell_back), key=lambda s: s.remote_move_time or s.start)
    if fell_back_rows:
        ap("")
        ap("## Sessions that fell back (first 20)")
        ap("")
        ap("| Session | Start | Moved to remote | Fallback reason |")
        ap("|---|---|---|---|")
        for s in fell_back_rows[:20]:
            ap(f"| {s.session_id} | {_fmt_ts(s.start)} | {_fmt_ts(s.remote_move_time)} | {s.fallback_reason or ''} |")

    bucket_stats = _bucket_stats(sessions)
    ap("")
    ap("## Daytime vs nighttime")
    ap("")
    ap("| Bucket | Sessions | Requests | Fell back | Fallback rate | Avg ctx | Max ctx |")
    ap("|---|---|---|---|---|---|---|")
    for bucket in ("day", "night"):
        b = bucket_stats.get(bucket)
        if b is None:
            ap(f"| {bucket} | 0 | 0 | 0 | - | - | - |")
            continue
        ctx_vals = [s.max_context_size for s in b["sessions_list"] if s.max_context_size is not None]
        avg_ctx = round(sum(ctx_vals) / len(ctx_vals)) if ctx_vals else "-"
        max_ctx = max(ctx_vals) if ctx_vals else "-"
        ap(
            f"| {bucket} | {b['count']} | {b['requests']} | {b['fell_back']} | "
            f"{b['fallback_rate'] * 100:.1f}% | {avg_ctx} | {max_ctx} |"
        )

    ctx_vals = [s.max_context_size for s in sessions if s.max_context_size is not None]
    if ctx_vals:
        ap("")
        ap("## Context usage")
        ap("")
        ap("| Metric | Tokens |")
        ap("|---|---|")
        ap(f"| Avg max context per session | {round(sum(ctx_vals) / len(ctx_vals))} |")
        ap(f"| Highest context | {max(ctx_vals)} |")

    ap("")
    ap("## Recommendations")
    ap("")
    if not recs:
        ap("_No issues detected._")
    for r in recs:
        ap(f"### [{r.severity.upper()}] {r.title}")
        ap("")
        ap(f"> Evidence: {r.evidence}")
        ap("")
        ap(r.detail)
        ap("")

    ap("## Notes and limitations")
    ap("")
    ap(
        "- Sessions are identified by their session UUID; context/response sizes use the "
        "authoritative per-request `tokens=prompt/completion/total` from `Stream finished` lines "
        "(log-line payloads are truncated and are never used for sizes)."
    )
    ap(
        "- A session is included when it has at least one `Stream started` inside the window; "
        "day/night bucketing uses the session start time and the slot schedule in proxy/config.yaml."
    )
    ap(
        "- Sessions spanning a slot-schedule transition may observe 503s during the drain window; "
        "those are expected and not treated as errors."
    )
    ap(
        "- `Fallback triggered` lines carry no session UUID; per-session attribution prefers the "
        "session's own `routing_skip_local` line and otherwise the nearest fallback event within 60s."
    )
    ap(
        "- Related context: work item LP-0MSAOQTJS000FFVM (evaluate increasing local ctx-size) can "
        "use this report's `large_context_bypass` data. See the skill's SKILL.md for interpretation."
    )
    return "\n".join(lines) + "\n"


def _pct(part: int, total: int) -> float:
    return (part / total * 100.0) if total else 0.0


def _bucket_stats(sessions: list[SessionStats]) -> dict:
    stats: dict = {}
    for s in sessions:
        b = stats.setdefault(
            s.bucket or "day",
            {"count": 0, "requests": 0, "fell_back": 0, "sessions_list": []},
        )
        b["count"] += 1
        b["requests"] += s.messages
        b["sessions_list"].append(s)
        if s.fell_back:
            b["fell_back"] += 1
    for b in stats.values():
        b["fallback_rate"] = (b["fell_back"] / b["count"]) if b["count"] else 0.0
    return stats


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_analysis(
    log_dir: Path,
    window_start: datetime,
    window_end: datetime,
    output_dir: Path,
    config: dict | None = None,
) -> AnalysisRun:
    """Discover log files, stream-parse them, aggregate sessions, and write
    the CSVs and report into ``output_dir``."""
    log_dir = Path(log_dir)
    output_dir = Path(output_dir)
    if config is None:
        config = config_loader.load_proxy_config(config_loader.find_config_path())
    schedule = bucketing.schedule_from_config(config, (config or {}).get("session_slot_pool_size"))

    files = log_parser.discover_log_files(log_dir, window_start)
    events = chain.from_iterable(
        log_parser.iter_events(f, window_start, window_end) for f in files
    )
    summary = aggregation.aggregate(events, window_start, window_end, schedule)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csvs(summary, output_dir)
    report_path = output_dir / "report.md"
    report_path.write_text(build_report(summary, config), encoding="utf-8")
    return AnalysisRun(summary=summary, files=files)


def summary_to_json(summary: AnalysisResult) -> dict:
    """Machine-readable summary of the analysis (one dict; JSON-serialisable)."""
    sessions = list(summary.sessions.values())
    local_only = sum(1 for s in sessions if s.remote_requests == 0)
    fell_back = sum(1 for s in sessions if s.fell_back)
    remote_only = sum(1 for s in sessions if s.local_requests == 0 and s.remote_requests > 0)
    total = summary.total_requests
    return {
        "window_start": _fmt_ts(summary.window_start),
        "window_end": _fmt_ts(summary.window_end),
        "sessions": len(sessions),
        "local_only_sessions": local_only,
        "fallback_sessions": fell_back,
        "remote_only_sessions": remote_only,
        "total_requests": total,
        "local_requests": summary.local_requests,
        "remote_requests": summary.remote_requests,
        "fallback_events": len(summary.fallback_events),
        "fallback_rate": round((len(summary.fallback_events) / total) if total else 0.0, 4),
        "dispatch_denied": summary.dispatch_denied_count,
        "unattributed_events": summary.unattributed_events,
        "day_sessions": sum(1 for s in sessions if s.bucket != "night"),
        "night_sessions": sum(1 for s in sessions if s.bucket == "night"),
        "recommendations": len(recommendations.generate_recommendations(summary, None)),
    }
