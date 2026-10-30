"""Shared tree traversal and AC-coverage helpers for plan/intake skills.

Provides functions for:
  - Fetching the full descendant tree of a work item
  - Ordering siblings by ``wl dep`` edges (topological) then listed order
  - Extracting acceptance criteria from work item descriptions
  - Computing whether child ACs collectively cover parent ACs
  - Auto-closing unambiguous coverage gaps
  - Detecting and reporting unresolvable conflicts
  - Applying the review: closing gaps, commenting, and advancing/stopping

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

from shared.status_lifecycle import StatusLifecycle, resolve_worklog_flags

logger = logging.getLogger("tree_coverage")

#: Similarity floor for auto-closing an uncovered parent AC.
#:
#: ``compute_coverage`` treats a child AC as covering a parent AC at or above
#: the coverage threshold (0.4). An uncovered AC whose best match to any child
#: AC is at least this floor has a *clear mapping* — the child already partially
#: covers it — so the review can close the gap without inventing requirements.
#: Below the floor (including zero overlap) the mapping is unclear and the
#: review stops. Keeping the floor below the coverage threshold is what makes
#: the ``auto_close`` recommendation reachable at all.
AUTO_CLOSE_SIMILARITY_THRESHOLD = 0.2

#: Stable marker embedded in coverage-review comments so re-runs are idempotent.
COVERAGE_COMMENT_MARKER = "[tree-coverage-review]"

#: Diagnostic used when an ``## Acceptance Criteria`` section exists but no
#: entries could be parsed (fail-closed guard).
AC_SECTION_UNPARSEABLE_MESSAGE = (
    "An 'Acceptance Criteria' section is present but no AC entries "
    "could be parsed (check list formatting: use '- ', '+ ' or "
    "'1. ' markers). Failing closed instead of reporting vacuous "
    "100% coverage."
)

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
    """Return the *direct* children of *work_item_id*.

    Uses ``wl list --parent <id> --json`` (direct children only). The earlier
    ``wl show <id> --children`` form returns the *full descendant set*, which
    would make grandchildren look like siblings; the tree helpers require
    direct children so recursion and coverage walks stay correct.
    """
    cmd = ["wl", "list", "--parent", work_item_id, "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning(
            "wl list children failed target=%s stderr=%s",
            work_item_id, proc.stderr,
        )
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "wl list children invalid JSON target=%s", work_item_id
        )
        return []
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning(
            "wl list children returned error target=%s", work_item_id
        )
        return []
    if not isinstance(data, dict):
        return []
    # Real ``wl list`` shape is ``workItems``; the ``children`` key is kept for
    # backward compatibility with callers that pass ``show --children`` data.
    items = data.get("workItems", data.get("children", []))
    return items if isinstance(items, list) else []


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

    # Build dependency map: child_id -> set of prerequisite IDs.
    #
    # Two sources are merged:
    #   1. Parent-level edges (legacy shape) from ``_get_dep_edges`` — kept for
    #      callers/tests that pass synthetic edge dicts (targetId/prerequisiteId).
    #   2. Each child's *own* outbound edges from ``wl dep list <child>`` — the
    #      real worklog shape (``{"item":..., "inbound":[...], "outbound":[...]}``).
    #      Child-to-child edges live on the children, not the parent, so (2) is
    #      what makes real dependency ordering work.
    deps: dict[str, set[str]] = {}
    for edge in _get_dep_edges(work_item_id, runner=runner):
        target = edge.get("targetId") or edge.get("target")
        prereq = edge.get("prerequisiteId") or edge.get("prerequisite")
        if target and prereq:
            deps.setdefault(target, set()).add(prereq)

    for child in children:
        child_id = child.get("id")
        if not child_id:
            continue
        for prereq in _get_child_dependency_ids(child_id, runner=runner):
            deps.setdefault(child_id, set()).add(prereq)

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


def _get_child_dependency_ids(
    child_id: str,
    runner: Any | None = None,
) -> list[str]:
    """Return the IDs of *child_id*'s outbound prerequisites.

    Calls ``wl dep list <child_id> --json`` and reads the real worklog shape::

        {"success": true, "item": "<id>", "inbound": [...], "outbound": [...]}

    Each ``outbound`` entry is a prerequisite the child depends on (its ``id``
    is the prerequisite's work-item ID). The legacy ``dependencies`` array
    shape is also accepted. Returns an empty list on any failure so ordering
    degrades gracefully to listed order.
    """
    cmd = ["wl", "dep", "list", child_id, "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning(
            "wl dep list (child) failed target=%s stderr=%s",
            child_id, proc.stderr,
        )
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("wl dep list (child) invalid JSON target=%s", child_id)
        return []
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning("wl dep list (child) returned error target=%s", child_id)
        return []

    prereqs: list[str] = []

    if isinstance(data, dict) and isinstance(data.get("outbound"), list):
        # Real wl shape: each outbound entry's ``id`` is a prerequisite.
        for edge in data["outbound"]:
            if not isinstance(edge, dict):
                continue
            prereq = (
                edge.get("id")
                or edge.get("targetId")
                or edge.get("prerequisiteId")
            )
            if prereq:
                prereqs.append(prereq)
        return prereqs

    # Legacy array shape: {"targetId": <depending id>, "prerequisiteId": <prereq>}.
    legacy: list = []
    if isinstance(data, dict) and isinstance(data.get("dependencies"), list):
        legacy = data["dependencies"]
    elif isinstance(data, list):
        legacy = data
    for edge in legacy:
        if not isinstance(edge, dict):
            continue
        target = edge.get("targetId") or edge.get("target")
        if target and target != child_id:
            continue
        prereq = edge.get("prerequisiteId") or edge.get("prerequisite")
        if prereq:
            prereqs.append(prereq)
    return prereqs


# ---------------------------------------------------------------------------
# AC extraction
# ---------------------------------------------------------------------------

# Ordered-list AC marker: ``1. text`` or ``1) text`` (any number of digits).
_ORDERED_AC_RE = re.compile(r"^\d+[.)]\s+(.+)$")

# Fenced-code-block delimiter (````` or ``~~~``); the fence's content is not AC text.
_CODE_FENCE_RE = re.compile(r"^(```|~~~)")

# A lone list marker or a thematic break: an entry boundary, never AC text.
_AC_BOUNDARY_RE = re.compile(r"^(?:[-+*]|\d+[.)])$|^(?:[-*_]\s*){3,}$")


def extract_acceptance_criteria(description: str) -> list[str]:
    """Extract acceptance criteria entries from a work item description.

    Looks for a section header matching ``## Acceptance Criteria`` (case-insensitive)
    and collects list entries until the next section header (``##``) or end of
    content. Both markdown bullets (``- `` / ``+ ``) and ordered-list items
    (``1. `` / ``1) ``) are recognised; bullets and numbers interleave in
    document order of appearance.

    Strips the leading list marker (``- `` / ``+ `` or the trailing ``digits.)``
    plus whitespace) and surrounding whitespace from each entry. Text after an
    ordered-list marker is kept verbatim, so bold-led items such as
    ``1. **Title.** text`` yield ``**Title.** text``; an unwrapped single-line
    entry is returned exactly as written.

    **Continuation joining (hard-wrapped bullets).** Only *top-level* list
    items start an AC: a marker indented deeper than the first AC marker is
    nested detail inside an AC item (a sub-list or explanatory bullet), not an
    AC itself, and its text is never appended to the parent AC. A line with no
    list marker (a wrapped continuation line) that is not blank, not a heading,
    not a table row, and not inside a fenced code block is appended to the
    open AC, so a bullet hard-wrapped over N physical lines yields exactly one
    complete AC. Continuation lines are joined with a single space, except
    where the accumulated text ends with a hyphen attached to the preceding
    word (``high-`` + ``priority``), which is rejoined without a space; a
    hyphen preceded by whitespace (``text -``) keeps the separating space.
    Joined entries have intra-entry whitespace collapsed to single spaces.

    **Entry boundaries.** An AC entry is flushed by the next top-level list
    marker, by a blank line, by a heading, by a table row, by a code fence, by
    a lone list marker or thematic break, or by the end of the section. Text
    after a blank line is therefore never merged into the preceding AC.

    **Ignored content.** Fenced code-block lines, markdown table rows, and
    lone list markers / thematic breaks are never appended to an AC.

    Arguments:
        description: The work item description text.

    Returns:
        A list of AC strings (empty list if no section found).
    """
    lines = description.split("\n")
    acs: list[str] = []
    in_ac_section = False
    in_code_fence = False
    # Leading-whitespace depth of the first AC marker; any marker indented
    # deeper is nested detail under an AC item, not a top-level AC.
    base_indent: int | None = None
    # Lines accumulated for the AC entry currently being built (its first line
    # plus any wrapped continuation lines).
    pending: list[str] = []

    def _flush() -> None:
        """Close the pending AC entry, if any."""
        if not pending:
            return
        acs.append(_join_ac_continuations(pending))
        pending.clear()

    for line in lines:
        stripped = line.strip()
        # Check for the AC section header
        if re.match(r"^#+\s+Acceptance\s+Criteria", stripped, re.IGNORECASE):
            # A repeated header terminates any open entry and re-enters.
            _flush()
            in_ac_section = True
            in_code_fence = False
            continue
        if not in_ac_section:
            continue
        # Fenced code blocks are not AC text (and end the open entry).
        if _CODE_FENCE_RE.match(stripped):
            _flush()
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        # Exit the section if we hit another heading
        if re.match(r"^##\s+", stripped):
            break
        if not stripped:
            # A blank line terminates the current AC entry.
            _flush()
            continue
        if stripped.startswith("|"):
            # Markdown table row: not AC text, and ends the open entry.
            _flush()
            continue
        if _AC_BOUNDARY_RE.match(stripped):
            # A lone list marker or thematic break: ends the open entry and is
            # itself not AC text.
            _flush()
            continue
        if re.match(r"^#+\s", stripped):
            # Deeper heading inside the section: ends the open entry.
            _flush()
            continue
        # Collect AC entries: markdown bullets and ordered-list items
        ac_text = _strip_ac_list_marker(stripped)
        if ac_text:
            indent = len(line) - len(line.lstrip())
            if base_indent is None:
                base_indent = indent
            _flush()
            if indent > base_indent:
                # Deeper-indented marker: nested detail under an AC item.
                # It is not an AC and its text must not join the parent AC.
                continue
            pending.append(ac_text)
            continue
        # Unmarked continuation line: append it to the open AC entry.
        if pending:
            pending.append(stripped)

    _flush()
    return acs


def _join_ac_continuations(parts: list[str]) -> str:
    """Join an AC's first line with its wrapped continuation lines.

    Continuation lines are separated by a single space, except when the text
    accumulated so far ends with a hyphen attached to the preceding word
    (``high-`` + ``priority``), where the parts are rejoined without a space.
    Multi-line entries have intra-entry whitespace collapsed to single spaces;
    a single-line entry is returned unchanged so pre-existing behaviour is
    preserved byte for byte.
    """
    if len(parts) == 1:
        return parts[0]
    joined = parts[0]
    for part in parts[1:]:
        if len(joined) > 1 and joined.endswith("-") and not joined[-2].isspace():
            joined += part
        else:
            joined += " " + part
    return re.sub(r"\s+", " ", joined).strip()


def has_acceptance_criteria_section(description: str) -> bool:
    """Return True when *description* contains an AC section header."""
    return bool(
        re.search(
            r"^#+\s+Acceptance\s+Criteria",
            description,
            re.MULTILINE | re.IGNORECASE,
        )
    )


def _strip_ac_list_marker(stripped: str) -> str:
    """Strip a markdown bullet or ordered-list marker from an AC line.

    Returns the entry text with surrounding whitespace trimmed, or an empty
    string when the line is not a bullet / ordered-list entry.
    """
    for marker in ("- ", "+ "):
        if stripped.startswith(marker):
            return stripped[len(marker):].strip()
    match = _ORDERED_AC_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return ""



def _fetch_item_description(
    work_item_id: str,
    runner: Any | None = None,
) -> str | None:
    """Fetch a work item's description via ``wl show``.

    Returns the description string, or ``None`` when the fetch fails, the
    payload is not valid JSON, or the item reports an error.
    """
    cmd = ["wl", "show", work_item_id, "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning(
            "wl show failed for AC extraction target=%s", work_item_id
        )
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "wl show invalid JSON for AC extraction target=%s", work_item_id
        )
        return None
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning(
            "wl show returned error for AC extraction target=%s", work_item_id
        )
        return None
    description = data.get("workItem", {}).get("description", "")
    if not isinstance(description, str):
        return None
    return description


def extract_acs_from_item(
    work_item_id: str,
    runner: Any | None = None,
) -> list[str]:
    """Extract ACs from a work item by fetching it via ``wl show``."""
    description = _fetch_item_description(work_item_id, runner=runner)
    if description is None:
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
          - ``warnings``: non-fatal diagnostics (e.g. an unparseable AC section)
          - ``recommendation``: one of ``"proceed"``, ``"auto_close"``, or ``"stop"``

    Fails closed: when the parent has an ``## Acceptance Criteria`` section but
    no AC entries could be parsed, the recommendation is ``"stop"`` (with a
    warning) instead of a vacuous ``fully_covered: true``. An absent or empty
    AC section is not a failure and keeps the historical behaviour.
    """
    # Fetch the parent description once so AC extraction and the AC-section
    # presence check share a single ``wl show`` call.
    parent_description = _fetch_item_description(work_item_id, runner=runner)
    if parent_description is None:
        parent_acs: list[str] = []
        ac_section_present = False
    else:
        parent_acs = extract_acceptance_criteria(parent_description)
        ac_section_present = has_acceptance_criteria_section(parent_description)

    # Fail closed: an AC section that exists but yields nothing parseable is
    # an extraction failure, not proof of coverage.
    warnings: list[str] = []
    ac_section_unparseable = ac_section_present and not parent_acs

    # Fetch children
    children_data = _wl_show_children(work_item_id, runner=runner)
    if not children_data:
        if ac_section_unparseable:
            return _fail_closed_review(
                work_item_id,
                [],
                warnings,
                AC_SECTION_UNPARSEABLE_MESSAGE,
            )
        return {
            "work_item_id": work_item_id,
            "parent_acs": parent_acs,
            "child_summary": [],
            "coverage": {
                "coverage_map": {},
                "uncovered": [],
                "fully_covered": True,
                "coverage_pct": 100.0,
            },
            "resolved_gaps": [],
            "unresolvable_conflicts": [],
            "warnings": warnings,
            "recommendation": "proceed",
        }

    # Order children by dependencies
    ordered_children = order_by_dependencies(children_data, work_item_id, runner=runner)

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

    if ac_section_unparseable:
        result = _fail_closed_review(
            work_item_id,
            child_summary,
            warnings,
            AC_SECTION_UNPARSEABLE_MESSAGE,
        )
        return result

    # Compute coverage
    coverage = compute_coverage(parent_acs, child_acs_list)

    # Resolve gaps
    resolved_gaps: list[dict] = []
    unresolvable_conflicts: list[str] = []

    for p_idx, parent_ac in enumerate(parent_acs):
        if parent_ac in coverage["uncovered"]:
            gap_result = resolve_coverage_gaps(
                parent_ac,
                child_acs_flat,
                similarity_threshold=AUTO_CLOSE_SIMILARITY_THRESHOLD,
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
        "warnings": warnings,
        "recommendation": recommendation,
    }


