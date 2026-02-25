"""Audit detection & polling flow.

This module extracts audit candidate detection, cooldown filtering, and
one-at-a-time selection from ``TriageAuditRunner`` into a focused polling
layer.  It queries for ``in_review`` items, applies store-based cooldown,
selects the oldest eligible candidate, and hands it off to the audit
command handlers via a well-defined protocol.

Work item: SA-0MLYEOG9V107HE1D
"""

from __future__ import annotations

import enum
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

LOG = logging.getLogger("ampa.audit_poller")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class PollerOutcome(enum.Enum):
    """Possible outcomes of a polling cycle."""

    no_candidates = "no_candidates"
    """No work items passed cooldown filtering (or none are in_review)."""

    handed_off = "handed_off"
    """A candidate was selected and handed off to the audit handler."""

    query_failed = "query_failed"
    """The ``wl list`` query failed (non-zero exit code or invalid JSON)."""


@dataclass(frozen=True)
class PollerResult:
    """Structured result returned by the polling cycle.

    Attributes:
        outcome: The outcome of this polling cycle.
        selected_item_id: The work item ID of the selected candidate, or
            ``None`` when no candidate was handed off.
        error: An optional error message when *outcome* is
            ``PollerOutcome.query_failed``.
    """

    outcome: PollerOutcome
    selected_item_id: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Handoff protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditHandoffHandler(Protocol):
    """Protocol for the audit command handler that receives a selected
    candidate from the poller.

    Implementations must define a ``__call__`` method that accepts a single
    work item dict (the shape returned by ``wl list --json``) and returns
    ``True`` on success or ``False`` on failure.

    The work item dict is expected to contain at least the following keys
    (matching the output of ``wl list --json`` / ``wl show <id> --json``):

    - ``id`` (str): The work item identifier.
    - ``title`` (str): Human-readable title.
    - ``status`` (str): Current status (e.g. ``"in-progress"``).
    - ``stage`` (str): Current stage (e.g. ``"in_review"``).
    - ``priority`` (str): Priority label.
    - ``updatedAt`` or ``updated_at`` (str | None): ISO-8601 timestamp of
      the last update, used for candidate ordering.

    Example usage::

        class MyHandler:
            def __call__(self, work_item: Dict[str, Any]) -> bool:
                # execute audit logic
                return True

        handler: AuditHandoffHandler = MyHandler()
        result = poll_and_handoff(..., handler=handler)
    """

    def __call__(self, work_item: Dict[str, Any]) -> bool:
        """Execute the audit for the given *work_item*.

        Args:
            work_item: A dict representing the work item as returned by
                ``wl list --json``.

        Returns:
            ``True`` if the audit completed successfully, ``False``
            otherwise.  The poller does **not** alter its behaviour based
            on this return value (the ``last_audit_at`` timestamp has
            already been persisted before the handler is called), but
            callers may use it for logging or metrics.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Candidate query & normalization
# ---------------------------------------------------------------------------


def _query_candidates(
    run_shell: Callable[..., subprocess.CompletedProcess],
    cwd: str,
    timeout: int = 60,
) -> List[Dict[str, Any]]:
    """Query ``wl list --stage in_review --json`` and return normalised items.

    Handles multiple JSON response shapes:

    - A bare list of work item dicts.
    - A dict wrapping the list under ``workItems``, ``work_items``,
      ``items``, ``data``, or any key ending with ``workitems``
      (case-insensitive).

    Deduplicates items by ID (``id``, ``work_item_id``, or ``work_item``
    key).  Items without a recognisable ID are silently dropped.

    This function never raises.  On any failure (non-zero exit code,
    invalid JSON, unexpected structure) it logs the error and returns an
    empty list.

    Args:
        run_shell: Callable that executes a shell command and returns a
            ``subprocess.CompletedProcess`` instance.
        cwd: Working directory for the shell command.
        timeout: Maximum seconds for the shell command.

    Returns:
        A list of unique work item dicts, each guaranteed to have an
        ``"id"`` key.
    """
    try:
        proc = run_shell(
            "wl list --stage in_review --json",
            cwd=cwd,
            timeout=timeout,
        )
    except Exception:
        LOG.exception("wl list --stage in_review command failed to execute")
        return []

    if proc.returncode != 0:
        LOG.warning(
            "wl list --stage in_review exited with code %s: %s",
            proc.returncode,
            proc.stderr,
        )
        return []

    try:
        raw = json.loads(proc.stdout or "null")
    except Exception:
        LOG.exception("Failed to parse wl list --stage in_review output as JSON")
        return []

    items: List[Dict[str, Any]] = []

    if isinstance(raw, list):
        items.extend(raw)
    elif isinstance(raw, dict):
        for key in ("workItems", "work_items", "items", "data"):
            val = raw.get(key)
            if isinstance(val, list):
                items.extend(val)
                break
        if not items:
            for k, v in raw.items():
                if isinstance(v, list) and k.lower().endswith("workitems"):
                    items.extend(v)
                    break

    # Deduplicate by ID
    unique: Dict[str, Dict[str, Any]] = {}
    for it in items:
        wid = it.get("id") or it.get("work_item_id") or it.get("work_item")
        if not wid:
            continue
        unique[str(wid)] = {**it, "id": wid}

    return list(unique.values())
