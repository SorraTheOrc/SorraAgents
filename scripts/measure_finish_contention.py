#!/usr/bin/env python3
# <!-- REFACTOR-SA-0MWM7HCWO0015YCP
# smell: unknown
# severity: low
# description: Unparenthesized implicit string concatenation in collection
# -->
# <!-- REFACTOR-SA-0MWM7HADO000KMIO
# smell: formatting
# severity: low
# description: Shebang is present but file is not executable
# -->
"""Measure ``implement.py finish`` lock contention before/after (SA-0MWFK10NH006QI7E).

Read-only companion to :mod:`measure_slot_wait`, built for the parent epic
**SA-0MWFJRWN00084FE1** ("Speed up implement finish under concurrent sessions,
target concurrency 6"). It answers two questions with counted data:

1. **Where did agent wall-clock go?** — the same bucketing used by the
   2026-11-13 baseline analysis of 36 AI_Hell implement sessions: summed
   session time vs wall-clock span, effective parallelism, peak concurrency,
   the ``bash``-wait share, the ``wait_for_job`` / own ``finish`` / scoped
   ``run_tests`` / hand-rolled polling buckets, per-repository lock refusals
   and lock-wait samples, non-fast-forward rebase events, dev-push full-suite
   gate runs and no-op sessions.
2. **Does a 6-way concurrent ``finish`` fan-out still serialise?** — a
   synthetic fan-out drives the **real** ``phase_finish`` code path in six
   concurrent threads against one main checkout, with only the external
   boundaries stubbed (build, suites, git commit/push, worklog). The
   expensive pre-lock phase is stubbed to the recorded baseline duration and
   the locked mutating window to a deliberately conservative value, so the
   measurement shows how the *schedule* changed, not how fast the stubs are.
   Every session's lock wait and lock hold is measured.

Reuse, not duplication
----------------------
The session-log parsing helpers (timestamp parsing, JSON-in-JSON
normalisation, text-block iteration, the gate/finish/run-tests classifiers and
the linear-interpolation percentile) are loaded from
:mod:`measure_slot_wait` — the existing read-only session-log analyser
(SA-0MW7C69KU008ZT5K). This module adds only the *session-level* bucketing and
the fan-out harness; the two share one definition of a wait sample so a
re-check never drifts between harnesses.

Metric definitions (identical in the "before" and "after" windows)
------------------------------------------------------------------
======================  ======================================================
session                 A ``*.jsonl`` pi session log whose **first** record's
                        timestamp falls inside ``[--since, --until)``. By
                        default only *implement* sessions count (a ``bash``
                        command invoking ``implement.py``); ``--all-sessions``
                        disables that filter.
wall-clock span         ``max(last record) - min(first record)`` over the
                        selected sessions — the batch's real elapsed time.
summed session time     Sum of each session's own span. Effective parallelism
                        is ``summed / wall-clock span``; peak concurrency is
                        the maximum number of overlapping session spans.
bash-wait share         ``sum(bash tool-call duration) / summed session time``
                        — how much of the agent's wall-clock sat in ``bash``.
buckets                 Each ``bash`` call is classified once, in this
                        precedence: ``wait_for_job`` → ``own_finish`` →
                        ``scoped_tests`` → ``polling`` → ``other``.
lock refusal            A ``Another run holds the repository lock`` message in
                        a *run-source* tool result (``implement.py``,
                        ``wait_for_job.py`` or ``git push``). Echoes from
                        ``grep``/``cat``/probe commands are ignored.
lock-wait sample        Time a session spent absorbing lock contention:

                        * post-bounded-wait (``waited_seconds`` in the finish
                          report, or ``Acquired the repository lock … after
                          N s``) — the reported internal wait;
                        * pre-bounded-wait retry loop (a ``bash`` call that
                          refused at least once and never reported a wait) —
                          the call's whole duration, which is the loop
                          absorbing another session's finish;
                        * pre-bounded-wait fail-fast episode — from a refused
                          single-shot ``finish`` to the start of the next
                          ``finish`` attempt that was not refused (when the
                          lock became available). The successful finish's own
                          work is never counted as a wait.

                        The forms are mutually exclusive per call, so one
                        acquisition is counted once.
rebase event            A ``non-fast-forward`` marker in a run-source push
                        result — the push race whose rebase previously forced
                        a second full-suite gate run.
full-suite gate run     A distinct ``run_tests: gate-subject pushed=<sha>``
                        (or gate-provenance block) in a run-source result,
                        de-duplicated by the sha it gated.
no-op session           A session shorter than :data:`NO_OP_SESSION_SECONDS`
                        with no ``bash`` calls — the drive retry that spawned
                        and immediately gave up. (An implement-only scan does
                        not select such a session; ``--all-sessions`` shows
                        it.)
======================  ======================================================

Pass/fail checks (parent AC1)
-----------------------------
* ``median_lock_wait_under_60s`` — median lock-wait sample ``< 60 s``.
* ``max_lock_wait_under_300s`` — no single lock-wait sample ``> 300 s``.
* ``no_session_majority_lock_blocked`` — no session spends more than half of
  its wall-clock behind the per-repository lock.

A check is ``None`` (not ``False``) when fewer than
:data:`MIN_LOCK_SAMPLES` samples exist, so a thin window never reads as a hard
failure — it reads as `inconclusive`.

Read-only guarantee
-------------------
The ``session`` mode only reads files. The ``fanout`` mode writes nothing
outside its own temporary directory (``--repo-root`` to reuse one), never
touches the live session store, the worklog or the real repository, and never
runs a real suite or a real git push.

Usage::

    python3 scripts/measure_finish_contention.py session \
        --since 2026-11-13T00:00:00Z --until 2026-11-14T00:00:00Z \
        --project AI_Hell --json

    python3 scripts/measure_finish_contention.py session \
        --since 2026-11-14T00:00:00Z --baseline before.json --json

    python3 scripts/measure_finish_contention.py fanout --count 6 --json

Exit status is ``0`` unless a check fails (``verdict == "fail"``).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

#: Default session-log root (the pi session store).
DEFAULT_SESSIONS_ROOT = "~/.pi/agent/sessions"

#: Parent epic's outcome thresholds (AC: median < 60 s, max <= 5 min).
MEDIAN_LOCK_WAIT_SECONDS = 60.0
MAX_LOCK_WAIT_SECONDS = 300.0

#: A session spending more than this share of its wall-clock behind the
#: per-repository lock is "majority blocked" — the symptom the epic removes.
MAJORITY_LOCK_SHARE = 0.5

#: Fewer lock-wait samples than this leave the thresholds unevaluable.
MIN_LOCK_SAMPLES = 3

#: An implement session shorter than this with no ``bash`` call is a no-op.
NO_OP_SESSION_SECONDS = 120.0

#: Recorded baseline full-suite duration (2026-11-13: ~5.4 min / 326 s) used
#: as the default concurrent pre-lock phase for the synthetic fan-out.
DEFAULT_SUITE_SECONDS = 326.0

#: Conservative default for the locked mutating window (commit -> fast-forward
#: -> push -> ancestry assertion). Observed pushes complete in a few seconds;
#: 15 s deliberately overshoots so the fan-out cannot pass by under-stating
#: the critical section.
DEFAULT_LOCK_WINDOW_SECONDS = 15.0

#: Bounded wait the synthetic fan-out configures for the lock.
DEFAULT_FANOUT_LOCK_TIMEOUT = 900.0

#: A bash command that belongs to the implement skill (session selection).
_IMPLEMENT_COMMAND_RE = re.compile(
    r"implement\.py\s+(?:start|finish|drive|parent|cleanup|clean)\b"
    r"|skill:implement\b"
)

#: A bash command that retries ``finish`` in a shell loop (pre-fix pattern).
_RETRY_LOOP_RE = re.compile(r"\bfor\s+\w+\s+in\b|\bwhile\b|\buntil\b")

#: A retry-loop finish invocation (pre-bounded-wait lock absorption).
_LOOPED_FINISH_RE = re.compile(
    r"implement\.py\s+finish|skill:implement\b"
)

#: A command that produces/absorbs lock contention (genuine-run filter).
_RUN_SOURCE_COMMAND_RE = re.compile(
    r"implement\.py|wait_for_job\.py|\bgit\b[^\n]*\bpush\b"
)

_LOCK_REFUSAL_MARKER = "Another run holds the repository lock"
_LOCK_ACQUIRED_RE = re.compile(
    r"Acquired the repository lock\b[^\n\"]*?after\s+([0-9]+(?:\.[0-9]+)?)s"
)
_WAITED_SECONDS_RE = re.compile(
    r'"waited_seconds"\s*:\s*([0-9]+(?:\.[0-9]+)?)'
)
_REBASE_MARKER = "non-fast-forward"
_POLLING_RE = re.compile(r"\bsleep\s+[0-9]")
_POLLING_COMPANION_RE = re.compile(r"\b(?:grep|wc)\b")
_SCOPED_TESTS_RE = re.compile(r"run_tests\.py")
_WAIT_FOR_JOB_RE = re.compile(r"wait_for_job\.py")
_OWN_FINISH_RE = re.compile(r"implement\.py\s+finish\b|phase-finish|--phase\s+finish")

#: Bucket names in classification precedence order.
BUCKETS = ("wait_for_job", "own_finish", "scoped_tests", "polling", "other")


# ---------------------------------------------------------------------------
# Shared helpers (reused from the existing session-log analyser)
# ---------------------------------------------------------------------------


def _load_slot_wait():
    """Load ``measure_slot_wait`` by file location (``scripts`` is not a pkg)."""
    path = SCRIPTS_DIR / "measure_slot_wait.py"
    spec = importlib.util.spec_from_file_location("_measure_slot_wait", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SLOT_WAIT = _load_slot_wait()


def _round(value: float, digits: int = 3) -> float:
    """Round for stable output (never ``-0.0``)."""
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _minutes(seconds: float) -> float:
    """Seconds as minutes, rounded for stable reports."""
    return _round(seconds / 60.0, 1)


def _share(part: float, whole: float) -> float:
    """``part / whole`` as a 0..1 share (0.0 for an empty whole)."""
    if not whole:
        return 0.0
    return _round(part / whole, 3)


def _session_id(path: Path) -> str:
    """Stable session identifier from the log file name (no content)."""
    stem = path.stem
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        stem,
    )
    return match.group(1) if match else stem


def _project(path: Path) -> str:
    """Project slug from the pi session directory name (``--…--`` wrapping)."""
    parent = path.parent.name
    if parent.startswith("--") and parent.endswith("--"):
        parent = parent[2:-2]
    return parent or "unknown"


def classify_command(command: str) -> str:
    """Classify one ``bash`` command into exactly one baseline bucket.

    Precedence matches the 2026-11-13 analysis so the before/after numbers
    stay comparable: a ``wait_for_job`` wrapper outranks the ``finish`` or
    ``run_tests`` it drives, and a hand-rolled poll loop is only ``polling``
    when it is not already one of the named call sites.
    """
    text = command or ""
    if _WAIT_FOR_JOB_RE.search(text):
        return "wait_for_job"
    if _OWN_FINISH_RE.search(text):
        return "own_finish"
    if _SCOPED_TESTS_RE.search(text):
        return "scoped_tests"
    if _POLLING_RE.search(text) and _POLLING_COMPANION_RE.search(text):
        return "polling"
    return "other"


def is_run_source(command: str) -> bool:
    """True when a command can produce a genuine lock/push measurement."""
    return bool(_RUN_SOURCE_COMMAND_RE.search(command or ""))


def is_implement_command(command: str) -> bool:
    """True when a command invokes the implement skill."""
    return bool(_IMPLEMENT_COMMAND_RE.search(command or ""))


# ---------------------------------------------------------------------------
# Session-log parsing
# ---------------------------------------------------------------------------


def _timestamp(raw: str) -> datetime | None:
    """Parse a session timestamp via the shared helper (single definition)."""
    return SLOT_WAIT._parse_timestamp(raw)


def _message_text(message: dict) -> str:
    """Concatenate a message's text blocks (shared iterator)."""
    return "\n".join(SLOT_WAIT._iter_text_blocks(message))