def _fail_closed_review(
    work_item_id: str,
    child_summary: list[dict],
    warnings: list[str],
    message: str,
) -> dict[str, Any]:
    """Build a fail-closed coverage-review result.

    Reports ``stop`` with ``fully_covered: False`` and the diagnostic *message*
    (also appended to *warnings* and ``unresolvable_conflicts``), so
    ``apply_coverage_review`` records it and does not advance the stage.
    """
    warnings.append(message)
    return {
        "work_item_id": work_item_id,
        "parent_acs": [],
        "child_summary": child_summary,
        "coverage": {
            "coverage_map": {},
            "uncovered": [],
            "fully_covered": False,
            "coverage_pct": 0.0,
        },
        "resolved_gaps": [],
        "unresolvable_conflicts": [message],
        "warnings": warnings,
        "recommendation": "stop",
    }


# ---------------------------------------------------------------------------
# Apply step (mutation) — used by the plan/intake final review
# ---------------------------------------------------------------------------


def _run_wl_json(
    command: list[str],
    runner: Any | None = None,
) -> dict[str, Any] | None:
    """Run a ``wl`` command and return parsed JSON, or ``None`` on failure."""
    cmd = list(command)
    cmd[1:1] = resolve_worklog_flags(cmd)
    proc = _execute_subprocess(cmd, runner=runner)
    if proc.returncode != 0:
        logger.warning(
            "wl command failed cmd=%s stderr=%s", " ".join(cmd), proc.stderr
        )
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("wl command returned invalid JSON cmd=%s", " ".join(cmd))
        return None
    if isinstance(data, dict) and data.get("success") is False:
        logger.warning("wl command returned error cmd=%s", " ".join(cmd))
        return None
    return data


