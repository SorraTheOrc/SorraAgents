"""Context assembler — builds structured delegation payloads from work item data.

Assembles all context a delegated agent needs for autonomous implementation:
description, acceptance criteria, comments, parent chain, child items, and
the shell command to dispatch.

Usage::

    from ampa.engine.context import ContextAssembler

    assembler = ContextAssembler(work_item_fetcher=fetcher)
    ctx = assembler.assemble("WL-123")
    cmd = build_dispatch_command(ctx.work_item_id, action="implement")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

LOG = logging.getLogger("ampa.engine.context")


# ---------------------------------------------------------------------------
# Protocols for external dependencies (mockable)
# ---------------------------------------------------------------------------


class WorkItemFetcher(Protocol):
    """Protocol for fetching full work item data from ``wl show``."""

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        """Return the ``wl show {id} --children --json`` output, or ``None``.

        The returned dict should have the structure::

            {
                "workItem": { ... },
                "comments": [ ... ],
                "children": [ ... ],
            }
        """
        ...


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comment:
    """A structured comment from a work item."""

    author: str
    content: str
    timestamp: str = ""


@dataclass(frozen=True)
class ParentContext:
    """Context from the parent work item."""

    id: str
    title: str
    description: str  # Truncated if very long
    status: str = ""
    stage: str = ""


@dataclass(frozen=True)
class ChildItem:
    """Summary of a child work item."""

    id: str
    title: str
    status: str = ""
    stage: str = ""


@dataclass(frozen=True)
class DelegationContext:
    """Complete context for a delegation payload.

    Contains all information a delegated agent needs for autonomous work.
    """

    work_item_id: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...] = ()
    comments: tuple[Comment, ...] = ()
    parent_context: ParentContext | None = None
    child_items: tuple[ChildItem, ...] = ()
    tags: tuple[str, ...] = ()
    stage: str = ""
    status: str = ""
    assignee: str = ""
    priority: str = ""
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Acceptance criteria extraction
# ---------------------------------------------------------------------------

# Pattern: ## Acceptance Criteria header (any heading level)
_RE_AC_HEADER = re.compile(
    r"^#{1,6}\s+Acceptance\s+Criteria\b",
    re.IGNORECASE | re.MULTILINE,
)

# Pattern: checkbox items - [ ] or - [x]
_RE_CHECKBOX = re.compile(
    r"^\s*[-*]\s+\[[ xX]\]\s+(.+)$",
    re.MULTILINE,
)


def extract_acceptance_criteria(description: str) -> tuple[str, ...]:
    """Extract acceptance criteria from a work item description.

    Looks for:
    1. An ``## Acceptance Criteria`` section — extracts all lines until the
       next heading.
    2. Checkbox items (``- [ ] ...`` or ``- [x] ...``) anywhere in the
       description.

    Returns a tuple of criteria strings (deduplicated, order preserved).
    """
    criteria: list[str] = []
    seen: set[str] = set()

    # Strategy 1: Extract from AC section
    match = _RE_AC_HEADER.search(description)
    if match:
        # Find the section content until the next heading or end
        section_start = match.end()
        # Find next heading
        next_heading = re.search(
            r"^#{1,6}\s+", description[section_start:], re.MULTILINE
        )
        if next_heading:
            section_text = description[
                section_start : section_start + next_heading.start()
            ]
        else:
            section_text = description[section_start:]

        # Extract checkbox items from the section
        for cb_match in _RE_CHECKBOX.finditer(section_text):
            item = cb_match.group(1).strip()
            if item and item not in seen:
                criteria.append(item)
                seen.add(item)

        # If no checkboxes found, extract non-empty lines as criteria
        if not criteria:
            for line in section_text.strip().splitlines():
                line = line.strip()
                # Skip empty lines and list markers
                if line and line not in seen:
                    # Remove leading list markers
                    cleaned = re.sub(r"^[-*]\s+", "", line).strip()
                    if cleaned:
                        criteria.append(cleaned)
                        seen.add(cleaned)

    # Strategy 2: If no AC section found, find all checkbox items
    if not criteria:
        for cb_match in _RE_CHECKBOX.finditer(description):
            item = cb_match.group(1).strip()
            if item and item not in seen:
                criteria.append(item)
                seen.add(item)

    return tuple(criteria)


# ---------------------------------------------------------------------------
# Work item data extraction
# ---------------------------------------------------------------------------


def _extract_work_item(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the work item dict from a ``wl show`` payload."""
    # Handle workItem wrapper
    wi = payload.get("workItem")
    if isinstance(wi, dict):
        return wi
    # Also try common wrapper keys
    for key in ("work_item", "item", "data"):
        val = payload.get(key)
        if isinstance(val, dict):
            return val
    return payload


def _extract_comments(payload: dict[str, Any]) -> tuple[Comment, ...]:
    """Extract structured comments from the payload."""
    comments_list = payload.get("comments") or []
    result: list[Comment] = []
    for c in comments_list:
        if not isinstance(c, dict):
            continue
        content = c.get("comment") or c.get("body") or c.get("text") or ""
        if not content:
            continue
        result.append(
            Comment(
                author=str(c.get("author") or c.get("user") or ""),
                content=str(content),
                timestamp=str(
                    c.get("createdAt")
                    or c.get("created_at")
                    or c.get("timestamp")
                    or ""
                ),
            )
        )
    return tuple(result)


