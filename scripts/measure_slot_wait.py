#!/usr/bin/env python3
"""Post-fix slot-wait re-measurement over the pi session-log corpus.

Read-only companion to :mod:`measure_audit_performance`, built for
**SA-0MW7C69KU008ZT5K** (parent **SA-0MVXUCGPK003G2KV**) to prove — or refute —
with counted data that the 2026-10-31 fast-lane fix (``SA-0MVWZQ269009SJXV``,
shipped ``a2659c38`` on 2026-10-31T23:21) removed host test-slot starvation.

Why this exists
---------------
The starvation RCA was closed on an ad hoc 30-log scan that found only 12
``slot_wait_seconds`` samples, all 0.00 s. That was too small to close the
question and said nothing about the two dominant wait categories — the
``implement finish`` gate and the dev-push full-suite gate — which surface
inside ``wait_for_job`` output rather than as a bare ``run_tests.py`` JSON
result. This harness is the reproducible replacement: the same corpus always
yields the same report.

Corpus
------
Every ``*.jsonl`` file under ``--sessions-root`` (default
``~/.pi/agent/sessions``). Each line is a pi session record; only records whose
``timestamp`` is at or after ``--since`` are counted. The default ``--since``
is the fix commit time, so the window is the post-fix period (or every session
since that instant if fewer).

Attribution — "genuine run" filter
----------------------------------
A measurement is trusted only when the tool result that carried it was produced
by a **test-slot consumer's own command** — ``run_tests.py``,
``implement.py``, ``git push`` or ``wait_for_job.py``. Session logs are full of
*echoed* fragments (a ``grep`` of another log, a ``python3 -c`` probe, a ``cat``
of the runner source) that contain the same ``slot_wait_seconds`` / ``queued N s``
text without being a real run. Counting them inflates the corpus and can invent
a >120 s sample out of a doc snippet. The filter pairs each ``toolResult`` with
its assistant ``toolCall`` command and drops results from any other command.
Results with no resolvable command (e.g. a forked/compacted log) are kept only
when their text carries a structural run marker, so genuine data is never lost
to a missing call record.

Wait categories
---------------
=====================  ==========================================================
``run_tests_full``     ``run_tests.py`` slot wait, ``full`` lane (``test`` semaphore)
``run_tests_fast``     ``run_tests.py`` slot wait, ``fast`` lane (``test-fast``,
                       i.e. the partial run)
``implement_finish``   ``implement finish`` wait, from the
                       ``Implement run_tests: test slot acquired — … wait_seconds=``
                       telemetry line
``dev_push_gate``      dev-push full-suite gate wait, from the aggregate
                       ``slot_wait_seconds`` of a gate-provenance block
=====================  ==========================================================

Double-count avoidance
----------------------
* A ``run_tests.py`` result object carries **both** per-suite samples
  (``"slot_wait_seconds": X, "slot_lane": "full"``) and an aggregate (the sum,
  with ``"slot_lanes"``). Counting both double-counts one run. For
  ``run_tests`` the per-suite entries win; the aggregate is used only when no
  per-suite entry is present and it names a single lane.
* A finish/gate block also contains the inner ``run_tests.py`` result. Those
  JSON samples are ignored — the finish wait comes from the telemetry line and
  the gate wait from the aggregate — so a single acquisition is counted once.
* A finish telemetry line repeats whenever a ``wait_for_job`` tail re-prints the
  finish log, so finish samples are de-duplicated by their ``queued_at`` clock.
* ``queued N s`` notices are a text rendering of a suite's ``slot_wait_seconds``
  (printed only when the wait reaches 1 s). They are collected for the
  "no notice ≥ 30 s" check and de-duplicated by ``(lane, rounded seconds)`` so
  an echoed copy cannot invent a second notice.

Read-only guarantee
-------------------
The harness only reads files. It never runs a test, never acquires a semaphore
slot, and never writes to the session store, ``.worklog/`` or any log.

Usage::

    python3 scripts/measure_slot_wait.py
    python3 scripts/measure_slot_wait.py --since 2026-10-31T23:21:00Z --json
    python3 scripts/measure_slot_wait.py --sessions-root ~/.pi/agent/sessions

Exit status is ``0`` when the report's pass/fail checks all pass (or a category
is under-sampled and therefore not evaluable), and ``1`` when at least one
check fails. The ``--json`` report is the source of truth either way.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Default session-log root (the pi session store).
DEFAULT_SESSIONS_ROOT = "~/.pi/agent/sessions"

#: The fast-lane fix (``SA-0MVWZQ269009SJXV``) commit and its timestamp — the
#: measurement window start. Anything before this predates the fix.
POST_FIX_COMMIT = "a2659c38"
DEFAULT_SINCE = "2026-10-31T23:21:00Z"

#: Parent thresholds (``SA-0MVXUCGPK003G2KV`` AC3).
MAX_SLOT_WAIT_SECONDS = 120.0
PARTIAL_P90_SECONDS = 30.0
QUEUED_NOTICE_SECONDS = 30.0

#: A category with fewer samples than this is not summarised as a percentile;
#: it is labelled "no regression observed in N samples" with a re-check date
#: (AC4). The max is still reported so a lone outlier is never hidden.
MIN_SAMPLES_FOR_PERCENTILE = 10

#: How far ahead a re-check is scheduled for an under-sampled category.
RECHECK_DAYS = 14

#: JSON ``slot_lane`` value -> test-slot semaphore, per the AC's lane mapping
#: (``test`` = full, ``test-fast`` = partial).
LANE_SEMAPHORE = {"full": "test", "fast": "test-fast"}

#: Commands that actually consume a test slot. A tool result produced by any
#: other command is an echo (grep/probe/cat) and is not a measurement.
_RUN_SOURCE_COMMAND = re.compile(
    r"run_tests\.py|implement\.py|\bgit\b[^\n]*\bpush\b|wait_for_job\.py"
)

#: A run_tests.py result object starts with one of these structural keys. Used
#: as a fallback for a tool result whose command is not resolvable.
_STRUCTURAL_MARKERS = (
    '"suites"',
    "run_tests: gate-subject",
    "run_tests: gate-verdict",
    "Implement run_tests:",
    '"notice_codes"',
)

#: Per-suite slot sample inside a run_tests.py JSON result.
_PER_SUITE_RE = re.compile(
    r'"slot_wait_seconds"\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*,\s*'
    r'"slot_lane"\s*:\s*"(full|fast)"'
)

#: Aggregate slot sample inside a run_tests.py JSON result.
_AGGREGATE_RE = re.compile(
    r'"slot_wait_seconds"\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*,\s*'
    r'"slot_lanes"\s*:\s*\[([^\]]*)\]'
)

#: ``implement finish``'s slot-wait telemetry line (em-dash or hyphen).
_FINISH_RE = re.compile(
    r"Implement run_tests: test slot acquired\s*[—-]\s*"
    r"queued_at=([0-9]+(?:\.[0-9]+)?)\s+wait_seconds=([0-9]+(?:\.[0-9]+)?)"
)

#: Indented ``queued N.Ns for a <lane>-lane test slot`` notice (text output).
_QUEUED_RE = re.compile(
    r"(?m)^[ \t]+queued\s+([0-9]+(?:\.[0-9]+)?)s\s+for\s+an?\s+"
    r"(full|fast)-lane\s+test\s+slot"
)

#: A suite result header in run_tests.py text output, e.g.
#: ``all: PASS [full scope] [type full] [full lane] (pytest …)``.
_SUITE_HEADER_RE = re.compile(r"\[(?:full|fast) lane\]")

#: Gate-provenance / dev-push markers.
_GATE_MARKERS = (
    "run_tests: gate-subject",
    "run_tests: gate-verdict",
    "run_tests: gate-root",
    "run_tests: gate-refused",
    "pre-push: full-suite gate",
    "pre-push: test gate",
)

#: ``gate-subject … pushed=<sha>`` — the gate run's identity for de-duplication.
_GATE_SHA_RE = re.compile(r"run_tests: gate-subject\s+pushed=(\S+)")


def _round(value: float, digits: int = 3) -> float:
    """Round for stable, diff-friendly output (avoids ``-0.0``)."""
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Linear-interpolation percentile over an ascending, non-empty list.

    Uses the same definition as ``numpy.percentile`` (the default "linear"
    method) so the numbers can be cross-checked with an ad hoc calculation.
    """
    count = len(sorted_values)
    if count == 0:
        return 0.0
    if count == 1:
        return sorted_values[0]
    rank = (count - 1) * percentile / 100.0
    lower = math.floor(rank)
    upper = min(lower + 1, count - 1)
    weight = rank - lower
    return sorted_values[lower] + (
        sorted_values[upper] - sorted_values[lower]
    ) * weight


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse an ISO-8601 session timestamp (``Z`` or offset) into UTC."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iter_text_blocks(message: dict):
    """Yield the text carried by a session message's content blocks."""
    content = message.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                yield block.get("text", "")