def _comment_bodies(work_item_id: str, runner: Any | None = None) -> list[str]:
    """Return the bodies of *work_item_id*'s comments (best effort)."""
    data = _run_wl_json(
        ["wl", "comment", "list", work_item_id, "--json"], runner=runner
    )
    if not data:
        return []
    comments = data.get("comments", data) if isinstance(data, dict) else data
    bodies: list[str] = []
    for comment in comments or []:
        if isinstance(comment, dict):
            body = (
                comment.get("comment")
                or comment.get("body")
                or comment.get("text")
                or ""
            )
        elif isinstance(comment, str):
            body = comment
        else:
            body = ""
        if body:
            bodies.append(body)
    return bodies


def _add_comment_if_absent(
    work_item_id: str,
    body: str,
    runner: Any | None = None,
) -> bool:
    """Add *body* as a comment unless an identical review comment exists.

    Idempotence guard: re-running the apply step must not duplicate comments.
    Returns True when a comment was added.
    """
    if body in _comment_bodies(work_item_id, runner=runner):
        return False
    _run_wl_json(
        [
            "wl", "comment", "add", work_item_id,
            "--comment", body,
            "--author", "plan-intake-coverage",
            "--json",
        ],
        runner=runner,
    )
    return True


def _create_coverage_child(
    parent_id: str,
    parent_ac: str,
    runner: Any | None = None,
) -> str | None:
    """Create a child work item whose AC directly covers *parent_ac*.

    The child restates the uncovered parent AC verbatim, so it is a direct
    mapping rather than an invented requirement. Returns the new child ID, or
    ``None`` on failure.
    """
    title = f"Close coverage gap: {parent_ac}"
    if len(title) > 120:
        title = title[:117] + "..."
    description = (
        "Auto-created by the plan/intake AC coverage review to close an "
        "uncovered parent acceptance criterion.\n\n"
        "## Acceptance Criteria\n"
        f"- {parent_ac}\n"
    )
    data = _run_wl_json(
        [
            "wl", "create",
            "--title", title,
            "--description", description,
            "--parent", parent_id,
            "--issue-type", "task",
            "--priority", "medium",
            "--json",
        ],
        runner=runner,
    )
    if not data:
        return None
    item = data.get("workItem", data)
    return item.get("id") if isinstance(item, dict) else None


