#!/usr/bin/env python3
"""
Machine-hygiene before/after table rendering and approval gate.

Renders the diagnostic before table, the proposed-actions table, and the
after table (with residual/legitimate load), and provides the fail-closed
approval gate that halts before any destructive action.

The approval gate is deliberately interactive-with-injection: the actual
input/output functions are parameters, so the gate is unit-testable and can
never execute an unapproved action.
"""

from collections.abc import Callable, Sequence
from typing import Any

# ---------------------------------------------------------------------------
# Symbols and priorities
# ---------------------------------------------------------------------------

STATUS_SYMBOLS = {"low": "🟢", "warning": "🟡", "critical": "🔴"}
STATUS_ASCII = {"low": "OK", "warning": "WARN", "critical": "CRIT"}
PRIORITY = {"critical": "P0", "warning": "P1", "low": "P2"}

# Canonical order for the metric tables.
METRIC_ORDER = [
    "load_average",
    "cpu_pressure",
    "memory_pressure",
    "io_pressure",
    "memory_usage",
    "swap_usage",
    "runnable_processes",
    "process_count",
    "thread_count",
    "rss_top10",
    "zombie_processes",
    "stale_lock_files",
]

# Process names that represent legitimate, active workload — remediation
# must not suggest killing these without operator awareness.
LEGITIMATE_LOAD_MARKERS = {
    "pytest", "python", "python3", "node", "vitest", "jest", "npm",
    "pnpm", "yarn", "chrome", "chromium", "chrome-headless", "puppeteer",
    "playwright", "pi", "herdr", "tmux", "wl",
}


# ---------------------------------------------------------------------------
# Low-level rendering
# ---------------------------------------------------------------------------

def _symbol(status: str, ascii_only: bool = False) -> str:
    table = STATUS_ASCII if ascii_only else STATUS_SYMBOLS
    return table.get(status, "?")


def _format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return f"{len(value)} items"
    return str(value)


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a simple monospaced table with headers and a separator."""
    all_rows = [list(headers)] + [list(r) for r in rows]
    widths = [max(len(str(r[i])) for r in all_rows) for i in range(len(headers))]

    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(
            str(cell).ljust(widths[i]) for i, cell in enumerate(cells)
        ) + " |"

    out = [line(headers), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out.extend(line(r) for r in rows)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Before table
# ---------------------------------------------------------------------------

def render_before_table(metrics: dict[str, Any], ascii_only: bool = False) -> str:
    """Render the before table: metric, value, status symbol, priority.

    AC1 + AC3: status symbols (Unicode with ASCII fallback) and priority
    annotations are shown for every metric.
    """
    rows: list[list[str]] = []
    for name in METRIC_ORDER:
        info = metrics.get(name)
        if info is None:
            continue
        status = info.get("status", "low")
        rows.append([
            name,
            _format_value(info.get("value")),
            _symbol(status, ascii_only),
            PRIORITY.get(status, "P2"),
            status,
        ])

    lines = ["## Before", "", _render_table(
        ["Metric", "Value", "Status", "Priority", "Level"], rows
    )]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Legitimate / residual load
# ---------------------------------------------------------------------------

def detect_legitimate_load(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Identify legitimate active workload (AC4).

    Uses the RSS breakdown plus the runnable-process count to report workload
    that should not be auto-remediated (active test suites, agent sessions).
    """
    findings: list[dict[str, Any]] = []
    breakdown = metrics.get("rss_breakdown", {}).get("value") or []
    for item in breakdown:
        name = str(item.get("name") or "").lower()
        if name in LEGITIMATE_LOAD_MARKERS:
            findings.append({
                "name": item.get("name"),
                "rss_bytes": item.get("total_rss_bytes", 0),
                "reason": "active tooling/agent workload",
            })

    runnable = metrics.get("runnable_processes") or {}
    if (runnable.get("value") or 0) > 0 and findings:
        findings.append({
            "name": "runnable-processes",
            "value": runnable.get("value"),
            "reason": "active schedulable work — pace rather than kill",
        })
    return findings


