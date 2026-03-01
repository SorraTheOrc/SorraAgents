"""Enhanced Progress Reports for the APMA.

Generates human-readable progress reports for a work-item hierarchy, including
percent-complete calculations, top-risks identification, and delegation audit
trail.

Usage (CLI)::

    python -m ampa.progress_report --work-item SA-XXXX
    python -m ampa.progress_report --work-item SA-XXXX --format json

Usage (Python)::

    from ampa.progress_report import generate_progress_report
    report = generate_progress_report("SA-XXXX")
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

LOG = logging.getLogger("ampa.progress_report")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMPLETED_STATUSES = ("completed", "closed")
_BLOCKED_STATUSES = ("blocked",)
_DELETED_STATUSES = ("deleted",)
_IN_PROGRESS_STATUSES = ("in-progress", "in_progress")

# Items with these priority levels are considered high-risk by default
_HIGH_RISK_PRIORITIES = ("critical", "high")

# Stale threshold in days — items in_progress for longer than this
# are flagged as potentially stalled.
_STALE_THRESHOLD_DAYS = 7


# ---------------------------------------------------------------------------
# wl interaction
# ---------------------------------------------------------------------------


def _run_wl(
    args: List[str],
    cwd: Optional[str] = None,
    timeout: int = 300,
) -> Dict[str, Any]:
    """Run a ``wl`` CLI command and return parsed JSON output."""
    cmd = ["wl"] + args + ["--json"]
    LOG.debug("Running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"wl command timed out: {' '.join(cmd)}") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse wl output: {proc.stdout[:512]}") from exc


def _fetch_work_item(
    work_item_id: str,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a work-item with its children via ``wl show --children``."""
    return _run_wl(["show", work_item_id, "--children"], cwd=cwd)


