"""Downtime idle-gate / audit-queue coordination metrics.

Read-only measurement tooling for the idle-gate evaluation
(SA-0MTG5UPBR0028K50, parent SA-0MTG0Y34T007ZHPF). It collects the
metrics the research item needs in order to decide whether the herdr
downtime dispatcher's strict idle gate for audit dispatch should be
relaxed:

- **Gate windows** -- the herdr downtime worker only dispatches while a
  strict idle window is open (continuous LLM idle + free slots). Clustering
  consecutive dispatch events from ``<root>/.worklog/downtime-dispatches.log``
  (JSONL, bounded rolling log) approximates each gate opening, its duration,
  and how many items were drained per window.
- **Queue admission waits** -- the audit runner's bounded priority queue
  emits ``Audit slot acquired: ...`` lines carrying ``priority``,
  ``queue_position`` and ``wait_seconds``. Parsing them gives the wait
  distribution per priority tier and shows whether priority ordering is
  effective.
- **Concurrency verdicts** -- audit reports read from a worklog
  ``audit_results`` table are classified into legacy fail-fast
  (``no slot free within 0.0s``), bounded-queue saturation
  (``concurrency queue 'audit' saturated``) and host-wide saturation
  (``host-wide audit concurrency limit reached``), so the north-star
  "zero fail-fast verdicts under load" claim is measurable.

The tool is **read-only**: it never writes to a worklog, queue, or log.
Output is JSON by default (machine readable) with a ``--human`` summary.

Usage
-----
Collect metrics from every project worklog on the host and print JSON::

    python3 skill/shared/audit_gate_metrics.py \
        --root ~/projects/ContextHub \
        --root ~/projects/Tableau-Card-Engine \
        --root ~/projects/SorraAgents

Add ``--human`` for a readable summary, ``--audit-log`` to include
``.audit_debug`` queue-admission lines, and any ``--worklog-db`` to include
concurrency-verdict counts.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_GAP_SECONDS = 1800
"""Gap that separates two gate windows (30 minutes).

