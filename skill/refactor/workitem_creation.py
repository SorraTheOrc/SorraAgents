"""Work item creation from detected code smells.

Creates Worklog work items for pre-existing code smells so that technical
debt is tracked and can be addressed systematically.

Provides:
  - create_smell_work_item(): Create a single work item for one smell.
  - create_smell_work_items(): Batch-create work items for multiple smells.
  - severity_to_priority(): Map smell severity to work item priority.
  - build_smell_title(): Generate a work item title from a smell.
  - build_smell_description(): Generate a work item description from a smell.
  - has_existing_smell_comment(): Check for existing REFACTOR comments.

Usage:

    from skill.refactor.workitem_creation import (
        create_smell_work_item,
        create_smell_work_items,
        has_existing_smell_comment,
    )

    smell = {"file": "src/main.py", "line": 42, "severity": "critical", ...}
    work_item_id = create_smell_work_item(smell)

    # Batch creation with duplicate prevention
    results = create_smell_work_items([smell1, smell2, smell3])
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any


LOG = logging.getLogger("refactor.workitem_creation")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Severity-to-priority mapping.
SEVERITY_PRIORITY_MAP: dict[str, str] = {
    "critical": "high",
    "high": "high",
    "medium": "medium",
    "low": "low",
}

# Default priority for unknown or missing severity values.
DEFAULT_PRIORITY = "medium"

# The tag applied to all smell-based work items.
REFACTOR_TAG = "Refactor"

# Prefix for work item titles.
TITLE_PREFIX = "Refactor:"

# Pattern to detect existing REFACTOR comments in source files.
REFACTOR_COMMENT_PATTERN = re.compile(
    r"REFACTOR-(\S+)\s*\n\s*(?:#|//|--|<!--)\s*smell:\s*(\S+)",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def severity_to_priority(severity: Any) -> str:
    """Map a smell severity level to a Worklog priority.

    Args:
        severity: The severity value (``"critical"``, ``"high"``,
                  ``"medium"``, ``"low"``, or any other value).

    Returns:
        A Worklog priority string (``"high"``, ``"medium"``, ``"low"``).
    """
    if not isinstance(severity, str):
        return DEFAULT_PRIORITY
    return SEVERITY_PRIORITY_MAP.get(severity.lower(), DEFAULT_PRIORITY)


def build_smell_title(smell: dict[str, Any]) -> str:
    """Build a descriptive work item title for a code smell.

    The title follows the format::

        Refactor: <Smell Type> in <file>

    Args:
        smell: A smell finding dict with at least ``"file"``,
               ``"smell_type"``, and ``"message"`` keys.

    Returns:
        A title string suitable for a work item.
    """
    smell_type = smell.get("smell_type", "unknown").replace("_", " ").title()
    file_path = smell.get("file", "unknown file")
    return f"{TITLE_PREFIX} {smell_type} in {file_path}"


def build_smell_description(smell: dict[str, Any]) -> str:
    """Build a detailed work item description for a code smell.

    The description includes the file path, line number, smell type,
    severity, source, and original message in a structured markdown format.

    Args:
        smell: A smell finding dict.

    Returns:
        A markdown-formatted description string.
    """
    file_path = smell.get("file", "unknown")
    line = smell.get("line", 0)
    severity = smell.get("severity", "medium")
    smell_type = smell.get("smell_type", "unknown")
    source = smell.get("source", "unknown")
    code = smell.get("code", "")
    message = smell.get("message", "No description provided")

    parts = [
        f"## Code Smell: {smell_type}",
        "",
        f"- **File:** `{file_path}`",
        f"- **Line:** {line}",
        f"- **Severity:** {severity}",
        f"- **Source:** {source}",
        f"- **Code:** `{code}`" if code else "",
        "",
        "### Description",
        "",
        message,
        "",
        "---",
        "",
        "This work item was automatically created by the refactor skill.",
    ]
    return "\n".join(p for p in parts if p)


def has_existing_smell_comment(file_path: str, smell_type: str) -> bool:
    """Check if a source file already has a REFACTOR comment for a smell type.

    Prevents duplicate work items for the same code smell.

    Args:
        file_path: Path to the source file to check.
        smell_type: The smell type to look for (e.g. ``"security"``).

    Returns:
        ``True`` if a REFACTOR comment with the given smell type exists,
        ``False`` otherwise.
    """
    if not os.path.isfile(file_path):
        return False

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return False

    if not content.strip():
        return False

    # Check for REFACTOR comments matching the smell type
    for match in REFACTOR_COMMENT_PATTERN.finditer(content):
        matched_smell_type = match.group(2).strip().lower()
        if matched_smell_type == smell_type.lower():
            return True

    return False


def create_smell_work_item(smell: dict[str, Any]) -> str | None:
    """Create a single Worklog work item for a code smell.

    Checks for duplicate REFACTOR comments before creating. If a duplicate
    exists, returns ``None`` without creating a new work item.

    Args:
        smell: A smell finding dict.

    Returns:
        The work item ID if creation succeeded, or ``None`` if it was
        skipped (duplicate) or failed.
    """
    # Check for duplicate first
    file_path = smell.get("file", "")
    smell_type = smell.get("smell_type", "unknown")
    if file_path and has_existing_smell_comment(file_path, smell_type):
        LOG.info(
            "Skipping creation for %s in %s: duplicate REFACTOR comment exists",
            smell_type,
            file_path,
        )
        return None

    title = build_smell_title(smell)
    description = build_smell_description(smell)
    priority = severity_to_priority(smell.get("severity", "medium"))

    # Build the wl create command
    cmd = [
        "wl",
        "create",
        "--title",
        title,
        "--description",
        description,
        "--priority",
        priority,
        "--tags",
        REFACTOR_TAG,
        "--json",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        LOG.warning("wl command not found; cannot create work item")
        return None
    except subprocess.TimeoutExpired:
        LOG.warning("wl create timed out for smell: %s", smell_type)
        return None
    except OSError as exc:
        LOG.warning("Failed to run wl create: %s", exc)
        return None

    if result.returncode != 0:
        LOG.warning(
            "wl create failed (code %d): %s", result.returncode, result.stderr
        )
        return None

    # Parse the JSON output
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        LOG.warning("Failed to parse wl create output: %s", result.stdout)
        return None

    work_item_id = data.get("workItem", {}).get("id")
    if work_item_id:
        LOG.info("Created work item %s for %s", work_item_id, title)
        return work_item_id

    LOG.warning("wl create output missing workItem.id: %s", result.stdout)
    return None


def create_smell_work_items(
    smells: list[dict[str, Any]],
) -> list[str]:
    """Batch-create Worklog work items for a list of code smells.

    Each smell is checked for duplicate REFACTOR comments before creation.
    Duplicates are skipped silently.

    Args:
        smells: A list of smell finding dicts.

    Returns:
        A list of work item IDs that were successfully created (duplicates
        and failures are excluded).
    """
    results: list[str] = []
    for smell in smells:
        work_item_id = create_smell_work_item(smell)
        if work_item_id is not None:
            results.append(work_item_id)
    return results