def _fetch_comments(
    work_item_id: str,
    cwd: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch comments for a work-item via ``wl comment list``."""
    try:
        result = _run_wl(["comment", "list", work_item_id], cwd=cwd)
        return result.get("comments", [])
    except RuntimeError:
        LOG.exception("Failed to fetch comments for %s", work_item_id)
        return []


# ---------------------------------------------------------------------------
# Percent-complete calculation
# ---------------------------------------------------------------------------


def _compute_percent_complete(
    children: List[Dict[str, Any]],
) -> Tuple[float, Dict[str, int]]:
    """Compute percent-complete from child item statuses.

    Only counts non-deleted children. Returns (percent, status_counts).

    Status weighting:
    - completed/closed = 1.0
    - in-progress/in_progress = 0.5
    - blocked = 0.25
    - open/other = 0.0
    """
    status_counts: Dict[str, int] = {
        "completed": 0,
        "in_progress": 0,
        "blocked": 0,
        "open": 0,
        "deleted": 0,
    }

    active_children: List[Dict[str, Any]] = []
    for child in children:
        status = (child.get("status") or "open").lower()
        # Normalize status
        if status in _COMPLETED_STATUSES:
            status_counts["completed"] += 1
            active_children.append(child)
        elif status in _DELETED_STATUSES:
            status_counts["deleted"] += 1
            # Don't count deleted items in progress
        elif status in _IN_PROGRESS_STATUSES:
            status_counts["in_progress"] += 1
            active_children.append(child)
        elif status in _BLOCKED_STATUSES:
            status_counts["blocked"] += 1
            active_children.append(child)
        else:
            status_counts["open"] += 1
            active_children.append(child)

    total = len(active_children)
    if total == 0:
        return 0.0, status_counts

    weighted_sum = (
        status_counts["completed"] * 1.0
        + status_counts["in_progress"] * 0.5
        + status_counts["blocked"] * 0.25
        + status_counts["open"] * 0.0
    )

    percent = (weighted_sum / total) * 100
    return round(percent, 1), status_counts


# ---------------------------------------------------------------------------
# Risk identification
# ---------------------------------------------------------------------------


def _identify_risks(
    children: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Identify top risks from child work-items.

    Risk categories:
    1. Blocked items (status=blocked)
    2. Items with explicit risk field set
    3. High-priority items that are still open
    4. Stale items (in_progress for longer than threshold)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    risks: List[Dict[str, Any]] = []

    for child in children:
        status = (child.get("status") or "open").lower()

        # Skip completed/deleted items
        if status in _COMPLETED_STATUSES or status in _DELETED_STATUSES:
            continue

        child_id = child.get("id", "unknown")
        child_title = child.get("title", "Untitled")
        child_priority = (child.get("priority") or "medium").lower()
        child_risk = child.get("risk", "")

        risk_reasons: List[str] = []

        # 1. Blocked items
        if status in _BLOCKED_STATUSES:
            risk_reasons.append("Item is blocked")

        # 2. Explicit risk field
        if child_risk:
            risk_reasons.append(f"Risk field: {child_risk}")

        # 3. High-priority + still open
        if (
            child_priority in _HIGH_RISK_PRIORITIES
            and status not in _IN_PROGRESS_STATUSES
        ):
            risk_reasons.append(
                f"High-priority ({child_priority}) item still in '{status}' status"
            )

        # 4. Stale items
        updated_at = child.get("updatedAt", "")
        if updated_at and status in _IN_PROGRESS_STATUSES:
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                days_stale = (now - updated).days
                if days_stale >= _STALE_THRESHOLD_DAYS:
                    risk_reasons.append(
                        f"Stale: in-progress for {days_stale} days "
                        f"(threshold: {_STALE_THRESHOLD_DAYS})"
                    )
            except (ValueError, TypeError):
                pass

        if risk_reasons:
            risks.append(
                {
                    "id": child_id,
                    "title": child_title,
                    "status": status,
                    "priority": child_priority,
                    "risk_level": _compute_risk_level(risk_reasons, child_priority),
                    "reasons": risk_reasons,
                }
            )

    # Sort by risk level (high first)
    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    risks.sort(key=lambda r: risk_order.get(r["risk_level"], 99))

    return risks


def _compute_risk_level(reasons: List[str], priority: str) -> str:
    """Compute an overall risk level from reasons and priority."""
    if any("blocked" in r.lower() for r in reasons):
        return "critical" if priority == "critical" else "high"
    if any("stale" in r.lower() for r in reasons):
        return "high"
    if priority in _HIGH_RISK_PRIORITIES:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Delegation audit trail
# ---------------------------------------------------------------------------


def _extract_delegation_trail(
    comments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Extract delegation-related comments from a work-item's comment list.

    Identifies comments that contain delegation plan markers, assignment
    changes, or APMA action records.
    """
    delegation_keywords = [
        "delegation plan",
        "delegation",
        "delegated to",
        "assigned to",
        "apma",
        "brief intake",
        "proposed tasks",
    ]

    trail: List[Dict[str, Any]] = []
    for comment in comments:
        text = (comment.get("comment") or "").lower()
        if any(kw in text for kw in delegation_keywords):
            trail.append(
                {
                    "id": comment.get("id", ""),
                    "author": comment.get("author", "unknown"),
                    "date": comment.get("createdAt", ""),
                    "summary": _summarize_comment(comment.get("comment", "")),
                }
            )

    return trail


def _summarize_comment(text: str, max_length: int = 200) -> str:
    """Produce a short summary of a comment."""
    # Take first non-empty line as summary
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            if len(stripped) > max_length:
                return stripped[:max_length] + "..."
            return stripped
    return text[:max_length] if text else ""


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _format_markdown_report(report_data: Dict[str, Any]) -> str:
    """Format the report data as human-readable markdown."""
    wi = report_data["work_item"]
    pct = report_data["percent_complete"]
    counts = report_data["status_counts"]
    risks = report_data["top_risks"]
    trail = report_data["delegation_trail"]

    lines = [
        f"# Progress Report: {wi['title']}",
        "",
        f"**ID:** {wi['id']}",
        f"**Status:** {wi['status']} | **Priority:** {wi['priority']}",
        "",
        "---",
        "",
        "## Progress",
        "",
        f"**Percent Complete: {pct}%**",
        "",
        f"| Status | Count |",
        f"|--------|-------|",
        f"| Completed | {counts.get('completed', 0)} |",
        f"| In Progress | {counts.get('in_progress', 0)} |",
        f"| Blocked | {counts.get('blocked', 0)} |",
        f"| Open | {counts.get('open', 0)} |",
        f"| Deleted | {counts.get('deleted', 0)} |",
        "",
    ]

    # Progress bar
    bar_width = 20
    filled = int(pct / 100 * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)
    lines.append(f"Progress: [{bar}] {pct}%")
    lines.append("")

    # Top risks
    lines.append("## Top Risks")
    lines.append("")
    if risks:
        for risk in risks:
            level_emoji = {
                "critical": "[CRITICAL]",
                "high": "[HIGH]",
                "medium": "[MEDIUM]",
                "low": "[LOW]",
            }.get(risk["risk_level"], "[?]")
            lines.append(f"### {level_emoji} {risk['title']}")
            lines.append(f"- **ID:** {risk['id']}")
            lines.append(
                f"- **Status:** {risk['status']} | **Priority:** {risk['priority']}"
            )
            for reason in risk["reasons"]:
                lines.append(f"- {reason}")
            lines.append("")
    else:
        lines.append("No risks identified.")
        lines.append("")

    # Delegation audit trail
    lines.append("## Delegation Audit Trail")
    lines.append("")
    if trail:
        for entry in trail:
            date_str = entry["date"][:10] if entry["date"] else "unknown"
            lines.append(f"- **{date_str}** ({entry['author']}): {entry['summary']}")
        lines.append("")
    else:
        lines.append("No delegation actions recorded.")
        lines.append("")

    lines.append("---")
    lines.append("Report generated by APMA Progress Reporter.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_progress_report(
    work_item_id: str,
    *,
    cwd: Optional[str] = None,
    now: Optional[datetime] = None,
    _wl_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    _comment_fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Generate a progress report for a work-item and its children.

    Parameters
    ----------
    work_item_id:
        The ``wl`` work-item ID to report on.
    cwd:
        Working directory for wl commands.
    now:
        Override current time (for testing stale detection).
    _wl_fetcher:
        Injectable fetcher for testing (replaces ``_fetch_work_item``).
    _comment_fetcher:
        Injectable comment fetcher for testing.

    Returns
    -------
    dict
        A report dict with ``work_item``, ``percent_complete``,
        ``status_counts``, ``top_risks``, ``delegation_trail``,
        and ``markdown`` keys.
    """
    if not work_item_id or not work_item_id.strip():
        raise ValueError("Work item ID must not be empty")

    # Fetch data
    fetcher = _wl_fetcher or _fetch_work_item
    data = fetcher(work_item_id, cwd=cwd)

    work_item = data.get("workItem", {})
    children = data.get("children", [])
    inline_comments = data.get("comments", [])

    if not work_item.get("id"):
        raise RuntimeError(
            f"Failed to fetch work-item {work_item_id}: response missing 'workItem.id'"
        )

    # Fetch comments (use inline ones if available, otherwise fetch separately)
    if _comment_fetcher:
        comments = _comment_fetcher(work_item_id, cwd=cwd)
    elif inline_comments:
        comments = inline_comments
    else:
        comments = _fetch_comments(work_item_id, cwd=cwd)

    # Compute metrics
    percent, status_counts = _compute_percent_complete(children)
    risks = _identify_risks(children, now=now)
    trail = _extract_delegation_trail(comments)

    report_data = {
        "work_item": {
            "id": work_item.get("id"),
            "title": work_item.get("title", "Untitled"),
            "status": work_item.get("status", "unknown"),
            "priority": work_item.get("priority", "medium"),
        },
        "percent_complete": percent,
        "status_counts": status_counts,
        "top_risks": risks,
        "delegation_trail": trail,
        "children_count": len(children),
    }

    report_data["markdown"] = _format_markdown_report(report_data)

    return report_data


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entry point for progress_report."""
    parser = argparse.ArgumentParser(
        description="APMA Progress Report — generate a progress report for a work-item",
    )
    parser.add_argument(
        "--work-item",
        required=True,
        help="Work-item ID to report on",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for wl commands",
    )
    args = parser.parse_args(argv)

    report = generate_progress_report(
        args.work_item,
        cwd=args.cwd,
    )

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(report["markdown"])


if __name__ == "__main__":
    main()