Consecutive dispatch events closer together than this are treated as one
gate opening; the first event at least this far from the previous window
starts a new one. Thirty minutes matches the dispatcher's poll cadence and
the observed burst structure of the rolling dispatch logs.
"""

DISPATCH_LOG_RELATIVE = Path(".worklog") / "downtime-dispatches.log"
AUDIT_DEBUG_DIRNAME = ".audit_debug"

# ── Verdict patterns (north-star AC1 evidence) ─────────────────────────

LEGACY_FAILFAST_PATTERN = "no slot free within 0.0s"
BOUNDED_SATURATION_PATTERN = "concurrency queue 'audit' saturated"
HOST_SATURATION_PATTERN = "host-wide audit concurrency limit reached"

VERDICT_PATTERNS: dict[str, str] = {
    "legacy_failfast": LEGACY_FAILFAST_PATTERN,
    "bounded_saturation": BOUNDED_SATURATION_PATTERN,
    "host_saturation": HOST_SATURATION_PATTERN,
}


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware UTC ``datetime`` or ``None``.

    Accepts both ``...Z`` and naive ``YYYY-MM-DDTHH:MM:SS`` forms (the
    downtime dispatch log and the audit queue log use different spellings).
    Malformed/absent values yield ``None`` so callers can skip them.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DispatchEvent:
    """One deduplicated downtime dispatch marker."""

    item_id: str
    kind: str
    timestamp: datetime


@dataclass(frozen=True)
class DispatchWindow:
    """A cluster of dispatch events treated as one gate opening."""

    start: datetime
    end: datetime
    events: tuple[DispatchEvent, ...]

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())

    def kind_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "count": len(self.events),
            "kind_counts": self.kind_counts(),
        }


@dataclass(frozen=True)
class QueueAdmission:
    """One audit-runner queue admission (a slot was acquired)."""

    timestamp: datetime
    priority: str
    position: int | None
    wait_seconds: float


@dataclass
class MetricsReport:
    """Aggregate metrics collected from the configured sources."""

    dispatch: dict[str, Any] = field(default_factory=dict)
    queue: dict[str, Any] = field(default_factory=dict)
    verdicts: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch": self.dispatch,
            "queue": self.queue,
            "verdicts": self.verdicts,
            "sources": self.sources,
        }


# ── Parsing ────────────────────────────────────────────────────────────


def parse_dispatch_log(text: str) -> list[DispatchEvent]:
    """Parse a downtime dispatch log (JSONL) into deduplicated events.

    Malformed lines and pane-close lifecycle entries are skipped. A single
    dispatch is written twice by the worker (a dispatch marker plus a
    post-spawn enrichment entry); duplicates are collapsed on
    ``(item_id, kind, timestamp)`` so window counts are not inflated.
    Spawn-failed entries are skipped -- a failed spawn is not a dispatch.
    """
    seen: set[tuple[str, str, str]] = set()
    events: list[DispatchEvent] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("entryType") == "pane-close":
            continue
        if entry.get("outcome") == "spawn-failed":
            continue
        item_id = entry.get("itemId")
        if not isinstance(item_id, str) or not item_id:
            continue
        timestamp = _parse_iso(entry.get("dispatchedAt"))
        if timestamp is None:
            continue
        kind = entry.get("kind") if isinstance(entry.get("kind"), str) else "unknown"
        key = (item_id, kind, timestamp.isoformat())
        if key in seen:
            continue
        seen.add(key)
        events.append(DispatchEvent(item_id=item_id, kind=kind, timestamp=timestamp))
    events.sort(key=lambda event: event.timestamp)
    return events


def cluster_windows(
    events: Sequence[DispatchEvent], gap_seconds: int = DEFAULT_GAP_SECONDS
) -> list[DispatchWindow]:
    """Cluster ordered dispatch events into gate windows.

    A new window starts when the gap from the previous event exceeds
    ``gap_seconds``. Returns windows oldest-first. An empty input yields
    ``[]``; a single event yields one zero-length window.
    """
    if not events:
        return []
    ordered = sorted(events, key=lambda event: event.timestamp)
    windows: list[DispatchWindow] = []
    current: list[DispatchEvent] = [ordered[0]]
    for event in ordered[1:]:
        gap = (event.timestamp - current[-1].timestamp).total_seconds()
        if gap > gap_seconds:
            windows.append(_window_from(current))
            current = [event]
        else:
            current.append(event)
    windows.append(_window_from(current))
    return windows


def _window_from(events: Sequence[DispatchEvent]) -> DispatchWindow:
    return DispatchWindow(
        start=events[0].timestamp,
        end=events[-1].timestamp,
        events=tuple(events),
    )


def compute_dispatch_metrics(
    events: Sequence[DispatchEvent], gap_seconds: int = DEFAULT_GAP_SECONDS
) -> dict[str, Any]:
    """Compute gate-window metrics from dispatch events.

    Reports total dispatches, per-day window/dispatch counts, mean and peak
    window duration, and items drained per window (overall and audit-only).
    """
    windows = cluster_windows(events, gap_seconds=gap_seconds)
    per_day: dict[str, dict[str, Any]] = {}
    for window in windows:
        day = window.start.date().isoformat()
        bucket = per_day.setdefault(
            day, {"windows": 0, "dispatches": 0, "audit_dispatches": 0, "durations": []}
        )
        bucket["windows"] += 1
        bucket["dispatches"] += len(window.events)
        bucket["audit_dispatches"] += window.kind_counts().get("audit", 0)
        bucket["durations"].append(window.duration_seconds)

    days: dict[str, Any] = {}
    for day, bucket in sorted(per_day.items()):
        durations = bucket.pop("durations")
        windows_count = bucket["windows"]
        days[day] = {
            **bucket,
            "avg_window_seconds": round(sum(durations) / len(durations), 3) if durations else 0.0,
            "max_window_seconds": round(max(durations), 3) if durations else 0.0,
            "dispatches_per_window": (
                round(bucket["dispatches"] / windows_count, 3) if windows_count else 0.0
            ),
        }

    durations = [window.duration_seconds for window in windows]
    kind_totals: dict[str, int] = {}
    for event in events:
        kind_totals[event.kind] = kind_totals.get(event.kind, 0) + 1
    return {
        "gap_seconds": gap_seconds,
        "total_dispatches": len(events),
        "total_windows": len(windows),
        "kind_totals": kind_totals,
        "audit_dispatches": kind_totals.get("audit", 0),
        "avg_window_seconds": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "max_window_seconds": round(max(durations), 3) if durations else 0.0,
        "avg_dispatches_per_window": (
            round(len(events) / len(windows), 3) if windows else 0.0
        ),
        "per_day": days,
    }


_QUEUE_ADMISSION_RE = re.compile(
    r"Audit slot acquired:\s+"
    r"queued_at=(?P<queued>\S+)\s+"
    r"priority=(?P<priority>\w+)\s+"
    r"queue_position=(?P<position>\S+)\s+"
    r"dequeued_at=\S+\s+"
    r"wait_seconds=(?P<wait>[\d.]+)"
)


def parse_queue_admissions(text: str) -> list[QueueAdmission]:
    """Parse ``Audit slot acquired`` lines into admission records.

    Lines that do not match (including malformed timestamps) are skipped.
    ``queue_position`` is ``None`` when the log omitted it.
    """
    admissions: list[QueueAdmission] = []
    for match in _QUEUE_ADMISSION_RE.finditer(text):
        timestamp = _parse_iso(match.group("queued"))
        if timestamp is None:
            continue
        raw_position = match.group("position")
        try:
            position: int | None = int(raw_position)
        except (TypeError, ValueError):
            position = None
        try:
            wait = float(match.group("wait"))
        except ValueError:
            continue
        admissions.append(
            QueueAdmission(
                timestamp=timestamp,
                priority=match.group("priority").lower(),
                position=position,
                wait_seconds=wait,
            )
        )
    admissions.sort(key=lambda admission: admission.timestamp)
    return admissions


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


def compute_queue_metrics(
    admissions: Sequence[QueueAdmission], slow_wait_seconds: float = 30.0
) -> dict[str, Any]:
    """Compute queue-admission wait statistics overall and per priority."""
    by_priority: dict[str, dict[str, Any]] = {}
    per_day: dict[str, dict[str, Any]] = {}
    for admission in admissions:
        bucket = by_priority.setdefault(
            admission.priority, {"count": 0, "waits": [], "slow": 0}
        )
        bucket["count"] += 1
        bucket["waits"].append(admission.wait_seconds)
        if admission.wait_seconds > slow_wait_seconds:
            bucket["slow"] += 1

        day_bucket = per_day.setdefault(
            admission.timestamp.date().isoformat(), {"count": 0, "waits": []}
        )
        day_bucket["count"] += 1
        day_bucket["waits"].append(admission.wait_seconds)

    priority_summary: dict[str, Any] = {}
    for priority, bucket in sorted(by_priority.items()):
        waits = sorted(bucket["waits"])
        priority_summary[priority] = {
            "count": bucket["count"],
            "avg_wait_seconds": round(sum(waits) / len(waits), 3) if waits else 0.0,
            "max_wait_seconds": round(max(waits), 3) if waits else 0.0,
            "p95_wait_seconds": round(_percentile(waits, 0.95), 3),
            "slow_admissions": bucket["slow"],
        }

    day_summary: dict[str, Any] = {}
    for day, bucket in sorted(per_day.items()):
        waits = sorted(bucket["waits"])
        day_summary[day] = {
            "count": bucket["count"],
            "avg_wait_seconds": round(sum(waits) / len(waits), 3) if waits else 0.0,
            "max_wait_seconds": round(max(waits), 3) if waits else 0.0,
        }

    all_waits = sorted(admission.wait_seconds for admission in admissions)
    return {
        "total_admissions": len(admissions),
        "slow_wait_threshold_seconds": slow_wait_seconds,
        "slow_admissions": sum(1 for wait in all_waits if wait > slow_wait_seconds),
        "avg_wait_seconds": round(sum(all_waits) / len(all_waits), 3) if all_waits else 0.0,
        "max_wait_seconds": round(max(all_waits), 3) if all_waits else 0.0,
        "p95_wait_seconds": round(_percentile(all_waits, 0.95), 3),
        "by_priority": priority_summary,
        "per_day": day_summary,
    }


def classify_concurrency_verdict(raw_output: str) -> set[str]:
    """Return the concurrency-verdict categories present in an audit report."""
    if not raw_output:
        return set()
    return {name for name, pattern in VERDICT_PATTERNS.items() if pattern in raw_output}


def count_concurrency_verdicts(raw_outputs: Iterable[str]) -> dict[str, int]:
    """Count audit reports (items) per concurrency-verdict category.

    Each input is one work item's raw audit output; an item is counted once
    per category even when the pattern occurs many times in its report.
    """
    counts = {name: 0 for name in VERDICT_PATTERNS}
    for raw_output in raw_outputs:
        for category in classify_concurrency_verdict(raw_output or ""):
            counts[category] += 1
    return counts


# ── Source loading ─────────────────────────────────────────────────────


def load_dispatch_events(paths: Iterable[Path]) -> tuple[list[DispatchEvent], list[str]]:
    """Load and merge dispatch events from JSONL log paths."""
    events: list[DispatchEvent] = []
    used: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        events.extend(parse_dispatch_log(text))
        used.append(str(path))
    events.sort(key=lambda event: event.timestamp)
    return events, used


def load_queue_admissions(paths: Iterable[Path]) -> tuple[list[QueueAdmission], list[str]]:
    """Load and merge queue-admission records from text/log paths."""
    admissions: list[QueueAdmission] = []
    used: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        admissions.extend(parse_queue_admissions(text))
        used.append(str(path))
    admissions.sort(key=lambda admission: admission.timestamp)
    return admissions, used


def load_worklog_verdicts(paths: Iterable[Path]) -> tuple[dict[str, int], list[str]]:
    """Count concurrency verdicts from the ``audit_results`` table of each DB.

    Opens each database read-only (``mode=ro``). A missing/unreadable DB or
    absent table is skipped. Returns the merged per-category item counts and
    the list of databases read.
    """
    totals = {name: 0 for name in VERDICT_PATTERNS}
    used: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT raw_output FROM audit_results")
            rows = cursor.fetchall()
        except sqlite3.Error:
            connection.close()
            continue
        counts = count_concurrency_verdicts(row[0] or "" for row in rows)
        for category, value in counts.items():
            totals[category] += value
        used.append(str(path))
        connection.close()
    return totals, used


def discover_dispatch_logs(roots: Iterable[Path]) -> list[Path]:
    """Return existing dispatch-log paths for the given project roots."""
    found: list[Path] = []
    for root in roots:
        candidate = Path(root) / DISPATCH_LOG_RELATIVE
        if candidate.exists():
            found.append(candidate)
    return found


def discover_audit_debug_dirs(roots: Iterable[Path]) -> list[Path]:
    """Return existing audit-debug directories for the given project roots.

    The audit debug directory is host-global (``~/.audit_debug``), not
    per-project, so a root is only included when the directory exists.
    """
    found: list[Path] = []
    for root in roots:
        candidate = Path(root) / AUDIT_DEBUG_DIRNAME
        if candidate.is_dir():
            found.append(candidate)
    return found


def iter_files(directories: Iterable[Path]) -> list[Path]:
    """Recursively list regular files under the given directories."""
    files: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        files.extend(sorted(path for path in directory.rglob("*") if path.is_file()))
    return files


def build_report(
    roots: Sequence[Path] = (),
    dispatch_logs: Sequence[Path] = (),
    audit_logs: Sequence[Path] = (),
    worklog_dbs: Sequence[Path] = (),
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> MetricsReport:
    """Collect all configured sources into a :class:`MetricsReport`."""
    resolved_dispatch = list(dispatch_logs) + discover_dispatch_logs(roots)
    events, dispatch_sources = load_dispatch_events(resolved_dispatch)

    admission_sources: list[Path] = list(audit_logs)
    if not admission_sources:
        admission_sources = iter_files(discover_audit_debug_dirs(roots))
    admissions, queue_sources = load_queue_admissions(admission_sources)
    # Admission lines are the same text as verdicts; reuse the read files for
    # the bounded/legacy verdict counts (audit_results is authoritative, but
    # debug logs capture runs that never persisted).
    verdict_totals, verdict_sources = load_worklog_verdicts(worklog_dbs)

    return MetricsReport(
        dispatch=compute_dispatch_metrics(events, gap_seconds=gap_seconds),
        queue=compute_queue_metrics(admissions),
        verdicts={
            "by_worklog_item": verdict_totals,
            "audit_debug_files_scanned": len(admission_sources),
        },
        sources={
            "dispatch_logs": dispatch_sources,
            "audit_logs": queue_sources,
            "worklog_dbs": verdict_sources,
        },
    )


# ── Rendering / CLI ────────────────────────────────────────────────────


def render_human(report: MetricsReport) -> str:
    """Render a human-readable summary of a metrics report."""
    dispatch = report.dispatch
    queue = report.queue
    lines = [
        "Downtime idle-gate / audit-queue metrics (SA-0MTG5UPBR0028K50)",
        "=" * 62,
        "",
        "Gate windows (dispatch clusters)",
        f"  gap threshold        : {dispatch.get('gap_seconds', 0)}s",
        f"  total dispatches     : {dispatch.get('total_dispatches', 0)}",
        f"  audit dispatches     : {dispatch.get('audit_dispatches', 0)}",
        f"  total windows        : {dispatch.get('total_windows', 0)}",
        f"  avg window duration  : {dispatch.get('avg_window_seconds', 0.0):.1f}s",
        f"  peak window duration : {dispatch.get('max_window_seconds', 0.0):.1f}s",
        f"  items per window     : {dispatch.get('avg_dispatches_per_window', 0.0):.2f}",
        "",
        "Per-day windows",
    ]
    for day, bucket in dispatch.get("per_day", {}).items():
        lines.append(
            f"  {day}: windows={bucket['windows']:3d} "
            f"dispatches={bucket['dispatches']:4d} "
            f"audit={bucket['audit_dispatches']:3d} "
            f"avg={bucket['avg_window_seconds']:.0f}s"
        )

    lines += [
        "",
        "Audit queue admissions",
        f"  total admissions     : {queue.get('total_admissions', 0)}",
        f"  avg wait             : {queue.get('avg_wait_seconds', 0.0):.2f}s",
        f"  max wait             : {queue.get('max_wait_seconds', 0.0):.1f}s",
        f"  p95 wait             : {queue.get('p95_wait_seconds', 0.0):.1f}s",
        (
            f"  slow (> {queue.get('slow_wait_threshold_seconds', 0):.0f}s)         : "
            f"{queue.get('slow_admissions', 0)}"
        ),
        "",
        "Queue waits by priority",
    ]
    for priority, bucket in queue.get("by_priority", {}).items():
        lines.append(
            f"  {priority:9s}: n={bucket['count']:4d} avg={bucket['avg_wait_seconds']:6.2f}s "
            f"max={bucket['max_wait_seconds']:7.2f}s p95={bucket['p95_wait_seconds']:6.2f}s "
            f"slow={bucket['slow_admissions']}"
        )

    verdicts = report.verdicts.get("by_worklog_item", {})
    lines += [
        "",
        "Concurrency-limit verdicts (by work item, worklog audit_results)",
        f"  legacy fail-fast (0s)    : {verdicts.get('legacy_failfast', 0)}",
        f"  bounded-queue saturation : {verdicts.get('bounded_saturation', 0)}",
        f"  host-wide saturation     : {verdicts.get('host_saturation', 0)}",
        "",
        "Verdict: see docs/dev/downtime-idle-gate-evaluation.md for the",
        "recommendation that interprets these metrics.",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: collect metrics and print JSON (or a human summary)."""
    parser = argparse.ArgumentParser(
        prog="audit_gate_metrics",
        description=(
            "Read-only downtime idle-gate / audit-queue metrics "
            "(SA-0MTG5UPBR0028K50)."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        type=Path,
        help="Project root; discovers <root>/.worklog/downtime-dispatches.log "
        "and <root>/.audit_debug. Repeatable.",
    )
    parser.add_argument(
        "--dispatch-log",
        action="append",
        default=[],
        type=Path,
        help="Explicit downtime dispatch JSONL log path. Repeatable.",
    )
    parser.add_argument(
        "--audit-log",
        action="append",
        default=[],
        type=Path,
        help="Explicit file containing 'Audit slot acquired' lines. Repeatable.",
    )
    parser.add_argument(
        "--worklog-db",
        action="append",
        default=[],
        type=Path,
        help="Worklog SQLite DB (read-only) for concurrency-verdict counts. Repeatable.",
    )
    parser.add_argument(
        "--gap-seconds",
        type=int,
        default=DEFAULT_GAP_SECONDS,
        help=f"Window gap in seconds (default {DEFAULT_GAP_SECONDS}).",
    )
    parser.add_argument(
        "--human", action="store_true", help="Print a human-readable summary."
    )
    parser.add_argument("--out", type=Path, help="Write the JSON report to this path.")
    args = parser.parse_args(argv)

    report = build_report(
        roots=args.root,
        dispatch_logs=args.dispatch_log,
        audit_logs=args.audit_log,
        worklog_dbs=args.worklog_db,
        gap_seconds=args.gap_seconds,
    )
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(payload + "\n", encoding="utf-8")
    if args.human:
        print(render_human(report))
    else:
        print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via CLI tests
    sys.exit(main())