def _normalise(text: str) -> str:
    """Un-escape JSON-in-JSON tool output.

    A run_tests.py result is often printed *inside* another JSON string, so its
    quotes arrive escaped (``\\"slot_lane\\"``). Replacing the escape pair with a
    real quote is idempotent for already-unescaped text and lets one regex set
    match both forms.
    """
    return text.replace('\\"', '"')


def _is_run_source_command(command: str) -> bool:
    """True when a bash command actually consumes a test slot."""
    return bool(_RUN_SOURCE_COMMAND.search(command or ""))


def _has_structural_marker(text: str) -> bool:
    """True when a block carries a run_tests.py / finish / gate structure."""
    return any(marker in text for marker in _STRUCTURAL_MARKERS)


def parse_block(text: str) -> dict:
    """Extract wait samples and notices from one text block.

    The function is pure and independent of the session-log envelope so it can
    be exercised on synthetic fixtures (AC5). It does **not** de-duplicate
    across blocks; callers own the corpus-wide de-duplication keys.

    Returns a dict of lists:

    * ``run_tests`` — ``(lane, seconds)`` per-suite samples.
    * ``finish`` — ``(queued_at, seconds)`` finish telemetry samples.
    * ``gate`` — ``(sha|None, rounded_seconds, lanes_tuple)`` gate samples.
    * ``queued`` — ``(lane, seconds)`` ``queued N s`` notices.

    Classification precedence — a block is a gate block or a finish block or a
    bare ``run_tests`` block, never two at once — is what prevents a single
    acquisition from being counted twice.
    """
    text = _normalise(text)

    per_suite = _PER_SUITE_RE.findall(text)
    aggregate = _AGGREGATE_RE.findall(text)
    finish_lines = _FINISH_RE.findall(text)

    is_gate = any(marker in text for marker in _GATE_MARKERS)
    is_finish = ("Implement run_tests:" in text) or ('"phase": "finish"' in text)

    run_tests: list[tuple[str, float]] = []
    finish: list[tuple[str, float]] = []
    gate: list[tuple[str | None, float, tuple[str, ...]]] = []
    queued: list[tuple[str, float]] = []

    # Finish telemetry is authoritative for the finish category and is always
    # collected, even when the block also names a gate (a finish that pushes).
    for queued_at, wait in finish_lines:
        finish.append((queued_at, float(wait)))

    if is_gate:
        sha_match = _GATE_SHA_RE.search(text)
        sha = sha_match.group(1) if sha_match else None
        for seconds, lanes in aggregate:
            lanes_tuple = tuple(re.findall(r'"(full|fast)"', lanes))
            if not lanes_tuple:
                continue
            gate.append((sha, _round(float(seconds), 6), lanes_tuple))
    elif is_finish:
        # A finish block's JSON is the same acquisition as its telemetry line.
        # Only fall back to the JSON when the telemetry line is absent (older
        # logs), so the wait is counted exactly once.
        if not finish:
            for seconds, _lane in per_suite:
                finish.append(("", float(seconds)))
    else:
        # A bare run_tests block: per-suite entries carry lane attribution and
        # win over the aggregate (which is their sum) to avoid double counting.
        if per_suite:
            for seconds, lane in per_suite:
                run_tests.append((lane, float(seconds)))
        elif aggregate:
            for seconds, lanes in aggregate:
                lanes_tuple = tuple(set(re.findall(r'"(full|fast)"', lanes)))
                if len(lanes_tuple) == 1:
                    run_tests.append((lanes_tuple[0], float(seconds)))

    # ``queued N s`` notices only count as genuine run text output: a bare grep
    # echo has no suite header / facts line and is excluded here.
    if ("facts:" in text) or _SUITE_HEADER_RE.search(text):
        for seconds, lane in _QUEUED_RE.findall(text):
            queued.append((lane, float(seconds)))

    return {
        "run_tests": run_tests,
        "finish": finish,
        "gate": gate,
        "queued": queued,
    }


