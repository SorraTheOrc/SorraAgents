"""Delegation Planner for the APMA.

Accepts a work-item ID, fetches its details and existing children from ``wl``,
analyses the work structure, and produces a delegation plan with proposed tasks,
suggested assignees, and rationale.

Supports ``dry_run`` (returns plan without mutations) and ``propose`` (posts
delegation comments on the work-item via ``wl``).

Usage (CLI)::

    python -m ampa.delegation_planner --work-item SA-XXXX --mode dry_run
    python -m ampa.delegation_planner --work-item SA-XXXX --mode propose

Usage (Python)::

    from ampa.delegation_planner import build_delegation_plan
    plan = build_delegation_plan("SA-XXXX", mode="dry_run")
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

LOG = logging.getLogger("ampa.delegation_planner")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_MODES = ("dry_run", "propose")

_CONFIG_FILE = Path(__file__).parent / "delegation_config.yaml"


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def _load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load delegation configuration from YAML."""
    import yaml  # type: ignore

    path = Path(config_path) if config_path else _CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(f"Delegation config not found: {path}")
    with open(path, "r") as fh:
        return yaml.safe_load(fh) or {}


def _get_agent_groups(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build a lookup dict of agent-group name -> agent-group config."""
    groups = {}
    for group in config.get("agent_groups", []):
        groups[group["name"]] = group
    return groups


def _get_category_rules(config: Dict[str, Any]) -> Dict[str, str]:
    """Return category -> agent-group mapping."""
    return config.get("category_rules", {})


def _get_category_keywords(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return category -> keyword-pattern list."""
    return config.get("category_keywords", {})


# ---------------------------------------------------------------------------
# wl interaction (same pattern as brief_intake.py)
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


def _add_comment(
    work_item_id: str,
    comment: str,
    author: str = "ampa-delegation-planner",
    cwd: Optional[str] = None,
) -> bool:
    """Add a comment to a work item. Returns True on success."""
    try:
        _run_wl(
            ["comment", "add", work_item_id, "--comment", comment, "--author", author],
            cwd=cwd,
        )
        return True
    except RuntimeError:
        LOG.exception("Failed to add comment to %s", work_item_id)
        return False


# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------


def _infer_category(
    title: str,
    description: str,
    issue_type: str,
    keywords_map: Dict[str, List[str]],
    category_rules: Dict[str, str],
) -> str:
    """Infer a delegation category from work-item content.

    Strategy:
    1. Check issue_type against category_rules.
    2. Match title and description against keyword patterns.
    3. Fall back to 'implementation' as default.
    """
    # 1. Check issue_type directly
    issue_lower = (issue_type or "").lower()
    if issue_lower in category_rules:
        return issue_lower

    # 2. Keyword matching on title + description
    combined_text = f"{title}\n{description}".lower()
    scores: Dict[str, int] = {}
    for category, patterns in keywords_map.items():
        score = 0
        for pattern in patterns:
            if re.search(pattern, combined_text, re.IGNORECASE):
                score += 1
        if score > 0:
            scores[category] = score

    if scores:
        return max(scores, key=scores.get)  # type: ignore[arg-type]

    # 3. Default
    return "implementation"


def _suggest_assignee(
    category: str,
    category_rules: Dict[str, str],
    agent_groups: Dict[str, Dict[str, Any]],
) -> Tuple[str, str]:
    """Look up the suggested assignee for a category.

    Returns (agent_group_name, rationale).
    """
    group_name = category_rules.get(category, "dev-agent")
    group = agent_groups.get(group_name, {})
    description = group.get("description", "General-purpose agent.")
    rationale = f"Category '{category}' maps to '{group_name}' — {description.strip()}"
    return group_name, rationale


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def _analyze_coverage(
    work_item: Dict[str, Any],
    children: List[Dict[str, Any]],
    keywords_map: Dict[str, List[str]],
    category_rules: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Analyze existing children and categorize them.

    Returns a dict mapping category -> list of child work-items in that
    category.
    """
    categorized: Dict[str, List[Dict[str, Any]]] = {}
    for child in children:
        cat = _infer_category(
            child.get("title", ""),
            child.get("description", ""),
            child.get("issueType", ""),
            keywords_map,
            category_rules,
        )
        categorized.setdefault(cat, []).append(child)
    return categorized


def _build_delegation_plan(
    work_item: Dict[str, Any],
    children: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a structured delegation plan dict.

    Returns a JSON-serializable dict with ``work_item``, ``existing_children``,
    ``proposed_delegations``, and ``summary`` keys.
    """
    agent_groups = _get_agent_groups(config)
    category_rules = _get_category_rules(config)
    keywords_map = _get_category_keywords(config)

    wi_id = work_item.get("id", "unknown")
    wi_title = work_item.get("title", "Untitled")
    wi_desc = work_item.get("description", "")
    wi_status = work_item.get("status", "unknown")
    wi_priority = work_item.get("priority", "medium")

    # Categorize existing children
    coverage = _analyze_coverage(work_item, children, keywords_map, category_rules)

    # Identify open (non-completed) children that need delegation
    open_children = [
        c for c in children if c.get("status") not in ("completed", "closed", "deleted")
    ]

    # Build delegation entries for each open child
    proposed_delegations: List[Dict[str, Any]] = []
    for child in open_children:
        child_id = child.get("id", "unknown")
        child_title = child.get("title", "Untitled")
        child_desc = child.get("description", "")
        child_type = child.get("issueType", "task")
        child_priority = child.get("priority", "medium")
        child_assignee = child.get("assignee", "")
        child_status = child.get("status", "open")

        category = _infer_category(
            child_title,
            child_desc,
            child_type,
            keywords_map,
            category_rules,
        )
        suggested_agent, rationale = _suggest_assignee(
            category,
            category_rules,
            agent_groups,
        )

        delegation_entry = {
            "child_id": child_id,
            "title": child_title,
            "category": category,
            "issue_type": child_type,
            "priority": child_priority,
            "current_assignee": child_assignee,
            "current_status": child_status,
            "suggested_assignee": suggested_agent,
            "rationale": rationale,
            "needs_assignment": not child_assignee,
            "acceptance_criteria": _extract_ac_section(child_desc),
        }
        proposed_delegations.append(delegation_entry)

    # Build summary
    total_children = len(children)
    open_count = len(open_children)
    completed_count = len(
        [c for c in children if c.get("status") in ("completed", "closed")]
    )
    unassigned_count = len([d for d in proposed_delegations if d["needs_assignment"]])

    summary = {
        "work_item_id": wi_id,
        "work_item_title": wi_title,
        "total_children": total_children,
        "open_children": open_count,
        "completed_children": completed_count,
        "unassigned_children": unassigned_count,
        "categories_covered": sorted(set(d["category"] for d in proposed_delegations)),
        "agent_groups_suggested": sorted(
            set(d["suggested_assignee"] for d in proposed_delegations)
        ),
    }

    return {
        "work_item": {
            "id": wi_id,
            "title": wi_title,
            "status": wi_status,
            "priority": wi_priority,
        },
        "existing_children_count": total_children,
        "proposed_delegations": proposed_delegations,
        "summary": summary,
    }


def _extract_ac_section(description: str) -> str:
    """Extract acceptance criteria from a work-item description."""
    pattern = re.compile(
        r"^#{2,3}\s*(acceptance\s*criteria|ac)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(description or "")
    if not match:
        return ""
    rest = description[match.end() :]
    end_match = re.search(r"^#{1,3}\s+", rest, re.MULTILINE)
    if end_match:
        return rest[: end_match.start()].strip()
    return rest.strip()


# ---------------------------------------------------------------------------
# Delegation comment formatting
# ---------------------------------------------------------------------------


def _format_delegation_comment(plan: Dict[str, Any]) -> str:
    """Format the delegation plan as a markdown comment for wl."""
    wi = plan["work_item"]
    delegations = plan["proposed_delegations"]
    summary = plan["summary"]

    lines = [
        "# APMA Delegation Plan",
        "",
        f"**Work Item:** {wi['id']} — {wi['title']}",
        f"**Status:** {wi['status']} | **Priority:** {wi['priority']}",
        "",
        f"## Summary",
        f"- Total children: {summary['total_children']}",
        f"- Open: {summary['open_children']} | Completed: {summary['completed_children']}",
        f"- Unassigned: {summary['unassigned_children']}",
        f"- Categories: {', '.join(summary['categories_covered']) or 'none'}",
        f"- Agent groups: {', '.join(summary['agent_groups_suggested']) or 'none'}",
        "",
        "## Proposed Delegations",
        "",
    ]

    for d in delegations:
        assignment_note = (
            f"currently: {d['current_assignee']}"
            if d["current_assignee"]
            else "unassigned"
        )
        lines.append(f"### {d['title']}  ")
        lines.append(f"- **ID:** {d['child_id']}")
        lines.append(f"- **Category:** {d['category']}")
        lines.append(f"- **Priority:** {d['priority']}")
        lines.append(f"- **Current:** {assignment_note} ({d['current_status']})")
        lines.append(f"- **Suggested assignee:** {d['suggested_assignee']}")
        lines.append(f"- **Rationale:** {d['rationale']}")
        if d["acceptance_criteria"]:
            lines.append(f"- **AC:** {d['acceptance_criteria'][:200]}")
        lines.append("")

    lines.append("---")
    lines.append(
        "This delegation plan was generated by APMA. "
        "Review and approve before agents begin work."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_delegation_plan(
    work_item_id: str,
    *,
    mode: str = "dry_run",
    config_path: Optional[str] = None,
    cwd: Optional[str] = None,
    _wl_fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    _wl_commenter: Optional[Callable[..., bool]] = None,
) -> Dict[str, Any]:
    """Produce a delegation plan for a work-item.

    Parameters
    ----------
    work_item_id:
        The ``wl`` work-item ID to plan delegation for.
    mode:
        ``"dry_run"`` returns the plan without mutations.
        ``"propose"`` posts delegation comments on the work-item.
    config_path:
        Optional path to a custom delegation_config.yaml.
    cwd:
        Working directory for wl commands.
    _wl_fetcher:
        Injectable fetcher for testing (replaces ``_fetch_work_item``).
    _wl_commenter:
        Injectable commenter for testing (replaces ``_add_comment``).

    Returns
    -------
    dict
        A JSON-serializable delegation plan with ``work_item``,
        ``proposed_delegations``, ``summary``, and ``mode`` keys.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"Invalid mode {mode!r}; expected one of {_VALID_MODES}")

    if not work_item_id or not work_item_id.strip():
        raise ValueError("Work item ID must not be empty")

    config = _load_config(config_path)

    # Fetch work-item data
    fetcher = _wl_fetcher or _fetch_work_item
    data = fetcher(work_item_id, cwd=cwd)

    work_item = data.get("workItem", {})
    children = data.get("children", [])

    if not work_item.get("id"):
        raise RuntimeError(
            f"Failed to fetch work-item {work_item_id}: response missing 'workItem.id'"
        )

    plan = _build_delegation_plan(work_item, children, config)
    plan["mode"] = mode

    if mode == "dry_run":
        return plan

    # --- propose mode: post delegation comment ---
    comment_text = _format_delegation_comment(plan)
    commenter = _wl_commenter or _add_comment
    success = commenter(work_item_id, comment_text, cwd=cwd)
    plan["comment_posted"] = success

    return plan


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entry point for delegation_planner."""
    parser = argparse.ArgumentParser(
        description="APMA Delegation Planner — produce a delegation plan for a work-item",
    )
    parser.add_argument(
        "--work-item",
        required=True,
        help="Work-item ID to plan delegation for",
    )
    parser.add_argument(
        "--mode",
        choices=_VALID_MODES,
        default="dry_run",
        help="dry_run (default) returns plan; propose posts comments to wl",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to custom delegation_config.yaml",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for wl commands",
    )
    args = parser.parse_args(argv)

    result = build_delegation_plan(
        args.work_item,
        mode=args.mode,
        config_path=args.config,
        cwd=args.cwd,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
