"""Shared tree traversal and AC-coverage helpers for plan/intake skills.

Provides functions for:
  - Fetching the full descendant tree of a work item
  - Ordering siblings by ``wl dep`` edges (topological) then listed order
  - Extracting acceptance criteria from work item descriptions
  - Computing whether child ACs collectively cover parent ACs
  - Auto-closing unambiguous coverage gaps
  - Detecting and reporting unresolvable conflicts

The module is designed to be called from both the plan and intake skills,
allowing them to verify that a parent's acceptance criteria are collectively
covered by its children.

Related work item: SA-0MSLRVQIF0040GAM
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import defaultdict, deque
from typing import Any

# Ensure the shared/ package is importable regardless of cwd.
# When invoked as ``python3 tree_coverage.py ...``, __file__ points to
# this file inside ``skills/shared/`` — prepend the parent so
# ``from shared.status_lifecycle`` resolves correctly.
_skill_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_skill_dir)  # .../skills/
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from shared.status_lifecycle import resolve_worklog_flags

logger = logging.getLogger("tree_coverage")

# ---------------------------------------------------------------------------
# Subprocess execution helper (supports custom runners for test injection)
# ---------------------------------------------------------------------------


def _execute_subprocess(
    cmd: list[str],
    input_data: str | None = None,
    runner: Any | None = None,
) -> Any:
    """Execute a subprocess, supporting custom runners for test injection."""
    import subprocess

    if runner is not None:
        if input_data is not None:
            return runner(list(cmd) + [input_data])
        return runner(list(cmd))
    return subprocess.run(cmd, input=input_data, capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# Tree fetch
# ---------------------------------------------------------------------------


def fetch_descendant_tree(
    work_item_id: str,
    runner: Any | None = None,
    _seen: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch the full descendant tree of a work item recursively.

    Returns a dict keyed by work item ID, each value containing:
      - ``id``: the work item ID
      - ``title``: short title
      - ``children``: list of descendant IDs (empty if leaf)

    Cycles are detected via ``_seen``; when a cycle is found the branch is
    pruned and a warning is logged.

    Arguments:
        work_item_id: The root work item ID.
        runner: Optional test runner (see ``_execute_subprocess``).
        _seen: Internal cycle-detection set (do not pass manually).
    """
    _seen = _seen or set()
    if work_item_id in _seen:
        logger.warning("Cycle detected at %s; pruning", work_item_id)
        return {work_item_id: {"id": work_item_id, "title": "", "children": []}}
    _seen.add(work_item_id)

    children_data = _wl_show_children(work_item_id, runner=runner)
    children_ids = [c["id"] for c in children_data] if children_data else []

    tree: dict[str, Any] = {}
    for child_id in children_ids:
        tree.update(fetch_descendant_tree(child_id, runner=runner, _seen=_seen))

    tree[work_item_id] = {
        "id": work_item_id,
        "title": _get_title_from_children(work_item_id, children_data),
        "children": children_ids,
    }
    return tree


def _wl_show_children(
    work_item_id: str,
    runner: Any | None = None,
) -> list[dict]:
    """Call ``wl show <id> --children --json`` and return the children list."""
    cmd = ["wl", "show", work_item_id, "--children", "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning(
            "wl show children failed target=%s stderr=%s",
            work_item_id, proc.stderr,
        )
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "wl show children invalid JSON target=%s", work_item_id
        )
        return []
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning(
            "wl show children returned error target=%s", work_item_id
        )
        return []
    return data.get("workItem", {}).get("children", []) if isinstance(data, dict) else []


def _get_title_from_children(
    work_item_id: str,
    children_data: list[dict],
) -> str:
    """Extract the title from the first child, or fall back to the parent."""
    if children_data:
        return children_data[0].get("title", "")
    return ""


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------


