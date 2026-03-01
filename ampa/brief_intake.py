"""Brief-to-epic creator for the APMA.

Accepts a project brief (plain text) and produces a structured plan with an
epic and child work-items in ``wl``.  Supports ``dry_run`` (returns the plan
without mutations) and ``propose`` (creates the epic and children via
``wl create``).

Usage (CLI)::

    python -m ampa.brief_intake --brief "Build a user auth system" --mode dry_run
    python -m ampa.brief_intake --brief "Build a user auth system" --mode propose

Usage (Python)::

    from ampa.brief_intake import brief_to_epic
    plan = brief_to_epic("Build a user auth system", mode="dry_run")
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("ampa.brief_intake")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_MODES = ("dry_run", "propose")

_TEMPLATE_FILE = Path(__file__).parent / "task_templates.yaml"


# ---------------------------------------------------------------------------
# Brief parsing
# ---------------------------------------------------------------------------


def _load_templates(template_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load child-task templates from YAML."""
    import yaml  # type: ignore

    path = Path(template_path) if template_path else _TEMPLATE_FILE
    if not path.exists():
        raise FileNotFoundError(f"Template file not found: {path}")
    with open(path, "r") as fh:
        data = yaml.safe_load(fh)
    return data.get("templates", [])


def _extract_title(brief: str) -> str:
    """Extract a concise title from the brief text.

    Heuristic: use the first non-empty line (stripped of markdown heading
    markers) as the title.  Falls back to the first 80 characters.
    """
    for line in brief.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return brief[:80].strip()


def _extract_summary(brief: str, max_chars: int = 1000) -> str:
    """Return a trimmed summary of the brief for embedding in descriptions."""
    text = brief.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n..."
    return text