def parse_session(path: Path) -> dict[str, Any] | None:
    """Parse one session log into spans and paired ``bash`` calls.

    Returns ``None`` for an unreadable or timestamp-less log. Each bash call is
    ``{command, started, duration, result}``; the record carries no prompt or
    response content, only timestamps, commands and tool output text (used for
    structural markers and discarded afterwards).
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    pending: dict[str, tuple[str, datetime]] = {}
    calls: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    stamps: list[datetime] = []

    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        timestamp = _timestamp(record.get("timestamp", ""))
        if timestamp is not None:
            stamps.append(timestamp)
        message = record.get("message") or {}
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "toolCall"
                ):
                    call_id = block.get("id")
                    if call_id is None or timestamp is None:
                        continue
                    arguments = block.get("arguments") or {}
                    command = arguments.get("command", "") or ""
                    timeline.append(
                        {
                            "call_id": call_id,
                            "started": timestamp,
                            "name": block.get("name"),
                            "command": command,
                        }
                    )
                    if block.get("name") != "bash":
                        continue
                    pending[call_id] = (command, timestamp)
        elif role == "toolResult" and isinstance(content, list):
            call_id = message.get("toolCallId")
            if call_id not in pending or timestamp is None:
                continue
            command, started = pending.pop(call_id)
            calls.append(
                {
                    "call_id": call_id,
                    "command": command,
                    "started": started,
                    "duration": max(
                        0.0, (timestamp - started).total_seconds()
                    ),
                    "result": _message_text(message),
                }
            )

    if not stamps:
        return None
    return {
        "path": path,
        "start": min(stamps),
        "end": max(stamps),
        "bash_calls": calls,
        "timeline": timeline,
    }


def _lock_samples(call: dict[str, Any], text: str) -> list[float]:
    """Lock-wait samples a single bash result *reports* (no inference).

    One ``finish`` invocation can report the same wait twice — the progress
    line (``Acquired the repository lock … after N s``) and the report's
    ``waited_seconds`` — so the *reported* value wins and the progress line is
    used only when no report is present (older logs). A *retry-loop*
    ``finish`` that refused without any report falls back to the loop's whole
    duration: the loop absorbed a concurrent holder internally.

    A fail-fast refusal reports ``waited_seconds`` ~ 0 — that is *not* a wait,
    so it yields nothing here; the window-level grouping in
    :func:`_session_lock_samples` turns it into the time until the lock became
    available. A bounded wait that *timed out* says "Timed out after …" and is
    a genuine (long) wait, so it keeps its reported sample.
    """
    command = call["command"]
    if not is_run_source(command):
        return []
    reported = [float(raw) for raw in _WAITED_SECONDS_RE.findall(text)]
    if reported and _LOCK_REFUSAL_MARKER not in text:
        return reported
    progress = [float(raw) for raw in _LOCK_ACQUIRED_RE.findall(text)]
    if progress:
        return progress
    if (
        _LOCK_REFUSAL_MARKER in text
        and _LOOPED_FINISH_RE.search(command)
        and _RETRY_LOOP_RE.search(command)
    ):
        return [float(call["duration"])]
    return []


def _session_lock_samples(
    calls: list[dict[str, Any]], timeline: list[dict[str, Any]]
) -> list[float]:
    """All lock-wait samples for one session, counting each episode once.

    Post-fix, a ``finish`` absorbs the wait internally and reports it — those
    reported samples are taken as-is. Pre-fix a ``finish`` refused fail-fast
    and the agent re-ran it, so one contention episode spans several bash
    calls. An episode is measured from a refused call to the **immediately
    following** ``finish`` attempt (only intervening ``finish`` calls are
    tolerated) that was not refused — the moment the lock became available.
    If the agent did other work in between, the gap is *not* a lock wait and no
    sample is invented. A retry-loop call is one sample of its own duration
    (handled in :func:`_lock_samples`), so its retries are never re-grouped.
    """
    bash_by_id = {call["call_id"]: call for call in calls}
    samples: list[float] = []
    consumed: set[str] = set()

    for index, event in enumerate(timeline):
        call = bash_by_id.get(event["call_id"])
        if call is None or event["call_id"] in consumed:
            continue
        command = call["command"]
        text = SLOT_WAIT._normalise(call["result"])
        reported = _lock_samples(call, text)
        if reported:
            samples.extend(reported)
            continue
        if not (
            _LOCK_REFUSAL_MARKER in text
            and _LOOPED_FINISH_RE.search(command)
            and not _RETRY_LOOP_RE.search(command)
        ):
            continue
        cursor = index + 1
        while cursor < len(timeline):
            candidate_event = timeline[cursor]
            candidate = bash_by_id.get(candidate_event["call_id"])
            if candidate is None or not _LOOPED_FINISH_RE.search(
                candidate["command"]
            ):
                break
            consumed.add(candidate["call_id"])
            candidate_text = SLOT_WAIT._normalise(candidate["result"])
            if _LOCK_REFUSAL_MARKER not in candidate_text:
                samples.append(
                    max(
                        0.0,
                        (
                            candidate_event["started"] - call["started"]
                        ).total_seconds(),
                    )
                )
                break
            cursor += 1
    return samples


def analyse_session(
    path: Path,
    *,
    gate_keys: set[tuple],
) -> dict[str, Any] | None:
    """Summarise one session; ``gate_keys`` de-duplicates gate runs globally.

    Mutates ``gate_keys`` with the gate-run identities this session reported so
    the window counts each dev-push full-suite gate run once.
    """
    session = parse_session(path)
    if session is None:
        return None

    span = max(0.0, (session["end"] - session["start"]).total_seconds())
    buckets = {name: 0.0 for name in BUCKETS}
    bash_seconds = 0.0
    refusals = 0
    rebase_events = 0
    implement = False

    for call in session["bash_calls"]:
        command = call["command"]
        duration = float(call["duration"])
        bash_seconds += duration
        buckets[classify_command(command)] += duration
        if is_implement_command(command):
            implement = True
        if not is_run_source(command):
            continue
        text = SLOT_WAIT._normalise(call["result"])
        refusals += text.count(_LOCK_REFUSAL_MARKER)
        rebase_events += text.count(_REBASE_MARKER)
        parsed = SLOT_WAIT.parse_block(call["result"])
        for sha, rounded, lanes in parsed["gate"]:
            gate_keys.add(sha or (rounded, lanes))

    lock_samples = _session_lock_samples(
        session["bash_calls"], session["timeline"]
    )
    no_op = span < NO_OP_SESSION_SECONDS and not session["bash_calls"]
    lock_total = sum(lock_samples)

    return {
        "session_id": _session_id(path),
        "project": _project(path),
        "start": session["start"].isoformat(),
        "end": session["end"].isoformat(),
        "span_seconds": _round(span),
        "bash_seconds": _round(bash_seconds),
        "bash_share": _share(bash_seconds, span),
        "buckets": {name: _round(value) for name, value in buckets.items()},
        "lock_waits": [_round(value) for value in lock_samples],
        "lock_wait_seconds": _round(lock_total),
        "lock_share": _share(lock_total, span),
        "lock_refusals": refusals,
        "rebase_events": rebase_events,
        "implement_session": implement,
        "no_op": no_op,
    }


def _iter_session_files(root: Path) -> list[Path]:
    """Session files under *root*, in a deterministic order."""
    return SLOT_WAIT._iter_session_files(root)


def analyse_window(
    sessions_root: str | Path = DEFAULT_SESSIONS_ROOT,
    *,
    since: str | None = None,
    until: str | None = None,
    projects: Iterable[str] = (),
    all_sessions: bool = False,
    min_samples: int = MIN_LOCK_SAMPLES,
) -> dict[str, Any]:
    """Aggregate a session-log window into the baseline's metric set.

    Args:
        sessions_root: Session-log root (or a single ``*.jsonl``).
        since/until: ISO-8601 bounds on a session's **first** record.
        projects: Substring filters applied to the session directory path
            (e.g. ``("AI_Hell",)``); empty means every project.
        all_sessions: Count every session, not only implement sessions.
        min_samples: Lock-wait samples needed before the thresholds are
            evaluated (fewer ⇒ ``verdict == "inconclusive"``).

    Returns:
        A JSON-serialisable report; see the module docstring for definitions.
    """
    since_dt = _timestamp(since) if since else None
    until_dt = _timestamp(until) if until else None
    if since and since_dt is None:
        raise ValueError(f"invalid --since timestamp: {since!r}")
    if until and until_dt is None:
        raise ValueError(f"invalid --until timestamp: {until!r}")
    if since_dt and until_dt and until_dt < since_dt:
        raise ValueError("--until must not precede --since")

    root = Path(sessions_root).expanduser()
    project_filters = tuple(p for p in projects if p)
    files = _iter_session_files(root)

    gate_keys: set[tuple] = set()
    rows: list[dict[str, Any]] = []
    files_scanned = 0
    for path in files:
        if project_filters and not any(
            needle in str(path) for needle in project_filters
        ):
            continue
        files_scanned += 1
        row = analyse_session(path, gate_keys=gate_keys)
        if row is None:
            continue
        start = _timestamp(row["start"])
        if start is None:
            continue
        if since_dt is not None and start < since_dt:
            continue
        if until_dt is not None and start >= until_dt:
            continue
        if not all_sessions and not row["implement_session"]:
            continue
        rows.append(row)

    return _aggregate(
        rows,
        sessions_root=str(root),
        files_scanned=files_scanned,
        gate_runs=len(gate_keys),
        since=since_dt,
        until=until_dt,
        project_filters=project_filters,
        all_sessions=all_sessions,
        min_samples=min_samples,
    )


def _peak_concurrency(rows: list[dict[str, Any]]) -> int:
    """Maximum number of overlapping session spans (ties end before start)."""
    events: list[tuple[datetime, int]] = []
    for row in rows:
        start = _timestamp(row["start"])
        end = _timestamp(row["end"])
        if start is None or end is None:
            continue
        events.append((start, 1))
        events.append((end, -1))
    current = peak = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        current += delta
        peak = max(peak, current)
    return peak


def _aggregate(
    rows: list[dict[str, Any]],
    *,
    sessions_root: str,
    files_scanned: int,
    gate_runs: int,
    since: datetime | None,
    until: datetime | None,
    project_filters: tuple[str, ...],
    all_sessions: bool,
    min_samples: int,
) -> dict[str, Any]:
    """Build the aggregate report and evaluate the parent's thresholds."""
    summed = sum(row["span_seconds"] for row in rows)
    bash_seconds = sum(row["bash_seconds"] for row in rows)
    bucket_seconds = {
        name: sum(row["buckets"][name] for row in rows) for name in BUCKETS
    }

    starts = [row["start"] for row in rows]
    ends = [row["end"] for row in rows]
    first = _timestamp(min(starts)) if starts else None
    last = _timestamp(max(ends)) if ends else None
    wall_seconds = (
        max(0.0, (last - first).total_seconds()) if first and last else 0.0
    )

    samples: list[float] = []
    for row in rows:
        samples.extend(row["lock_waits"])
    samples.sort()
    reliable = len(samples) >= min_samples
    median = statistics.median(samples) if samples else 0.0
    maximum = samples[-1] if samples else 0.0
    mean = statistics.fmean(samples) if samples else 0.0

    majority_blocked = [
        row["session_id"]
        for row in rows
        if row["lock_waits"] and row["lock_share"] > MAJORITY_LOCK_SHARE
    ]
    sessions_with_wait = sum(1 for row in rows if row["lock_waits"])
    refusals = sum(row["lock_refusals"] for row in rows)
    rebase_events = sum(row["rebase_events"] for row in rows)
    no_ops = sum(1 for row in rows if row["no_op"])

    checks = _evaluate_checks(
        samples=samples,
        median=median,
        maximum=maximum,
        majority_blocked=majority_blocked,
        reliable=reliable,
        min_samples=min_samples,
    )
    failures = [check["id"] for check in checks if check["passed"] is False]
    if failures:
        verdict = "fail"
    elif any(check["passed"] is None for check in checks):
        verdict = "inconclusive"
    else:
        verdict = "pass"

    return {
        "success": bool(rows),
        "mode": "session",
        "sessions_root": sessions_root,
        "selection": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "project_filters": list(project_filters),
            "implement_only": not all_sessions,
            "files_scanned": files_scanned,
            "sessions": len(rows),
        },
        "window": {
            "start": first.isoformat() if first else None,
            "end": last.isoformat() if last else None,
            "wall_clock_minutes": _minutes(wall_seconds),
        },
        "aggregate": {
            "summed_session_minutes": _minutes(summed),
            "wall_clock_minutes": _minutes(wall_seconds),
            "effective_parallelism": (
                _round(summed / wall_seconds, 2) if wall_seconds else 0.0
            ),
            "peak_concurrency": _peak_concurrency(rows),
            "bash_minutes": _minutes(bash_seconds),
            "bash_share": _share(bash_seconds, summed),
            "buckets": {
                name: {
                    "minutes": _minutes(bucket_seconds[name]),
                    "share": _share(bucket_seconds[name], summed),
                }
                for name in BUCKETS
            },
            "lock": {
                "refusals": refusals,
                "samples": len(samples),
                "sessions_with_wait": sessions_with_wait,
                "median_seconds": _round(median) if samples else None,
                "mean_seconds": _round(mean) if samples else None,
                "max_seconds": _round(maximum) if samples else None,
                "majority_blocked_sessions": majority_blocked,
                "reliable": reliable,
            },
            "rebase_events": rebase_events,
            "full_suite_gate_runs": gate_runs,
            "no_op_sessions": no_ops,
        },
        "thresholds": {
            "median_lock_wait_seconds": MEDIAN_LOCK_WAIT_SECONDS,
            "max_lock_wait_seconds": MAX_LOCK_WAIT_SECONDS,
            "majority_lock_share": MAJORITY_LOCK_SHARE,
            "min_lock_samples": min_samples,
        },
        "checks": checks,
        "verdict": verdict,
        "failures": failures,
        "sessions": rows,
    }