def _extract_children(payload: dict[str, Any]) -> tuple[ChildItem, ...]:
    """Extract child item summaries from the payload."""
    children_list = payload.get("children") or []
    result: list[ChildItem] = []
    for child in children_list:
        if not isinstance(child, dict):
            continue
        child_id = str(child.get("id") or "")
        if not child_id:
            continue
        result.append(
            ChildItem(
                id=child_id,
                title=str(child.get("title") or ""),
                status=str(child.get("status") or ""),
                stage=str(child.get("stage") or ""),
            )
        )
    return tuple(result)


def _extract_parent_context(
    wi: dict[str, Any],
    payload: dict[str, Any],
    max_description_length: int = 500,
) -> ParentContext | None:
    """Extract parent context from ancestors or parentId.

    Uses the ``ancestors`` list if available, otherwise falls back to
    ``parentId`` with limited information.
    """
    # Try ancestors list first (from wl show --children --json)
    ancestors = payload.get("ancestors") or []
    if ancestors and isinstance(ancestors, list):
        # Immediate parent is the last ancestor
        parent = ancestors[-1] if ancestors else None
        if isinstance(parent, dict):
            desc = str(parent.get("description") or "")
            if len(desc) > max_description_length:
                desc = desc[:max_description_length].rstrip() + "..."
            return ParentContext(
                id=str(parent.get("id") or ""),
                title=str(parent.get("title") or ""),
                description=desc,
                status=str(parent.get("status") or ""),
                stage=str(parent.get("stage") or ""),
            )

    # Fallback: parentId field on the work item itself
    parent_id = wi.get("parentId") or wi.get("parent_id") or ""
    if parent_id:
        return ParentContext(
            id=str(parent_id),
            title="",
            description="(parent details not available)",
        )

    return None


def _extract_tags(wi: dict[str, Any]) -> tuple[str, ...]:
    """Extract and normalize tags."""
    tags = wi.get("tags") or []
    if isinstance(tags, str):
        return tuple(t.strip() for t in tags.split(",") if t.strip())
    if isinstance(tags, list):
        return tuple(str(t) for t in tags if t)
    return ()


# ---------------------------------------------------------------------------
# Context assembler
# ---------------------------------------------------------------------------


class ContextAssembler:
    """Builds structured delegation contexts from work item data.

    Parameters
    ----------
    work_item_fetcher:
        Implementation that fetches full work item data (``wl show``).
    max_parent_description:
        Maximum characters for parent description (truncated with ``...``).
    """

    def __init__(
        self,
        work_item_fetcher: WorkItemFetcher,
        max_parent_description: int = 500,
    ) -> None:
        self._fetcher = work_item_fetcher
        self._max_parent_desc = max_parent_description

    def assemble(self, work_item_id: str) -> DelegationContext | None:
        """Assemble a complete delegation context for a work item.

        Returns ``None`` if the work item cannot be fetched.
        """
        payload = self._fetcher.fetch(work_item_id)
        if payload is None:
            LOG.warning("Failed to fetch work item %s", work_item_id)
            return None

        wi = _extract_work_item(payload)

        description = str(wi.get("description") or "")
        acceptance_criteria = extract_acceptance_criteria(description)
        comments = _extract_comments(payload)
        children = _extract_children(payload)
        parent = _extract_parent_context(wi, payload, self._max_parent_desc)
        tags = _extract_tags(wi)

        return DelegationContext(
            work_item_id=str(wi.get("id") or work_item_id),
            title=str(wi.get("title") or ""),
            description=description,
            acceptance_criteria=acceptance_criteria,
            comments=comments,
            parent_context=parent,
            child_items=children,
            tags=tags,
            stage=str(wi.get("stage") or ""),
            status=str(wi.get("status") or ""),
            assignee=str(wi.get("assignee") or ""),
            priority=str(wi.get("priority") or ""),
            metadata={
                k: v
                for k, v in wi.items()
                if k
                not in {
                    "id",
                    "title",
                    "description",
                    "tags",
                    "stage",
                    "status",
                    "assignee",
                    "priority",
                    "parentId",
                    "parent_id",
                }
            },
        )


# ---------------------------------------------------------------------------
# Shell command builder
# ---------------------------------------------------------------------------

# Stage-to-action mapping from engine PRD Section 5.2
STAGE_ACTION_MAP: dict[str, str] = {
    "idea": "intake",
    "intake_complete": "plan",
    "plan_complete": "implement",
}

# Action-to-shell-command templates
ACTION_COMMAND_MAP: dict[str, str] = {
    "intake": 'opencode run "/intake {id} do not ask questions"',
    "plan": 'opencode run "/plan {id}"',
    "implement": 'opencode run "work on {id} using the implement skill"',
}


def stage_to_action(stage: str) -> str | None:
    """Map a work item stage to a delegation action.

    Returns ``None`` if the stage has no delegation action.
    """
    return STAGE_ACTION_MAP.get(stage)


def build_dispatch_command(work_item_id: str, action: str) -> str | None:
    """Build the shell command string for dispatching a delegation.

    Parameters
    ----------
    work_item_id:
        The work item ID to include in the command.
    action:
        The delegation action (``intake``, ``plan``, ``implement``).

    Returns
    -------
    str or None
        The shell command string, or ``None`` if the action is unknown.
    """
    template = ACTION_COMMAND_MAP.get(action)
    if template is None:
        return None
    return template.format(id=work_item_id)