def _iter_session_files(root: Path) -> list[Path]:
    """Return the session-log corpus in a deterministic order."""
    if root.is_file():
        return [root]
    return sorted(path for path in root.rglob("*.jsonl") if path.is_file())


def _commands_by_tool_call(lines: list[str]) -> dict[str, str]:
    """Map ``toolCallId`` -> bash command for assistant tool calls in a file."""
    commands: dict[str, str] = {}
    for line in lines:
        if "toolCall" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message", {})
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "toolCall"
                and block.get("name") == "bash"
            ):
                arguments = block.get("arguments") or {}
                command = arguments.get("command", "") or ""
                call_id = block.get("id")
                if call_id is not None:
                    commands[call_id] = command
    return commands


def measure(
    sessions_root: str | Path = DEFAULT_SESSIONS_ROOT,
    *,
    since: str = DEFAULT_SINCE,
    min_samples: int = MIN_SAMPLES_FOR_PERCENTILE,
) -> dict:
    """Aggregate the session-log corpus into a slot-wait report.

    Args:
        sessions_root: Session-log root directory (or a single ``*.jsonl``).
        since: ISO-8601 window start; records before it are ignored.
        min_samples: Below this a category is labelled, not percentiled.

    Returns:
        A JSON-serialisable report dict (see the module docstring).
    """
    root = Path(sessions_root).expanduser()
    since_dt = _parse_timestamp(since)
    if since_dt is None:
        raise ValueError(f"invalid --since timestamp: {since!r}")

    files = _iter_session_files(root)

    # Per-category raw samples.
    run_tests_full: list[float] = []
    run_tests_fast: list[float] = []
    finish_samples: list[float] = []
    gate_samples: list[float] = []

    finish_seen: set[str] = set()
    gate_seen: set[tuple] = set()
    queued_seen: set[tuple[str, float]] = set()
    queued: list[tuple[str, float, str]] = []

    files_with_samples = 0
    records_in_window = 0
    max_ts: datetime | None = None

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        commands = _commands_by_tool_call(lines)
        file_had_sample = False

        for line in lines:
            if not any(
                key in line
                for key in ("slot_wait_seconds", "Implement run_tests", "queued ")
            ):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            timestamp = _parse_timestamp(record.get("timestamp", ""))
            if timestamp is None or timestamp < since_dt:
                continue
            records_in_window += 1
            message = record.get("message", {})
            if message.get("role") != "toolResult":
                continue

            # Genuine-run filter: reject echoes produced by analysis commands.
            call_id = message.get("toolCallId")
            command = commands.get(call_id) if call_id is not None else None
            if command is not None and not _is_run_source_command(command):
                continue

            for raw_text in _iter_text_blocks(message):
                if command is None and not _has_structural_marker(raw_text):
                    continue
                parsed = parse_block(raw_text)

                for lane, seconds in parsed["run_tests"]:
                    (run_tests_full if lane == "full" else run_tests_fast).append(
                        seconds
                    )
                    file_had_sample = True

                for queued_at, seconds in parsed["finish"]:
                    key = queued_at or f"{path}:{seconds}"
                    if key in finish_seen:
                        continue
                    finish_seen.add(key)
                    finish_samples.append(seconds)
                    file_had_sample = True

                for sha, rounded, lanes_tuple in parsed["gate"]:
                    key = (sha, rounded, lanes_tuple)
                    if key in gate_seen:
                        continue
                    gate_seen.add(key)
                    gate_samples.append(rounded)
                    file_had_sample = True

                for lane, seconds in parsed["queued"]:
                    key = (lane, round(seconds, 1))
                    if key in queued_seen:
                        continue
                    queued_seen.add(key)
                    queued.append((lane, seconds, timestamp.isoformat()))
                    file_had_sample = True

            if max_ts is None or timestamp > max_ts:
                max_ts = timestamp

        if file_had_sample:
            files_with_samples += 1

    all_samples = run_tests_full + run_tests_fast + finish_samples + gate_samples
    max_all = max(all_samples) if all_samples else 0.0

    categories = [
        _category_report(
            "run_tests_full",
            "run_tests.py slot wait — full lane (test semaphore)",
            run_tests_full,
            lane="full",
            min_samples=min_samples,
            window_end=max_ts,
        ),
        _category_report(
            "run_tests_fast",
            "run_tests.py slot wait — partial lane (test-fast semaphore)",
            run_tests_fast,
            lane="fast",
            min_samples=min_samples,
            window_end=max_ts,
        ),
        _category_report(
            "implement_finish",
            "implement finish wait",
            finish_samples,
            lane=None,
            min_samples=min_samples,
            window_end=max_ts,
        ),
        _category_report(
            "dev_push_gate",
            "dev-push full-suite gate wait",
            gate_samples,
            lane=None,
            min_samples=min_samples,
            window_end=max_ts,
        ),
    ]

    fast_row = next(row for row in categories if row["category"] == "run_tests_fast")

    checks = _evaluate_checks(
        max_all=max_all,
        fast_row=fast_row,
        queued=queued,
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
        "success": files_with_samples > 0,
        "sessions_root": str(root),
        "since": since_dt.isoformat(),
        "post_fix_commit": POST_FIX_COMMIT,
        "window_start": since_dt.isoformat(),
        "window_end": max_ts.isoformat() if max_ts else None,
        "files_scanned": len(files),
        "files_with_samples": files_with_samples,
        "records_in_window": records_in_window,
        "total_samples": len(all_samples),
        "categories": categories,
        "queued_notices": {
            "distinct": len(queued),
            "max_seconds": _round(max((value for _, value, _ in queued), default=0.0)),
            "threshold_seconds": QUEUED_NOTICE_SECONDS,
            "samples": [
                {"lane": lane, "seconds": _round(value), "at": at}
                for lane, value, at in sorted(queued, key=lambda item: item[1])
            ],
        },
        "thresholds": {
            "max_slot_wait_seconds": MAX_SLOT_WAIT_SECONDS,
            "partial_p90_seconds": PARTIAL_P90_SECONDS,
            "queued_notice_seconds": QUEUED_NOTICE_SECONDS,
            "min_samples_for_percentile": min_samples,
        },
        "checks": checks,
        "verdict": verdict,
        "failures": failures,
    }