def _advance_stage(
    work_item_id: str,
    target_stage: str | None,
    runner: Any | None = None,
) -> bool:
    """Advance *work_item_id* to *target_stage* (status ``open``).

    Returns True when a stage was set. Uses :class:`StatusLifecycle` so the
    transition follows the shared lifecycle rules.
    """
    if not target_stage:
        return False
    StatusLifecycle.update_status(
        work_item_id, "open", stage=target_stage, runner=runner
    )
    return True


def apply_coverage_review(
    work_item_id: str,
    target_stage: str | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    """Apply the AC coverage review, mutating the worklog where needed.

    Deterministic implementation of the plan/intake skills' "final AC coverage
    review" step:

      - ``proceed``    — coverage is complete; when *target_stage* is given the
        item is advanced to that stage (status ``open``).
      - ``auto_close`` — unambiguous gaps are closed by creating a child whose
        AC restates the uncovered parent AC; a comment records the closure and
        the item is advanced to *target_stage*.
      - ``stop``       — unresolvable conflicts are recorded in a comment and
        the item is **not** advanced (left ``open``).

    Idempotent: re-running does not create duplicate children or comments, and
    a fully covered tree reports ``proceed``.

    Arguments:
        work_item_id: The reviewed work item (parent node).
        target_stage: Stage to set on success (``plan_complete`` for the plan
            skill, ``intake_complete`` for intake). ``None`` sets no stage.
        runner: Optional test runner.

    Returns:
        A dict with ``action`` (``proceed``/``auto_close``/``stop``),
        ``advanced`` (bool), ``created_children`` (IDs), ``conflicts``, and the
        raw ``review``.
    """
    review = run_coverage_review(work_item_id, runner=runner)
    recommendation = review["recommendation"]

    if recommendation == "proceed":
        advanced = _advance_stage(work_item_id, target_stage, runner=runner)
        return {
            "work_item_id": work_item_id,
            "action": "proceed",
            "advanced": advanced,
            "created_children": [],
            "conflicts": [],
            "review": review,
        }

    if recommendation == "auto_close":
        created_children: list[str] = []
        for gap in review["resolved_gaps"]:
            child_id = _create_coverage_child(
                work_item_id, gap["parent_ac"], runner=runner
            )
            if child_id:
                created_children.append(child_id)
        gaps = "; ".join(f"'{g['parent_ac']}'" for g in review["resolved_gaps"])
        body = (
            f"{COVERAGE_COMMENT_MARKER} AC coverage review auto-closed "
            f"{len(created_children)} gap(s): {gaps}"
        )
        _add_comment_if_absent(work_item_id, body, runner=runner)
        advanced = _advance_stage(work_item_id, target_stage, runner=runner)
        return {
            "work_item_id": work_item_id,
            "action": "auto_close",
            "advanced": advanced,
            "created_children": created_children,
            "conflicts": [],
            "review": review,
        }

    conflicts = review["unresolvable_conflicts"]
    body = (
        f"{COVERAGE_COMMENT_MARKER} AC coverage review stopped: "
        f"{len(conflicts)} unresolved conflict(s): " + "; ".join(conflicts)
    )
    _add_comment_if_absent(work_item_id, body, runner=runner)
    return {
        "work_item_id": work_item_id,
        "action": "stop",
        "advanced": False,
        "created_children": [],
        "conflicts": conflicts,
        "review": review,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for tree_coverage.py.

    Usage:
        python3 tree_coverage.py run-coverage-review <work-item-id>
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Tree coverage helpers for plan/intake skills."
    )
    subparsers = parser.add_subparsers(dest="command")

    review_parser = subparsers.add_parser(
        "run-coverage-review",
        help="Run a full AC coverage review for a work item.",
    )
    review_parser.add_argument("work_item_id", help="The parent work item ID.")

    args = parser.parse_args()

    if args.command == "run-coverage-review":
        result = run_coverage_review(args.work_item_id)
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