def _extract_acceptance_criteria(brief: str) -> str:
    """Try to pull an acceptance-criteria section from the brief.

    Looks for headers like ``## Acceptance Criteria`` or ``### AC``.
    Returns the section text or an empty string if not found.
    """
    pattern = re.compile(
        r"^#{2,3}\s*(acceptance\s*criteria|ac)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(brief)
    if not match:
        return ""
    rest = brief[match.end() :]
    # Capture until next heading or end
    end_match = re.search(r"^#{1,3}\s+", rest, re.MULTILINE)
    if end_match:
        return rest[: end_match.start()].strip()
    return rest.strip()


def _build_epic_description(brief: str) -> str:
    """Compose the epic description from the brief."""
    title = _extract_title(brief)
    ac = _extract_acceptance_criteria(brief)
    desc = f"Epic created by APMA brief intake.\n\nBrief: {title}\n\n{brief.strip()}"
    if ac:
        desc += f"\n\n## Acceptance Criteria\n{ac}"
    return desc


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def _build_plan(
    brief: str,
    templates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a structured plan dict from a brief and templates.

    Returns a JSON-serializable dict with ``epic`` and ``children`` keys.
    """
    title = _extract_title(brief)
    summary = _extract_summary(brief)
    epic_description = _build_epic_description(brief)

    children: List[Dict[str, Any]] = []
    for tmpl in templates:
        child_title = f"{tmpl['title_prefix']} {title}"
        child_desc = tmpl["description_template"].format(
            brief_title=title,
            brief_summary=summary,
        )
        child_ac = tmpl["acceptance_criteria_template"].format(
            brief_title=title,
            brief_summary=summary,
        )
        children.append(
            {
                "category": tmpl.get("category", "unknown"),
                "title": child_title,
                "description": f"{child_desc.strip()}\n\n{child_ac.strip()}",
                "acceptance_criteria": child_ac.strip(),
                "suggested_assignee": tmpl.get("suggested_assignee", ""),
                "priority": tmpl.get("priority", "medium"),
                "issue_type": tmpl.get("issue_type", "task"),
            }
        )

    return {
        "epic": {
            "title": title,
            "description": epic_description,
            "issue_type": "epic",
            "priority": "high",
        },
        "children": children,
    }


# ---------------------------------------------------------------------------
# wl interaction
# ---------------------------------------------------------------------------


def _run_wl(
    args: List[str],
    cwd: Optional[str] = None,
    timeout: int = 300,
) -> Dict[str, Any]:
    """Run a ``wl`` CLI command and return parsed JSON output.

    Raises ``RuntimeError`` on non-zero exit or parse failure.
    """
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


def _create_work_item(
    title: str,
    description: str,
    *,
    issue_type: str = "task",
    priority: str = "medium",
    parent_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a work item via ``wl create`` and return the response."""
    args = [
        "create",
        "-t",
        title,
        "-d",
        description,
        "--issue-type",
        issue_type,
        "--priority",
        priority,
    ]
    if parent_id:
        args.extend(["--parent", parent_id])
    return _run_wl(args, cwd=cwd)


def _add_comment(
    work_item_id: str,
    comment: str,
    author: str = "ampa-brief-intake",
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
# Public API
# ---------------------------------------------------------------------------


def brief_to_epic(
    brief_text: str,
    *,
    mode: str = "dry_run",
    template_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse a project brief and produce an epic with child work-items.

    Parameters
    ----------
    brief_text:
        The project brief as plain text (may contain markdown).
    mode:
        ``"dry_run"`` returns the plan without creating wl items.
        ``"propose"`` creates the epic and children in wl.
    template_path:
        Optional path to a custom task_templates.yaml file.
    cwd:
        Working directory for wl commands.

    Returns
    -------
    dict
        A JSON-serializable plan dict with ``epic``, ``children``, and
        ``mode`` keys.  In ``propose`` mode, ``epic_id`` and
        ``child_ids`` are also included.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"Invalid mode {mode!r}; expected one of {_VALID_MODES}")

    if not brief_text or not brief_text.strip():
        raise ValueError("Brief text must not be empty")

    templates = _load_templates(template_path)
    plan = _build_plan(brief_text, templates)
    plan["mode"] = mode

    if mode == "dry_run":
        return plan

    # --- propose mode: create items in wl ---
    epic_data = plan["epic"]
    result = _create_work_item(
        title=epic_data["title"],
        description=epic_data["description"],
        issue_type=epic_data["issue_type"],
        priority=epic_data["priority"],
        cwd=cwd,
    )
    epic_id = result.get("workItem", {}).get("id")
    if not epic_id:
        raise RuntimeError(f"Failed to extract epic id from wl response: {result}")
    plan["epic_id"] = epic_id

    child_ids: List[str] = []
    for child in plan["children"]:
        try:
            child_result = _create_work_item(
                title=child["title"],
                description=child["description"],
                issue_type=child["issue_type"],
                priority=child["priority"],
                parent_id=epic_id,
                cwd=cwd,
            )
            child_id = child_result.get("workItem", {}).get("id", "")
            child_ids.append(child_id)
            child["created_id"] = child_id
        except RuntimeError:
            LOG.exception("Failed to create child: %s", child.get("title"))
            child_ids.append("")

    plan["child_ids"] = child_ids

    # Post a delegation rationale comment on the epic
    children_summary = "\n".join(
        f"- {c['title']} (assigned to: {c.get('suggested_assignee', 'unassigned')}, "
        f"id: {c.get('created_id', 'n/a')})"
        for c in plan["children"]
    )
    comment = (
        f"# APMA Brief Intake — Delegation Plan\n\n"
        f"Created epic and {len(child_ids)} child work-items from project brief.\n\n"
        f"## Proposed Tasks\n{children_summary}\n\n"
        f"## Rationale\n"
        f"Tasks decompose the brief into standard SDLC phases "
        f"(discovery, design, implementation, testing, documentation) "
        f"with suggested agent-group assignments based on task category."
    )
    _add_comment(epic_id, comment, cwd=cwd)

    return plan


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entry point for brief_intake."""
    parser = argparse.ArgumentParser(
        description="APMA Brief Intake — create an epic from a project brief",
    )
    parser.add_argument(
        "--brief",
        required=True,
        help="Project brief text (or @filename to read from file)",
    )
    parser.add_argument(
        "--mode",
        choices=_VALID_MODES,
        default="dry_run",
        help="dry_run (default) returns plan; propose creates wl items",
    )
    parser.add_argument(
        "--templates",
        default=None,
        help="Path to custom task_templates.yaml",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for wl commands",
    )
    args = parser.parse_args(argv)

    brief_text = args.brief
    if brief_text.startswith("@"):
        filepath = brief_text[1:]
        with open(filepath, "r") as fh:
            brief_text = fh.read()

    result = brief_to_epic(
        brief_text,
        mode=args.mode,
        template_path=args.templates,
        cwd=args.cwd,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