def render_residual_load(metrics: dict[str, Any]) -> str:
    """Render the residual/legitimate-load section of the after table (AC4)."""
    findings = detect_legitimate_load(metrics)
    lines = ["### Residual / legitimate load", ""]
    if not findings:
        lines.append("No legitimate residual load detected.")
        return "\n".join(lines)
    for finding in findings:
        amount = finding.get("rss_bytes")
        amount_text = f"{amount / (1024 ** 2):.0f} MB" if amount else str(
            finding.get("value", "")
        )
        lines.append(
            f"- {finding['name']} ({amount_text}): {finding['reason']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# After table
# ---------------------------------------------------------------------------

def render_after_table(
    before: dict[str, Any],
    after: dict[str, Any],
    ascii_only: bool = False,
) -> str:
    """Render the after table side-by-side with the before state (AC2/AC4)."""
    rows: list[list[str]] = []
    for name in METRIC_ORDER:
        b = before.get(name)
        a = after.get(name)
        if b is None and a is None:
            continue
        b_status = (b or {}).get("status", "low")
        a_status = (a or {}).get("status", "low")
        delta = ""
        bv = (b or {}).get("value")
        av = (a or {}).get("value")
        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
            diff = av - bv
            delta = f"{'+' if diff >= 0 else ''}{diff:g}"
        rows.append([
            name,
            _format_value(bv),
            _symbol(b_status, ascii_only),
            _format_value(av),
            _symbol(a_status, ascii_only),
            delta or "—",
        ])

    lines = [
        "## After",
        "",
        _render_table(
            ["Metric", "Before", "B", "After", "A", "Δ"], rows
        ),
        "",
        render_residual_load(after),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Proposed actions
# ---------------------------------------------------------------------------

def render_proposed_actions(
    actions: Sequence[dict[str, Any]], ascii_only: bool = False
) -> str:
    """Render the numbered proposed-actions table."""
    rows: list[list[str]] = []
    for idx, action in enumerate(actions, start=1):
        target = action.get("target", {})
        if "pids" in target:
            target_text = f"{target.get('name', '')} pids={target['pids']}"
        else:
            target_text = str(target.get("scope", target))
        rows.append([
            str(idx),
            action.get("action", ""),
            target_text,
            action.get("expected_impact", ""),
            action.get("impact", ""),
            action.get("risk", ""),
        ])
    if not rows:
        return "## Proposed actions\n\nNo remediation actions proposed."
    return "## Proposed actions\n\n" + _render_table(
        ["#", "Action", "Target", "Expected impact", "Impact", "Risk"], rows
    )


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------

def render_approval_gate(
    metrics: dict[str, Any],
    pressures: Sequence[dict[str, Any]],
    actions: Sequence[dict[str, Any]],
    ascii_only: bool = False,
) -> str:
    """Render the full findings presented at the approval gate (AC5)."""
    parts = [
        "# Machine-hygiene findings",
        "",
        render_before_table(metrics, ascii_only=ascii_only),
        "",
        "## Pressure analysis",
        "",
    ]
    if pressures:
        for pressure in pressures:
            parts.append(
                f"- **{pressure.get('metric')}** ({pressure.get('status')}): "
                f"{pressure.get('summary', '')}"
            )
    else:
        parts.append("No actionable pressure detected.")

    parts.extend([
        "",
        render_proposed_actions(actions, ascii_only=ascii_only),
        "",
        "> **Approval gate:** no destructive action is taken until the "
        "operator explicitly approves it.",
    ])
    return "\n".join(parts)


def parse_approval(raw: str, count: int) -> list[int]:
    """Parse an approval response into zero-based action indices.

    Fail-closed: ``all`` approves every action; ``none``/``''``/anything
    unrecognised approves nothing. Indices are 1-based in the UI.
    """
    text = (raw or "").strip().lower()
    if text == "all":
        return list(range(count))
    if text in ("", "none", "no", "n"):
        return []
    approved: list[int] = []
    for token in text.replace(" ", "").split(","):
        if not token:
            continue
        try:
            idx = int(token)
        except ValueError:
            return []  # fail closed on malformed input
        if 1 <= idx <= count:
            approved.append(idx - 1)
    return sorted(set(approved))


def prompt_for_approval(
    actions: Sequence[dict[str, Any]],
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Prompt the operator and return the approved subset of actions.

    Returns ``[]`` when there is nothing to approve, when the operator
    declines, or on EOF/malformed input — the gate never approves by default.
    """
    if not actions:
        return []
    output_fn(render_proposed_actions(actions))
    output_fn(
        "Approve which actions? Enter 'all', 'none', or a comma-separated "
        "list of numbers:"
    )
    try:
        raw = input_fn("> ")
    except (EOFError, KeyboardInterrupt):
        return []
    indices = parse_approval(raw, len(actions))
    return [actions[i] for i in indices]
