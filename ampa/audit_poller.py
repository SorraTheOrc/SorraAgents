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
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


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