def _category_report(
    category: str,
    description: str,
    values: list[float],
    *,
    lane: str | None,
    min_samples: int,
    window_end: datetime | None,
) -> dict:
    """Summarise one wait category (percentiles only when reliable)."""
    count = len(values)
    reliable = count >= min_samples
    ascending = sorted(values)
    p50 = _percentile(ascending, 50) if reliable else None
    p90 = _percentile(ascending, 90) if reliable else None
    max_seconds = _round(max(values)) if values else None

    note = None
    recheck_after = None
    if not reliable:
        if window_end is not None:
            recheck_after = (window_end + timedelta(days=RECHECK_DAYS)).date().isoformat()
        note = (
            f"no regression observed in {count} samples; re-check after "
            f"{recheck_after}" if recheck_after else
            f"no regression observed in {count} samples"
        )

    return {
        "category": category,
        "description": description,
        "lane": lane,
        "lane_semaphore": LANE_SEMAPHORE.get(lane) if lane else None,
        "calls": count,
        "samples": count,
        "reliable": reliable,
        "p50_seconds": _round(p50) if p50 is not None else None,
        "p90_seconds": _round(p90) if p90 is not None else None,
        "max_seconds": max_seconds,
        "note": note,
        "recheck_after": recheck_after,
    }


def _evaluate_checks(
    *,
    max_all: float,
    fast_row: dict,
    queued: list[tuple[str, float, str]],
    min_samples: int,
) -> list[dict]:
    """Evaluate the parent's three pass/fail thresholds (AC3).

    A check is ``passed: None`` (not ``False``) when the sample is too thin to
    judge, so an under-sampled window never reads as a hard failure.
    """
    checks: list[dict] = []

    checks.append(
        {
            "id": "no_run_over_120s",
            "passed": max_all <= MAX_SLOT_WAIT_SECONDS,
            "observed_seconds": _round(max_all),
            "threshold_seconds": MAX_SLOT_WAIT_SECONDS,
            "detail": (
                f"max slot wait across all categories is {_round(max_all)}s "
                f"(threshold {MAX_SLOT_WAIT_SECONDS:g}s)"
            ),
        }
    )

    if fast_row["reliable"]:
        fast_p90 = fast_row["p90_seconds"]
        checks.append(
            {
                "id": "partial_p90_under_30s",
                "passed": (fast_p90 is not None) and (fast_p90 < PARTIAL_P90_SECONDS),
                "observed_seconds": fast_p90,
                "threshold_seconds": PARTIAL_P90_SECONDS,
                "samples": fast_row["calls"],
                "detail": (
                    f"test-fast p90 is {fast_p90}s over "
                    f"{fast_row['calls']} samples (threshold <{PARTIAL_P90_SECONDS:g}s)"
                ),
            }
        )
    else:
        checks.append(
            {
                "id": "partial_p90_under_30s",
                "passed": None,
                "observed_seconds": None,
                "threshold_seconds": PARTIAL_P90_SECONDS,
                "samples": fast_row["calls"],
                "detail": (
                    f"only {fast_row['calls']} test-fast samples "
                    f"(need {min_samples}); p90 not evaluable"
                ),
            }
        )

    max_notice = max((value for _, value, _ in queued), default=0.0)
    checks.append(
        {
            "id": "no_queued_notice_30s",
            "passed": max_notice < QUEUED_NOTICE_SECONDS,
            "observed_seconds": _round(max_notice),
            "threshold_seconds": QUEUED_NOTICE_SECONDS,
            "detail": (
                f"max queued notice is {_round(max_notice)}s over "
                f"{len(queued)} distinct notices (threshold <{QUEUED_NOTICE_SECONDS:g}s)"
            ),
        }
    )
    return checks