def _evaluate_checks(
    *,
    samples: list[float],
    median: float,
    maximum: float,
    majority_blocked: list[str],
    reliable: bool,
    min_samples: int,
) -> list[dict[str, Any]]:
    """Evaluate the parent's three lock-contention thresholds.

    An under-sampled window yields ``passed: None`` for the sample-based
    checks (inconclusive, never a hard failure); the majority-blocked check is
    always evaluable because it is derived from the sessions themselves.
    """
    checks: list[dict[str, Any]] = [
        {
            "id": "no_session_majority_lock_blocked",
            "passed": not majority_blocked,
            "observed": len(majority_blocked),
            "threshold_share": MAJORITY_LOCK_SHARE,
            "detail": (
                "no session spends more than "
                f"{MAJORITY_LOCK_SHARE:.0%} of its wall-clock behind the "
                "per-repository lock"
                if not majority_blocked
                else "sessions majority-blocked on the lock: "
                + ", ".join(majority_blocked)
            ),
        }
    ]

    if reliable:
        checks.extend(
            [
                {
                    "id": "median_lock_wait_under_60s",
                    "passed": median < MEDIAN_LOCK_WAIT_SECONDS,
                    "observed_seconds": _round(median),
                    "threshold_seconds": MEDIAN_LOCK_WAIT_SECONDS,
                    "samples": len(samples),
                    "detail": (
                        f"median lock wait is {_round(median)}s over "
                        f"{len(samples)} samples "
                        f"(threshold <{MEDIAN_LOCK_WAIT_SECONDS:g}s)"
                    ),
                },
                {
                    "id": "max_lock_wait_under_300s",
                    "passed": maximum <= MAX_LOCK_WAIT_SECONDS,
                    "observed_seconds": _round(maximum),
                    "threshold_seconds": MAX_LOCK_WAIT_SECONDS,
                    "samples": len(samples),
                    "detail": (
                        f"max lock wait is {_round(maximum)}s over "
                        f"{len(samples)} samples "
                        f"(threshold <={MAX_LOCK_WAIT_SECONDS:g}s)"
                    ),
                },
            ]
        )
    else:
        checks.extend(
            [
                {
                    "id": "median_lock_wait_under_60s",
                    "passed": None,
                    "observed_seconds": _round(median) if samples else None,
                    "threshold_seconds": MEDIAN_LOCK_WAIT_SECONDS,
                    "samples": len(samples),
                    "detail": (
                        f"only {len(samples)} lock-wait samples "
                        f"(need {min_samples}); median not evaluable"
                    ),
                },
                {
                    "id": "max_lock_wait_under_300s",
                    "passed": None,
                    "observed_seconds": _round(maximum) if samples else None,
                    "threshold_seconds": MAX_LOCK_WAIT_SECONDS,
                    "samples": len(samples),
                    "detail": (
                        f"only {len(samples)} lock-wait samples "
                        f"(need {min_samples}); max not evaluable"
                    ),
                },
            ]
        )
    return checks