def order_by_dependencies(
    children: list[dict],
    work_item_id: str,
    runner: Any | None = None,
) -> list[dict]:
    """Order child items by ``wl dep`` edges (topological), ties by listed order.

    Children that have dependencies are placed after their prerequisites.
    Siblings with no dependency relationship preserve their listed order.

    Arguments:
        children: List of child dicts from ``wl show --children --json``.
        work_item_id: The parent work item ID (for fetching dep edges).
        runner: Optional test runner.

    Returns:
        A new list of children in dependency-respecting order.
    """
    if not children:
        return []

    # Build dependency map: child_id -> set of prerequisite IDs
    dep_edges = _get_dep_edges(work_item_id, runner=runner)
    deps: dict[str, set[str]] = {}
    for edge in dep_edges:
        target = edge.get("targetId") or edge.get("target")
        prereq = edge.get("prerequisiteId") or edge.get("prerequisite")
        if target and prereq:
            deps.setdefault(target, set()).add(prereq)

    # Build reverse map: prerequisite_id -> set of child_ids that depend on it
    dependents: dict[str, set[str]] = defaultdict(set)
    for cid, prereqs in deps.items():
        for prereq in prereqs:
            dependents[prereq].add(cid)

    # Topological sort using Kahn's algorithm, preserving insertion order
    child_ids = [c["id"] for c in children]
    child_set = set(child_ids)
    in_degree: dict[str, int] = {cid: 0 for cid in child_ids}

    for cid, prereqs in deps.items():
        if cid in child_set:
            in_degree[cid] = len(prereqs & child_set)

    queue = deque()
    for cid in child_ids:
        if in_degree[cid] == 0:
            queue.append(cid)

    ordered_ids: list[str] = []
    while queue:
        cid = queue.popleft()
        ordered_ids.append(cid)
        # Find all children that depend on this cid
        for dependent in dependents.get(cid, set()):
            if dependent in child_set:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

    # Append any remaining (unresolvable due to cycles) in original order
    remaining = [cid for cid in child_ids if cid not in set(ordered_ids)]
    ordered_ids.extend(remaining)

    # Build ordered list of child dicts
    child_map = {c["id"]: c for c in children}
    return [child_map[cid] for cid in ordered_ids if cid in child_map]


def _get_dep_edges(
    work_item_id: str,
    runner: Any | None = None,
) -> list[dict]:
    """Call ``wl dep list <id> --json`` and return dependency edges."""
    cmd = ["wl", "dep", "list", work_item_id, "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning(
            "wl dep list failed target=%s stderr=%s",
            work_item_id, proc.stderr,
        )
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "wl dep list invalid JSON target=%s", work_item_id
        )
        return []
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning("wl dep list returned error target=%s", work_item_id)
        return []
    if isinstance(data, list):
        return data
    return data.get("dependencies", []) if isinstance(data, dict) else []


# ---------------------------------------------------------------------------
# AC extraction
# ---------------------------------------------------------------------------


def extract_acceptance_criteria(description: str) -> list[str]:
    """Extract acceptance criteria bullets from a work item description.

    Looks for a section header matching ``## Acceptance Criteria`` (case-insensitive)
    and collects lines starting with ``- `` (markdown bullets) until the next
    section header (``##``) or end of content.

    Strips leading ``- `` and surrounding whitespace from each bullet.

    Arguments:
        description: The work item description text.

    Returns:
        A list of AC strings (empty list if no section found).
    """
    lines = description.split("\n")
    acs: list[str] = []
    in_ac_section = False

    for line in lines:
        stripped = line.strip()
        # Check for the AC section header
        if re.match(r"^#+\s+Acceptance\s+Criteria", stripped, re.IGNORECASE):
            in_ac_section = True
            continue
        # Exit the section if we hit another heading
        if in_ac_section and re.match(r"^##\s+", stripped):
            break
        # Collect AC bullets
        if in_ac_section and stripped.startswith("- "):
            ac_text = stripped[2:].strip()
            if ac_text:
                acs.append(ac_text)
        # Also handle + bullets
        if in_ac_section and stripped.startswith("+ "):
            ac_text = stripped[2:].strip()
            if ac_text:
                acs.append(ac_text)

    return acs