def render_table(report: dict) -> str:
    """Render the per-category table (plus a TOTAL row) as plain text."""
    header = (
        f"{'Category':<22}{'Lane':>7}{'Calls':>7}{'p50':>9}{'p90':>9}"
        f"{'max':>9}  Note"
    )
    rule = "-" * len(header)
    lines = [header, rule]
    for row in report["categories"]:
        p50 = f"{row['p50_seconds']:.2f}" if row["p50_seconds"] is not None else "-"
        p90 = f"{row['p90_seconds']:.2f}" if row["p90_seconds"] is not None else "-"
        maximum = f"{row['max_seconds']:.2f}" if row["max_seconds"] is not None else "-"
        lane = row["lane"] or "-"
        note = row["note"] or ""
        lines.append(
            f"{row['category']:<22}{lane:>7}{row['calls']:>7}"
            f"{p50:>9}{p90:>9}{maximum:>9}  {note}"
        )
    lines.append(rule)
    lines.append(
        f"{'TOTAL':<22}{'':>7}{report['total_samples']:>7}"
        f"{'':>9}{'':>9}{'':>9}  {report['verdict'].upper()}"
    )
    return "\n".join(lines)


def render_summary(report: dict) -> str:
    """One-paragraph corpus + verdict summary."""
    lines = [
        f"window: {report['window_start']} .. {report['window_end']} "
        f"(since post-fix {report['post_fix_commit']})",
        f"corpus: root={report['sessions_root']} "
        f"files_scanned={report['files_scanned']} "
        f"files_with_samples={report['files_with_samples']} "
        f"records_in_window={report['records_in_window']} "
        f"samples={report['total_samples']}",
    ]
    for check in report["checks"]:
        passed = check["passed"]
        marker = "PASS" if passed is True else "FAIL" if passed is False else "N/A "
        lines.append(f"  [{marker}] {check['id']}: {check['detail']}")
    lines.append(f"verdict: {report['verdict'].upper()}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_slot_wait.py",
        description=(
            "Post-fix slot-wait re-measurement over the pi session-log corpus."
        ),
        epilog=(
            "Read-only: never runs a test, never acquires a semaphore slot, "
            "never writes to the session store. See "
            "docs/dev/test-skill-reference.md for the classification rules."
        ),
    )
    parser.add_argument(
        "--sessions-root",
        default=DEFAULT_SESSIONS_ROOT,
        help=f"Session-log root directory (default: {DEFAULT_SESSIONS_ROOT}).",
    )
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=(
            "ISO-8601 window start; records before it are ignored "
            f"(default: the post-fix commit time {DEFAULT_SINCE})."
        ),
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=MIN_SAMPLES_FOR_PERCENTILE,
        help=(
            "Categories below this many samples are labelled rather than "
            f"percentiled (default: {MIN_SAMPLES_FOR_PERCENTILE})."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable report instead of the table.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit status."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        report = measure(
            args.sessions_root,
            since=args.since,
            min_samples=args.min_samples,
        )
    except ValueError as error:
        parser.error(str(error))

    if not report["success"]:
        print(
            "error: no slot-wait samples found under "
            f"{report['sessions_root']} since {report['since']} "
            f"({report['files_scanned']} file(s) scanned). "
            "Point --sessions-root at the pi session store "
            f"({DEFAULT_SESSIONS_ROOT}) or widen --since.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(render_table(report))
        print()
        print(render_summary(report))
    return 0 if report["verdict"] != "fail" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