# ---------------------------------------------------------------------------
# Synthetic 6-way concurrent finish fan-out
# ---------------------------------------------------------------------------


def _fanout_checks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate the fan-out rows against the parent's thresholds."""
    waits = sorted(row["lock_wait_seconds"] for row in rows)
    median = statistics.median(waits) if waits else 0.0
    maximum = waits[-1] if waits else 0.0
    majority = [
        row["session"] for row in rows if row["lock_share"] > MAJORITY_LOCK_SHARE
    ]
    failed = [row["session"] for row in rows if not row["success"]]
    checks = [
        {
            "id": "all_sessions_succeeded",
            "passed": not failed,
            "detail": (
                "every concurrent finish completed successfully"
                if not failed
                else "failed sessions: " + ", ".join(failed)
            ),
        },
        {
            "id": "median_lock_wait_under_60s",
            "passed": median < MEDIAN_LOCK_WAIT_SECONDS,
            "observed_seconds": _round(median),
            "threshold_seconds": MEDIAN_LOCK_WAIT_SECONDS,
            "samples": len(waits),
            "detail": (
                f"median lock wait is {_round(median)}s across "
                f"{len(waits)} finishes "
                f"(threshold <{MEDIAN_LOCK_WAIT_SECONDS:g}s)"
            ),
        },
        {
            "id": "max_lock_wait_under_300s",
            "passed": maximum <= MAX_LOCK_WAIT_SECONDS,
            "observed_seconds": _round(maximum),
            "threshold_seconds": MAX_LOCK_WAIT_SECONDS,
            "samples": len(waits),
            "detail": (
                f"max lock wait is {_round(maximum)}s across "
                f"{len(waits)} finishes "
                f"(threshold <={MAX_LOCK_WAIT_SECONDS:g}s)"
            ),
        },
        {
            "id": "no_session_majority_lock_blocked",
            "passed": not majority,
            "observed": len(majority),
            "threshold_share": MAJORITY_LOCK_SHARE,
            "detail": (
                "no finish spends more than "
                f"{MAJORITY_LOCK_SHARE:.0%} of its wall-clock waiting for the "
                "per-repository lock"
                if not majority
                else "majority-blocked finishes: " + ", ".join(majority)
            ),
        },
    ]
    return checks