def extract_acs_from_item(
    work_item_id: str,
    runner: Any | None = None,
) -> list[str]:
    """Extract ACs from a work item by fetching it via ``wl show``."""
    cmd = ["wl", "show", work_item_id, "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning(
            "wl show failed for AC extraction target=%s", work_item_id
        )
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "wl show invalid JSON for AC extraction target=%s", work_item_id
        )
        return []
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning(
            "wl show returned error for AC extraction target=%s", work_item_id
        )
        return []
    description = data.get("workItem", {}).get("description", "")
    if not isinstance(description, str):
        return []
    return extract_acceptance_criteria(description)


# ---------------------------------------------------------------------------
# Coverage computation
# ---------------------------------------------------------------------------


def compute_coverage(
    parent_acs: list[str],
    child_acs_list: list[list[str]],
    similarity_threshold: float = 0.4,
) -> dict[str, Any]:
    """Compute whether child ACs collectively cover parent ACs.

    For each parent AC, checks if any child AC covers it using keyword-based
    similarity. An AC is considered "covered" if the highest similarity score
    exceeds ``similarity_threshold``.

    Arguments:
        parent_acs: List of parent acceptance criterion strings.
        child_acs_list: List of child AC lists (one per child).
        similarity_threshold: Minimum Jaccard similarity to consider an AC
            covered (0.0–1.0). Defaults to 0.4.

    Returns:
        A dict with:
          - ``coverage_map``: dict mapping each parent AC index to the
            list of child AC indices that cover it (may be empty).
          - ``uncovered``: list of parent AC strings that have no covering
            child AC.
          - ``fully_covered``: bool — True if every parent AC is covered.
          - ``coverage_pct``: float — percentage of parent ACs covered.
    """
    if not parent_acs:
        return {
            "coverage_map": {},
            "uncovered": [],
            "fully_covered": True,
            "coverage_pct": 100.0,
        }

    coverage_map: dict[int, list[int]] = {}
    uncovered: list[str] = []

    for p_idx, parent_ac in enumerate(parent_acs):
        covered_by: list[int] = []
        best_score = 0.0

        for c_idx, child_ac in enumerate(child_acs_list):
            for c_ac in child_ac:
                score = jaccard_similarity(parent_ac, c_ac)
                best_score = max(best_score, score)
                if score >= similarity_threshold:
                    covered_by.append(c_idx)

        coverage_map[p_idx] = covered_by
        if not covered_by:
            uncovered.append(parent_ac)

    coverage_pct = ((len(parent_acs) - len(uncovered)) / len(parent_acs)) * 100

    return {
        "coverage_map": coverage_map,
        "uncovered": uncovered,
        "fully_covered": len(uncovered) == 0,
        "coverage_pct": coverage_pct,
    }


def jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two strings using word tokens.

    Tokens are lowercased and stripped of punctuation.

    Arguments:
        a: First string.
        b: Second string.

    Returns:
        A float in [0.0, 1.0].
    """
    def _tokens(s: str) -> set[str]:
        return set(re.findall(r"\b\w+\b", s.lower()))

    ta = _tokens(a)
    tb = _tokens(b)

    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0

    intersection = ta & tb
    union = ta | tb
    return len(intersection) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# Gap resolution
# ---------------------------------------------------------------------------


def resolve_coverage_gaps(
    parent_ac: str,
    child_acs: list[str],
    similarity_threshold: float = 0.85,
) -> dict[str, Any]:
    """Check whether a single parent AC can be auto-closed via a child AC.

    An unambiguous gap is one where a child AC is a very close match
    (above ``similarity_threshold``) to the uncovered parent AC.

    Arguments:
        parent_ac: The uncovered parent AC string.
        child_acs: All child AC strings (flat list).
        similarity_threshold: Threshold for "unambiguous" match (higher than
            the general coverage threshold). Defaults to 0.85.

    Returns:
        A dict with:
          - ``resolved``: bool — True if an unambiguous match was found.
          - ``matched_child_ac``: str or None — the matching child AC.
          - ``match_score``: float — similarity score (0.0–1.0).
          - ``conflict``: bool — True if the gap cannot be resolved.
    """
    best_score = 0.0
    best_match: str | None = None

    for c_ac in child_acs:
        score = jaccard_similarity(parent_ac, c_ac)
        if score > best_score:
            best_score = score
            best_match = c_ac

    if best_score >= similarity_threshold:
        return {
            "resolved": True,
            "matched_child_ac": best_match,
            "match_score": best_score,
            "conflict": False,
        }

    return {
        "resolved": False,
        "matched_child_ac": None,
        "match_score": best_score,
        "conflict": best_score > 0.0,  # partial match = potential conflict
    }


# ---------------------------------------------------------------------------
# Full coverage review (orchestrator)
# ---------------------------------------------------------------------------


def run_coverage_review(
    work_item_id: str,
    runner: Any | None = None,
) -> dict[str, Any]:
    """Run a full AC coverage review for a work item and its children.

    Orchestrates tree fetch, dependency ordering, AC extraction, coverage
    computation, and gap resolution to produce a comprehensive coverage
    report.

    Arguments:
        work_item_id: The work item to review (parent node).
        runner: Optional test runner.

    Returns:
        A dict with:
          - ``work_item_id``: the reviewed work item
          - ``parent_acs``: extracted parent ACs
          - ``child_summary``: list of dicts with child ID, title, AC count
          - ``coverage``: result from ``compute_coverage``
          - ``resolved_gaps``: list of gap resolutions (from ``resolve_coverage_gaps``)
          - ``unresolvable_conflicts``: list of conflict descriptions
          - ``recommendation``: one of ``"proceed"``, ``"auto_close"``, or ``"stop"``
    """
    # Fetch children
    children_data = _wl_show_children(work_item_id, runner=runner)
    if not children_data:
        return {
            "work_item_id": work_item_id,
            "parent_acs": extract_acs_from_item(work_item_id, runner=runner),
            "child_summary": [],
            "coverage": {
                "coverage_map": {},
                "uncovered": [],
                "fully_covered": True,
                "coverage_pct": 100.0,
            },
            "resolved_gaps": [],
            "unresolvable_conflicts": [],
            "recommendation": "proceed",
        }

    # Order children by dependencies
    ordered_children = order_by_dependencies(children_data, work_item_id, runner=runner)

    # Extract parent ACs
    parent_acs = extract_acs_from_item(work_item_id, runner=runner)

    # Extract child ACs
    child_acs_flat: list[str] = []
    child_acs_list: list[list[str]] = []
    child_summary: list[dict] = []

    for child in ordered_children:
        cid = child["id"]
        c_acs = extract_acs_from_item(cid, runner=runner)
        child_acs_list.append(c_acs)
        child_acs_flat.extend(c_acs)
        child_summary.append({
            "id": cid,
            "title": child.get("title", ""),
            "ac_count": len(c_acs),
        })

    # Compute coverage
    coverage = compute_coverage(parent_acs, child_acs_list)

    # Resolve gaps
    resolved_gaps: list[dict] = []
    unresolvable_conflicts: list[str] = []

    for p_idx, parent_ac in enumerate(parent_acs):
        if parent_ac in coverage["uncovered"]:
            gap_result = resolve_coverage_gaps(
                parent_ac, child_acs_flat, similarity_threshold=0.85
            )
            if gap_result["resolved"]:
                resolved_gaps.append({
                    "parent_ac": parent_ac,
                    "matched_child_ac": gap_result["matched_child_ac"],
                    "match_score": gap_result["match_score"],
                })
            elif gap_result["conflict"]:
                unresolvable_conflicts.append(
                    f"Partial match (score={gap_result['match_score']:.2f}) but "
                    f"not close enough to auto-close: '{parent_ac}'"
                )
            else:
                unresolvable_conflicts.append(
                    f"No match found for parent AC: '{parent_ac}'"
                )

    # Determine recommendation
    if coverage["fully_covered"]:
        recommendation = "proceed"
    elif resolved_gaps and not unresolvable_conflicts:
        recommendation = "auto_close"
    else:
        recommendation = "stop"

    return {
        "work_item_id": work_item_id,
        "parent_acs": parent_acs,
        "child_summary": child_summary,
        "coverage": coverage,
        "resolved_gaps": resolved_gaps,
        "unresolvable_conflicts": unresolvable_conflicts,
        "recommendation": recommendation,
    }
