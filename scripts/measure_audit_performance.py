#!/usr/bin/env python3
"""Audit performance measurement harness (SA-0MVI6VET100337TW).

Parses the ``Per-call timing:`` debug-log lines the audit runner emits for
every model call and prints a stable per-context latency table, so a change to
the audit pipeline can be measured instead of guessed.

Why this exists
---------------
The parent epic (``SA-0MVF1NKZQ009P3ZG``) based its diagnosis on an ad hoc
measurement that was never committed and could not be reproduced. This harness
is the reproducible replacement: the same corpus always yields the same table.

Corpus and parsing
------------------
The corpus is every ``*.log`` file under ``--root`` (default
``~/.audit_debug``), scanned recursively. A line is a measurement only when it
*starts* with ``Per-call timing:`` followed by ``issue_id=``, ``context=`` and
``elapsed_seconds=``. The start-of-line anchor matters: audit logs quote audit
prompts and markdown tables that also contain the phrase ``Per-call timing:``,
and those must never be counted (they inflate the corpus by orders of
magnitude if matched).

Contexts are normalised by stripping the ``:<suffix>`` part, so the per-child
and per-AC variants collapse onto their phase:

===============  ==========================================================
``parent``       Phase 1 parent screen
``child``        Phase 1 per-child screen (``child:<id>``)
``phase2_deep``  Phase 2 deep audit of the item
``phase2_child`` Phase 2 per-AC deep audit (``phase2_child:<ac-index>``)
``verdict_reask`` JSON-contract re-ask (wasted work)
``false-positive-screen``  False-positive triage screen
===============  ==========================================================

Metrics
-------
``--metric elapsed`` (default) sums ``elapsed_seconds`` — the true wall-clock
cost of each call. ``--metric per-ac`` sums ``avg_ac_elapsed_seconds`` where
that field is present (Phase 2 contexts only), i.e. the per-AC mean rather than
the whole call. The second mode exists solely to reproduce the epic brief's
ad hoc baseline, which was computed that way and therefore undercounts Phase 2
by roughly 2.7x. See ``docs/dev/audit-performance-baseline.md``.

Shares — ``phase1_share_pct`` and ``wasted_share_pct`` — are always derived
from elapsed wall-clock time, whichever metric the cost columns use, so they
stay comparable. ``median_seconds`` is likewise the wall-clock median. "Wasted"
is time in ``verdict_reask`` calls plus the elapsed time of calls that landed
on a budget (600 s / 900 s / 1,800 s by default, within
``--at-budget-tolerance``), i.e. the fallbacks that did no work.

Read-only guarantee
-------------------
The harness only reads files. It never writes to the corpus, to ``.worklog/``
or to the audit runner, and it never invokes the runner.

Usage::

    python3 scripts/measure_audit_performance.py
    python3 scripts/measure_audit_performance.py --root ~/.audit_debug --json
    python3 scripts/measure_audit_performance.py --metric per-ac

Exit status is ``0`` when at least one timing line was parsed and ``1`` when
the corpus holds no measurements.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

DEFAULT_ROOT = "~/.audit_debug"
DEFAULT_BUDGETS = (600.0, 900.0, 1800.0)
DEFAULT_AT_BUDGET_TOLERANCE_SECONDS = 1.0
METRICS = ("elapsed", "per-ac")

PHASE1_CONTEXTS = ("parent", "child")
REASK_CONTEXT = "verdict_reask"

# Anchored at the start of the line on purpose: audit logs embed the phrase
# "Per-call timing:" inside quoted prompts and markdown tables, and matching
# those produces nonsense aggregates. The audit runner's own parser is
# deliberately not reused here for that reason (it searches anywhere in a
# line).
_TIMING_PATTERN = re.compile(
    r"^\s*Per-call timing:\s+issue_id=(\S+)\s+context=(\S+)"
    r"\s+elapsed_seconds=(\d+(?:\.\d+)?)"
)
_AVG_AC_PATTERN = re.compile(r"avg_ac_elapsed_seconds=(\d+(?:\.\d+)?)")


def _round(value: float, digits: int = 2) -> float:
    """Round for stable, diff-friendly output (avoids ``-0.0``)."""
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Linear-interpolation percentile over an ascending list.

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


def _base_context(context: str) -> str:
    """Strip a ``:<suffix>`` (child id or AC index) from a context name."""
    return context.split(":", 1)[0]


def _iter_log_files(root: Path) -> list[Path]:
    """Return the corpus files in a deterministic order (``*.log`` only)."""
    if root.is_file():
        return [root]
    return sorted(path for path in root.rglob("*.log") if path.is_file())


def _parse_corpus(root: Path) -> tuple[list[dict], int]:
    """Read the corpus and return ``(records, files_scanned)``.

    Each record is ``{"context", "elapsed", "per_ac"}`` where ``per_ac`` is the
    ``avg_ac_elapsed_seconds`` value when the line carries one, else ``None``.
    Files are opened read-only with lossy decoding so a stray binary log cannot
    abort a run.
    """
    files = _iter_log_files(root)
    records: list[dict] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _TIMING_PATTERN.match(line)
            if match is None:
                continue
            avg_match = _AVG_AC_PATTERN.search(line)
            records.append(
                {
                    "context": _base_context(match.group(2)),
                    "elapsed": float(match.group(3)),
                    "per_ac": float(avg_match.group(1)) if avg_match else None,
                }
            )
    return records, len(files)


def _at_budget_buckets(
    elapsed: float,
    budgets: tuple[float, ...],
    tolerance: float,
) -> list[str]:
    """Return the budget labels a call duration is within ``tolerance`` of."""
    return [
        f"{budget:g}"
        for budget in budgets
        if abs(elapsed - budget) <= tolerance
    ]


def measure(
    root: str | Path = DEFAULT_ROOT,
    *,
    metric: str = "elapsed",
    budgets: tuple[float, ...] = DEFAULT_BUDGETS,
    at_budget_tolerance: float = DEFAULT_AT_BUDGET_TOLERANCE_SECONDS,
    contexts: tuple[str, ...] | None = None,
) -> dict:
    """Aggregate the corpus into a per-context latency report.

    Args:
        root: Corpus root directory (or a single log file). ``~`` is expanded.
        metric: ``"elapsed"`` (whole call, default) or ``"per-ac"`` (the
            Phase 2 per-AC mean where recorded, reproducing the epic brief).
        budgets: Call budgets used for the count-at-budget columns.
        at_budget_tolerance: A call counts as "at budget" when its elapsed time
            is within this many seconds of a budget (timeouts are rounded by
            the runner's wall clock, so an exact match is too strict).
        contexts: Optional base-context allow-list; applied before aggregation
            so the totals describe the filtered subset.

    Returns:
        A JSON-serialisable report dict (see ``--json``). ``success`` is
        ``True`` when at least one timing line was parsed.
    """
    if metric not in METRICS:
        raise ValueError(f"unsupported metric: {metric!r}")

    root_path = Path(root).expanduser()
    records, files_scanned = _parse_corpus(root_path)

    if contexts is not None:
        allowed = {_base_context(context) for context in contexts}
        records = [
            record for record in records if record["context"] in allowed
        ]

    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record["context"], []).append(record)

    rows: list[dict] = []
    elapsed_values: list[float] = []
    metric_values: list[float] = []
    timeout_seconds = 0.0
    reask_seconds = 0.0

    for context, context_records in grouped.items():
        values = [
            record["per_ac"]
            if metric == "per-ac" and record["per_ac"] is not None
            else record["elapsed"]
            for record in context_records
        ]
        elapsed = [record["elapsed"] for record in context_records]
        ascending = sorted(values)

        buckets = {f"{budget:g}": 0 for budget in budgets}
        for record in context_records:
            hit_budgets = _at_budget_buckets(
                record["elapsed"], budgets, at_budget_tolerance
            )
            for label in hit_budgets:
                buckets[label] += 1
            # Wall-clock waste is the full call duration, whichever metric the
            # cost columns use. Re-asks are counted once, separately, below.
            if hit_budgets and context != REASK_CONTEXT:
                timeout_seconds += record["elapsed"]

        if context == REASK_CONTEXT:
            reask_seconds += sum(elapsed)

        elapsed_values.extend(elapsed)
        metric_values.extend(values)
        rows.append(
            {
                "context": context,
                "calls": len(context_records),
                "total_seconds": _round(sum(values)),
                "total_hours": _round(sum(values) / 3600.0, 3),
                "p50_seconds": _round(_percentile(ascending, 50)),
                "p90_seconds": _round(_percentile(ascending, 90)),
                "max_seconds": _round(max(values)),
                "at_budget": buckets,
            }
        )

    rows.sort(key=lambda row: (-row["total_seconds"], row["context"]))

    total_seconds = sum(elapsed_values)
    metric_total = sum(metric_values)
    phase1_seconds = sum(
        record["elapsed"]
        for record in records
        if record["context"] in PHASE1_CONTEXTS
    )
    wasted_seconds = reask_seconds + timeout_seconds
    metric_ascending = sorted(metric_values)
    elapsed_ascending = sorted(elapsed_values)

    return {
        "success": bool(records),
        "root": str(root_path),
        "metric": metric,
        "budgets": [float(budget) for budget in budgets],
        "at_budget_tolerance_seconds": float(at_budget_tolerance),
        "files_scanned": files_scanned,
        "timing_lines": len(records),
        "total_calls": len(records),
        "total_seconds": _round(metric_total),
        "total_hours": _round(metric_total / 3600.0, 3),
        "elapsed_total_seconds": _round(total_seconds),
        "elapsed_total_hours": _round(total_seconds / 3600.0, 3),
        "median_seconds": _round(_percentile(elapsed_ascending, 50)),
        "p50_seconds": _round(_percentile(metric_ascending, 50)),
        "p90_seconds": _round(_percentile(metric_ascending, 90)),
        "max_seconds": _round(max(metric_values)) if metric_values else 0.0,
        "phase1_share_pct": _round(
            100.0 * phase1_seconds / total_seconds if total_seconds else 0.0, 1
        ),
        "wasted_share_pct": _round(
            100.0 * wasted_seconds / total_seconds if total_seconds else 0.0, 1
        ),
        "contexts": rows,
    }


def render_table(report: dict) -> str:
    """Render the per-context table (plus a TOTAL row) as plain text."""
    budgets = [f"{budget:g}" for budget in report["budgets"]]
    header = (
        f"{'Context':<24}{'Calls':>7}{'Total(h)':>11}{'p50':>9}{'p90':>9}"
        f"{'max':>9}"
        + "".join(f"{'@' + budget + 's':>8}" for budget in budgets)
    )
    rule = "-" * len(header)
    lines = [header, rule]
    for row in report["contexts"]:
        lines.append(
            f"{row['context']:<24}{row['calls']:>7}"
            f"{row['total_hours']:>11.2f}{row['p50_seconds']:>9.1f}"
            f"{row['p90_seconds']:>9.1f}{row['max_seconds']:>9.1f}"
            + "".join(
                f"{row['at_budget'][budget]:>8}" for budget in budgets
            )
        )

    total_buckets = {
        budget: sum(row["at_budget"][budget] for row in report["contexts"])
        for budget in budgets
    }
    lines.append(rule)
    lines.append(
        f"{'TOTAL':<24}{report['total_calls']:>7}"
        f"{report['total_hours']:>11.2f}{report['p50_seconds']:>9.1f}"
        f"{report['p90_seconds']:>9.1f}{report['max_seconds']:>9.1f}"
        + "".join(f"{total_buckets[budget]:>8}" for budget in budgets)
    )
    return "\n".join(lines)


def render_summary(report: dict) -> str:
    """One-line corpus summary (metric, totals, shares)."""
    return (
        f"corpus: root={report['root']} files={report['files_scanned']} "
        f"lines={report['timing_lines']} metric={report['metric']}\n"
        f"calls={report['total_calls']} total={report['total_hours']:.2f}h "
        f"median={report['median_seconds']:.1f}s "
        f"phase1_share={report['phase1_share_pct']:.1f}% "
        f"wasted_share={report['wasted_share_pct']:.1f}%"
    )


def _parse_budgets(
    parser: argparse.ArgumentParser, raw_values: list[str] | None
) -> tuple[float, ...]:
    """Parse repeatable/comma-separated ``--at-budget`` values."""
    if not raw_values:
        return DEFAULT_BUDGETS
    budgets: list[float] = []
    for raw in raw_values:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                budgets.append(float(part))
            except ValueError:
                parser.error(f"invalid --at-budget value: {part!r}")
    return tuple(sorted(dict.fromkeys(budgets)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_audit_performance.py",
        description=(
            "Measure audit LLM call timing from the audit debug-log corpus."
        ),
        epilog=(
            "Read-only: the harness never writes to the corpus, .worklog/ or "
            "the audit runner. See docs/dev/audit-performance-baseline.md."
        ),
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help=f"Corpus root directory or log file (default: {DEFAULT_ROOT}).",
    )
    parser.add_argument(
        "--context",
        action="append",
        default=None,
        help=(
            "Restrict to a base context (repeatable), e.g. --context parent. "
            "Suffixes such as child:<id> are normalised before matching."
        ),
    )
    parser.add_argument(
        "--at-budget",
        action="append",
        default=None,
        help=(
            "Comma-separated budgets for the count-at-budget columns "
            "(default: 600,900,1800). Repeatable."
        ),
    )
    parser.add_argument(
        "--at-budget-tolerance",
        type=float,
        default=DEFAULT_AT_BUDGET_TOLERANCE_SECONDS,
        help=(
            "Seconds within which a call counts as landing on a budget "
            f"(default: {DEFAULT_AT_BUDGET_TOLERANCE_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--metric",
        choices=METRICS,
        default="elapsed",
        help=(
            "elapsed = whole-call wall clock (default); per-ac = Phase 2 "
            "per-AC mean, reproducing the epic brief's ad hoc baseline."
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
    budgets = _parse_budgets(parser, args.at_budget)
    contexts = tuple(args.context) if args.context else None

    report = measure(
        args.root,
        metric=args.metric,
        budgets=budgets,
        at_budget_tolerance=args.at_budget_tolerance,
        contexts=contexts,
    )

    if not report["success"]:
        print(
            "error: no 'Per-call timing:' lines found under "
            f"{report['root']} ({report['files_scanned']} file(s) scanned). "
            "Point --root at the audit debug-log directory (default "
            "~/.audit_debug) or at a single log file.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(render_table(report))
        print()
        print(render_summary(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