def _fake_run_cmd(cmd, *args, **kwargs):
    """Stand-in for ``implement.run_cmd`` inside the fan-out harness."""
    import subprocess

    fake_commit = "1" * 40
    stdout = f"{fake_commit}\n" if len(cmd) > 1 and cmd[1] == "rev-parse" else ""
    if len(cmd) > 1 and cmd[1] == "merge-base":
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr=""
        )
    return subprocess.CompletedProcess(
        args=cmd, returncode=0, stdout=stdout, stderr=""
    )


def _tests_result(scope: str = "changed") -> dict[str, Any]:
    """A green suite result (the harness never runs a real suite)."""
    return {
        "success": True,
        "failures": [],
        "skipped": False,
        "tooling": "suite",
        "scope": scope,
        "notices": [],
    }


@contextlib.contextmanager
def _finish_fanout_harness(
    main_checkout: str,
    *,
    suite_seconds: float,
    lock_window_seconds: float,
    lock_timeout_seconds: float,
    barrier: threading.Barrier | None,
):
    """Patch every external boundary of ``phase_finish`` for the fan-out.

    Only the expensive and mutating boundaries are stubbed: the suites sleep
    for *suite_seconds* (concurrently, **outside** the lock), and the push —
    the dominant part of the locked window — sleeps for
    *lock_window_seconds*. The real ``phase_finish`` ordering, the real
    per-repository ``flock`` and the real bounded wait are exercised.
    """
    implement = _load_implement()
    worktree = str(Path(main_checkout).parent / "wt")
    Path(worktree).mkdir(parents=True, exist_ok=True)
    state = implement.ImplementState(
        work_item_id="WL-FANOUT",
        worktree_path=worktree,
        repo_root=main_checkout,
        parent_branch="dev",
    )
    fake_commit = "1" * 40
    local = threading.local()

    real_acquire = implement.acquire_main_checkout_lock
    real_release = implement._release_main_checkout_lock

    def _timed_acquire(repo_root, work_item_id, timeout=0.0, **kwargs):
        started = time.monotonic()
        fd, refusal = real_acquire(
            repo_root, work_item_id, timeout=timeout, **kwargs
        )
        local.acquire_started = started
        local.acquired_at = time.monotonic()
        local.released_at = None
        return fd, refusal

    def _timed_release(fd):
        if fd is not None:
            local.released_at = time.monotonic()
        return real_release(fd)

    def _fake_run_tests(cwd, scope="changed", base_ref=None, fingerprint=None):
        if scope == "full":
            if barrier is not None:
                try:
                    barrier.wait(timeout=lock_timeout_seconds)
                except threading.BrokenBarrierError:
                    pass
            if suite_seconds > 0:
                time.sleep(suite_seconds)
        return {
            **_tests_result(scope),
            "git_fingerprint": fingerprint,
        }

    def _fake_push(worktree_path, *args, **kwargs):
        if lock_window_seconds > 0:
            time.sleep(lock_window_seconds)
        return True

    previous_timeout = os.environ.get(
        implement.ENV_FINISH_LOCK_TIMEOUT
    )
    previous_skip = os.environ.pop(implement.ENV_TEST_SCOPE_SKIP, None)
    os.environ[implement.ENV_FINISH_LOCK_TIMEOUT] = str(lock_timeout_seconds)
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    implement, "_discover_worktree", return_value=worktree
                )
            )
            stack.enter_context(
                patch.object(implement, "read_state", return_value=state)
            )
            stack.enter_context(
                patch.object(
                    implement, "_worktree_placement_violation",
                    return_value=None,
                )
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "run_build",
                    return_value={
                        "success": True,
                        "stdout": "no build",
                        "stderr": "",
                        "exit_code": 0,
                        "skipped": True,
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "run_gate_suite",
                    return_value=(
                        _tests_result(),
                        {
                            "tree": "abc1234",
                            "scope": "changed",
                            "cached": False,
                            "index_unchanged": True,
                        },
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "stage_tested_tree",
                    return_value={
                        "success": True,
                        "tree": "abc1234",
                        "expected": "abc1234",
                    },
                )
            )
            stack.enter_context(
                patch.object(implement, "run_tests", side_effect=_fake_run_tests)
            )
            stack.enter_context(
                patch.object(implement, "git_commit", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    implement, "git_get_commit_hash", return_value="abc1234"
                )
            )
            stack.enter_context(
                patch.object(implement, "git_push_to_dev", side_effect=_fake_push)
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "_assert_commit_on_origin_dev",
                    return_value={
                        "verified": True,
                        "resolved_commit": fake_commit,
                        "target": "refs/remotes/origin/dev",
                        "target_tip": fake_commit,
                    },
                )
            )
            stack.enter_context(patch.object(implement, "_assert_push_subject"))
            stack.enter_context(
                patch.object(implement, "run_cmd", side_effect=_fake_run_cmd)
            )
            stack.enter_context(
                patch.object(
                    implement, "_record_finish_own_churn", return_value=[]
                )
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "cleanup_worktree_processes",
                    return_value={
                        "method": "wl_cleanup_worktree",
                        "terminated": 0,
                        "warning": "",
                    },
                )
            )
            stack.enter_context(patch.object(implement, "remove_state"))
            stack.enter_context(
                patch.object(implement, "_remove_worktree", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "_sync_parent_branch",
                    return_value={"method": "skipped", "success": True},
                )
            )
            stack.enter_context(
                patch.object(implement, "wl_add_comment", return_value=True)
            )
            stack.enter_context(
                patch.object(implement, "_surface_postfix_prose")
            )
            stack.enter_context(
                patch.object(
                    implement,
                    "_snapshot_main_checkout",
                    return_value={"taken_at": "T", "status": {}},
                )
            )
            stack.enter_context(
                patch.object(
                    implement.StatusLifecycle,
                    "update_status",
                    MagicMock(),
                )
            )
            stack.enter_context(
                patch.object(
                    implement.StatusLifecycle,
                    "show",
                    MagicMock(
                        return_value={
                            "workItem": {
                                "status": "completed",
                                "stage": "in_review",
                            }
                        }
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    implement, "acquire_main_checkout_lock", _timed_acquire
                )
            )
            stack.enter_context(
                patch.object(
                    implement, "_release_main_checkout_lock", _timed_release
                )
            )
            yield {"implement": implement, "local": local}
    finally:
        if previous_timeout is None:
            os.environ.pop(implement.ENV_FINISH_LOCK_TIMEOUT, None)
        else:
            os.environ[implement.ENV_FINISH_LOCK_TIMEOUT] = previous_timeout
        if previous_skip is not None:
            os.environ[implement.ENV_TEST_SCOPE_SKIP] = previous_skip


def _load_implement():
    """Import ``implement.py`` from this repository (scripts are not a pkg)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from skill.implement.scripts import implement

    return implement


def measure_fanout(
    count: int = 6,
    *,
    suite_seconds: float = DEFAULT_SUITE_SECONDS,
    lock_window_seconds: float = DEFAULT_LOCK_WINDOW_SECONDS,
    lock_timeout_seconds: float = DEFAULT_FANOUT_LOCK_TIMEOUT,
    main_checkout: str | None = None,
) -> dict[str, Any]:
    """Run *count* concurrent ``phase_finish`` calls and measure lock waits.

    All sessions share one main checkout, so the per-repository ``flock`` is
    the real serialisation point. The expensive pre-lock phase (the dev-push
    full suite) is stubbed to *suite_seconds* and synchronised on a barrier so
    every session is genuinely inside it at the same moment — the fan-out the
    epic targets. The locked window is stubbed to *lock_window_seconds*.

    No real git, suite, worklog or session-store write happens; the temporary
    checkout lives under ``tempfile.mkdtemp`` unless *main_checkout* is given.

    Returns:
        A report with per-session rows, the aggregate and the parent's checks.
    """
    if count < 1:
        raise ValueError("count must be >= 1")

    cleanup: tempfile.TemporaryDirectory | None = None
    if main_checkout is None:
        cleanup = tempfile.TemporaryDirectory(prefix="finish-fanout-")
        main_checkout = str(Path(cleanup.name) / "main")
    Path(main_checkout).mkdir(parents=True, exist_ok=True)

    barrier = threading.Barrier(
        count, timeout=max(30.0, suite_seconds * 2 + 30.0)
    )
    rows: list[dict[str, Any]] = []
    rows_lock = threading.Lock()
    hung: list[threading.Thread] = []

    try:
        with _finish_fanout_harness(
            main_checkout,
            suite_seconds=suite_seconds,
            lock_window_seconds=lock_window_seconds,
            lock_timeout_seconds=lock_timeout_seconds,
            barrier=barrier,
        ) as harness:
            implement = harness["implement"]
            local = harness["local"]

            def _run(index: int) -> None:
                label = f"fanout-{index}"
                started = time.monotonic()
                try:
                    report = implement.phase_finish(
                        "WL-FANOUT", json_output=True, no_refactor=True
                    )
                except Exception as error:  # noqa: BLE001 - reported per row
                    report = {"success": False, "message": repr(error)}
                wall = time.monotonic() - started
                lock_step = (report.get("steps") or {}).get(
                    "main_checkout_lock"
                ) or {}
                wait = float(lock_step.get("waited_seconds") or 0.0)
                acquired_at = getattr(local, "acquired_at", None)
                released_at = getattr(local, "released_at", None)
                hold = (
                    released_at - acquired_at
                    if acquired_at is not None and released_at is not None
                    else 0.0
                )
                row = {
                    "session": label,
                    "success": bool(report.get("success")),
                    "wall_seconds": _round(wall),
                    "lock_wait_seconds": _round(wait),
                    "lock_hold_seconds": _round(hold),
                    "lock_share": _share(wait, wall),
                    "lock_acquired": bool(lock_step.get("acquired")),
                    "message": (
                        None if report.get("success")
                        else str(report.get("message") or "")[:400]
                    ),
                }
                with rows_lock:
                    rows.append(row)

            threads = [
                threading.Thread(target=_run, args=(index,), daemon=True)
                for index in range(1, count + 1)
            ]
            # ``phase_finish(json_output=True)`` prints each report and logs
            # progress. Swallow that chatter for the whole fan-out (one global
            # redirect, never a per-thread one) so the harness output — and a
            # ``--json`` run — stays parseable.
            with contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=max(60.0, lock_timeout_seconds))
            hung = [t for t in threads if t.is_alive()]
    finally:
        if cleanup is not None:
            cleanup.cleanup()

    rows.sort(key=lambda row: row["session"])
    waits = [row["lock_wait_seconds"] for row in rows]
    walls = [row["wall_seconds"] for row in rows]
    holds = [row["lock_hold_seconds"] for row in rows]
    slowest = max(walls) if walls else 0.0
    checks = _fanout_checks(rows)
    if hung:
        checks.insert(
            0,
            {
                "id": "all_sessions_completed",
                "passed": False,
                "detail": f"{len(hung)} finish thread(s) never completed",
            },
        )
    failures = [check["id"] for check in checks if check["passed"] is False]
    verdict = "fail" if failures else "pass"

    return {
        "success": bool(rows) and not failures,
        "mode": "fanout",
        "method": (
            "real phase_finish x N against one main checkout, real "
            "per-repository flock; suites and git boundaries stubbed"
        ),
        "parameters": {
            "count": count,
            "suite_seconds": _round(suite_seconds),
            "lock_window_seconds": _round(lock_window_seconds),
            "lock_timeout_seconds": _round(lock_timeout_seconds),
        },
        "aggregate": {
            "completed": len(rows),
            "slowest_session_seconds": _round(slowest),
            "summed_session_seconds": _round(sum(walls)),
            "effective_parallelism": (
                _round(sum(walls) / slowest, 2) if slowest else 0.0
            ),
            "median_lock_wait_seconds": (
                _round(statistics.median(waits)) if waits else None
            ),
            "max_lock_wait_seconds": _round(max(waits)) if waits else None,
            "median_lock_hold_seconds": (
                _round(statistics.median(holds)) if holds else None
            ),
            "max_lock_share": (
                _round(max(row["lock_share"] for row in rows)) if rows else None
            ),
        },
        "thresholds": {
            "median_lock_wait_seconds": MEDIAN_LOCK_WAIT_SECONDS,
            "max_lock_wait_seconds": MAX_LOCK_WAIT_SECONDS,
            "majority_lock_share": MAJORITY_LOCK_SHARE,
        },
        "checks": checks,
        "verdict": verdict,
        "failures": failures,
        "sessions": rows,
    }


# ---------------------------------------------------------------------------
# Before/after comparison
# ---------------------------------------------------------------------------


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Side-by-side before/after rows for the same metric set.

    Both reports must come from :func:`analyse_window` (same method, different
    windows) so the delta is meaningful; a report from another mode is
    rejected.
    """
    for label, report in (("before", before), ("after", after)):
        if report.get("mode") != "session":
            raise ValueError(f"{label} report is not a session analysis")
    before_agg = before.get("aggregate") or {}
    after_agg = after.get("aggregate") or {}

    def _lock(report: dict, key: str):
        return ((report.get("aggregate") or {}).get("lock") or {}).get(key)

    rows = [
        ("sessions", (before.get("selection") or {}).get("sessions"),
         (after.get("selection") or {}).get("sessions")),
        ("summed_session_minutes", before_agg.get("summed_session_minutes"),
         after_agg.get("summed_session_minutes")),
        ("wall_clock_minutes", before_agg.get("wall_clock_minutes"),
         after_agg.get("wall_clock_minutes")),
        ("effective_parallelism", before_agg.get("effective_parallelism"),
         after_agg.get("effective_parallelism")),
        ("peak_concurrency", before_agg.get("peak_concurrency"),
         after_agg.get("peak_concurrency")),
        ("bash_share", before_agg.get("bash_share"),
         after_agg.get("bash_share")),
        (
            "wait_for_job_minutes",
            (before_agg.get("buckets") or {}).get("wait_for_job", {}).get(
                "minutes"
            ),
            (after_agg.get("buckets") or {}).get("wait_for_job", {}).get(
                "minutes"
            ),
        ),
        (
            "own_finish_minutes",
            (before_agg.get("buckets") or {}).get("own_finish", {}).get(
                "minutes"
            ),
            (after_agg.get("buckets") or {}).get("own_finish", {}).get(
                "minutes"
            ),
        ),
        ("lock_refusals", before_agg.get("lock", {}).get("refusals"),
         after_agg.get("lock", {}).get("refusals")),
        ("lock_wait_samples", _lock(before, "samples"),
         _lock(after, "samples")),
        ("lock_wait_median_seconds", _lock(before, "median_seconds"),
         _lock(after, "median_seconds")),
        ("lock_wait_max_seconds", _lock(before, "max_seconds"),
         _lock(after, "max_seconds")),
        ("rebase_events", before_agg.get("rebase_events"),
         after_agg.get("rebase_events")),
        ("full_suite_gate_runs", before_agg.get("full_suite_gate_runs"),
         after_agg.get("full_suite_gate_runs")),
        ("no_op_sessions", before_agg.get("no_op_sessions"),
         after_agg.get("no_op_sessions")),
    ]

    metrics = [
        {"metric": name, "before": before_value, "after": after_value}
        for name, before_value, after_value in rows
    ]
    return {
        "mode": "comparison",
        "before": {
            "selection": before.get("selection"),
            "verdict": before.get("verdict"),
        },
        "after": {
            "selection": after.get("selection"),
            "verdict": after.get("verdict"),
        },
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_seconds(value: float | None) -> str:
    """Render a seconds value for a table (``-`` when no samples exist)."""
    return "-" if value is None else f"{value}s"


def render_session_report(report: dict[str, Any]) -> str:
    """Plain-text session report (aggregate + checks)."""
    selection = report["selection"]
    window = report["window"]
    agg = report["aggregate"]
    lock = agg["lock"]
    lines = [
        f"window: {selection['since']} .. {selection['until']} "
        f"(project filters: {', '.join(selection['project_filters']) or 'all'})",
        f"sessions: {selection['sessions']} of {selection['files_scanned']} "
        f"file(s) scanned (implement-only: {selection['implement_only']})",
        f"elapsed: {window['wall_clock_minutes']} min; "
        f"summed session time {agg['summed_session_minutes']} min "
        f"({agg['effective_parallelism']}x effective parallelism, "
        f"peak concurrency {agg['peak_concurrency']})",
        f"bash wait: {agg['bash_minutes']} min "
        f"({agg['bash_share']:.0%} of summed session time)",
        "",
        f"{'bucket':<16}{'minutes':>9}{'share':>8}",
    ]
    for name in BUCKETS:
        bucket = agg["buckets"][name]
        lines.append(
            f"{name:<16}{bucket['minutes']:>9.1f}{bucket['share']:>8.0%}"
        )
    lines.extend(
        [
            "",
            f"lock refusals: {lock['refusals']}; "
            f"lock-wait samples: {lock['samples']} "
            f"(median {_render_seconds(lock['median_seconds'])}, "
            f"max {_render_seconds(lock['max_seconds'])})",
            f"sessions with a lock wait: {lock['sessions_with_wait']}; "
            f"majority-blocked: {len(lock['majority_blocked_sessions'])}",
            f"rebase events: {agg['rebase_events']}; "
            f"full-suite gate runs: {agg['full_suite_gate_runs']}; "
            f"no-op sessions: {agg['no_op_sessions']}",
            "",
        ]
    )
    for check in report["checks"]:
        marker = (
            "PASS" if check["passed"] is True
            else "FAIL" if check["passed"] is False
            else "N/A "
        )
        lines.append(f"  [{marker}] {check['id']}: {check['detail']}")
    lines.append(f"verdict: {report['verdict'].upper()}")
    return "\n".join(lines)


def render_fanout_report(report: dict[str, Any]) -> str:
    """Plain-text fan-out report (per-session table + checks)."""
    params = report["parameters"]
    agg = report["aggregate"]
    lines = [
        f"{params['count']}-way concurrent finish fan-out "
        f"(suite stub {params['suite_seconds']}s, locked window stub "
        f"{params['lock_window_seconds']}s)",
        f"{'session':<12}{'wall s':>9}{'lock wait s':>13}"
        f"{'lock hold s':>13}{'lock share':>11}  ok",
    ]
    for row in report["sessions"]:
        lines.append(
            f"{row['session']:<12}{row['wall_seconds']:>9.2f}"
            f"{row['lock_wait_seconds']:>13.2f}"
            f"{row['lock_hold_seconds']:>13.2f}"
            f"{row['lock_share']:>10.0%}  {'yes' if row['success'] else 'NO'}"
        )
    lines.extend(
        [
            "",
            f"slowest wall clock: {agg['slowest_session_seconds']}s; "
            f"effective parallelism {agg['effective_parallelism']}x; "
            f"median lock wait {agg['median_lock_wait_seconds']}s; "
            f"max lock wait {agg['max_lock_wait_seconds']}s",
            "",
        ]
    )
    for check in report["checks"]:
        marker = (
            "PASS" if check["passed"] is True
            else "FAIL" if check["passed"] is False
            else "N/A "
        )
        lines.append(f"  [{marker}] {check['id']}: {check['detail']}")
    lines.append(f"verdict: {report['verdict'].upper()}")
    return "\n".join(lines)


def render_comparison(report: dict[str, Any]) -> str:
    """Plain-text before/after table."""
    lines = [
        f"{'metric':<26}{'before':>14}{'after':>14}",
        "-" * 54,
    ]
    for row in report["metrics"]:
        lines.append(
            f"{row['metric']:<26}{row['before']!s:>14}"
            f"{row['after']!s:>14}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_finish_contention.py",
        description=(
            "Measure implement.py finish lock contention from session logs "
            "and a synthetic concurrent fan-out (SA-0MWFK10NH006QI7E)."
        ),
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    session = subparsers.add_parser(
        "session",
        help="Analyse pi session logs with the 2026-11-13 baseline buckets.",
    )
    session.add_argument(
        "--sessions-root",
        default=DEFAULT_SESSIONS_ROOT,
        help=f"Session-log root (default {DEFAULT_SESSIONS_ROOT}).",
    )
    session.add_argument(
        "--since",
        default=None,
        help="ISO-8601 window start on a session's first record.",
    )
    session.add_argument(
        "--until",
        default=None,
        help="ISO-8601 window end (exclusive) on a session's first record.",
    )
    session.add_argument(
        "--project",
        action="append",
        default=[],
        help=(
            "Only sessions whose directory path contains this substring. "
            "Repeatable (e.g. --project AI_Hell --project SorraAgents)."
        ),
    )
    session.add_argument(
        "--all-sessions",
        action="store_true",
        help="Count every session, not only implement-skill sessions.",
    )
    session.add_argument(
        "--min-samples",
        type=int,
        default=MIN_LOCK_SAMPLES,
        help=(
            "Lock-wait samples needed before the thresholds are evaluated "
            f"(default {MIN_LOCK_SAMPLES})."
        ),
    )
    session.add_argument(
        "--baseline",
        default=None,
        help=(
            "A saved session-mode JSON report to compare against "
            "(before/after table)."
        ),
    )
    session.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a table."
    )
    session.add_argument(
        "--out", default=None, help="Write the JSON report to this path."
    )

    fanout = subparsers.add_parser(
        "fanout",
        help="Run a synthetic N-way concurrent phase_finish fan-out.",
    )
    fanout.add_argument(
        "--count",
        type=int,
        default=6,
        help="Concurrent finish sessions (default 6 — the epic's target).",
    )
    fanout.add_argument(
        "--suite-seconds",
        type=float,
        default=DEFAULT_SUITE_SECONDS,
        help=(
            "Pre-lock full-suite stub duration per session "
            f"(default {DEFAULT_SUITE_SECONDS:g}s, the 2026-11-13 baseline)."
        ),
    )
    fanout.add_argument(
        "--lock-window-seconds",
        type=float,
        default=DEFAULT_LOCK_WINDOW_SECONDS,
        help=(
            "Locked mutating-window stub duration per session "
            f"(default {DEFAULT_LOCK_WINDOW_SECONDS:g}s)."
        ),
    )
    fanout.add_argument(
        "--lock-timeout-seconds",
        type=float,
        default=DEFAULT_FANOUT_LOCK_TIMEOUT,
        help=(
            "Bounded wait configured via IMPLEMENT_FINISH_LOCK_TIMEOUT "
            f"(default {DEFAULT_FANOUT_LOCK_TIMEOUT:g}s)."
        ),
    )
    fanout.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a table."
    )
    fanout.add_argument(
        "--out", default=None, help="Write the JSON report to this path."
    )
    return parser


def _write_out(report: dict[str, Any], path: str | None) -> None:
    """Write the report JSON to *path* when requested (only explicit writes)."""
    if not path:
        return
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit status."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.mode == "session":
        try:
            report = analyse_window(
                args.sessions_root,
                since=args.since,
                until=args.until,
                projects=args.project,
                all_sessions=args.all_sessions,
                min_samples=args.min_samples,
            )
        except ValueError as error:
            parser.error(str(error))
        _write_out(report, args.out)
        if not report["success"]:
            print(
                "error: no implement sessions found under "
                f"{report['sessions_root']} for the requested window "
                f"({report['selection']['files_scanned']} file(s) scanned).",
                file=sys.stderr,
            )
            return 1
        comparison = None
        if args.baseline:
            try:
                baseline = json.loads(
                    Path(args.baseline).expanduser().read_text(encoding="utf-8")
                )
                comparison = compare_reports(baseline, report)
            except (OSError, ValueError) as error:
                parser.error(f"cannot compare with --baseline: {error}")
        if args.json:
            payload = report if comparison is None else {
                "after": report,
                "comparison": comparison,
            }
            print(json.dumps(payload, indent=2, sort_keys=False))
        else:
            if comparison is not None:
                print(render_comparison(comparison))
                print()
            print(render_session_report(report))
        return 0 if report["verdict"] != "fail" else 1

    try:
        report = measure_fanout(
            args.count,
            suite_seconds=args.suite_seconds,
            lock_window_seconds=args.lock_window_seconds,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
    except ValueError as error:
        parser.error(str(error))
    _write_out(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(render_fanout_report(report))
    return 0 if report["verdict"] != "fail" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
