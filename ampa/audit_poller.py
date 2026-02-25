"""Audit detection & polling flow.

This module extracts audit candidate detection, cooldown filtering, and
one-at-a-time selection from ``TriageAuditRunner`` into a focused polling
layer.  It queries for ``in_review`` items, applies store-based cooldown,
selects the oldest eligible candidate, and hands it off to the audit
command handlers via a well-defined protocol.

Work item: SA-0MLYEOG9V107HE1D
"""

from __future__ import annotations

import datetime as dt
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


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def _from_iso(value: Optional[str]) -> Optional[dt.datetime]:
    """Parse an ISO-8601 timestamp string, returning ``None`` on failure."""
    if not value:
        return None
    try:
        v = value
        if isinstance(v, str) and v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return dt.datetime.fromisoformat(v)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cooldown filtering
# ---------------------------------------------------------------------------


def _filter_by_cooldown(
    candidates: List[Dict[str, Any]],
    last_audit_by_item: Dict[str, str],
    cooldown_hours: int,
    now: dt.datetime,
) -> List[Dict[str, Any]]:
    """Filter candidates by store-based cooldown.

    For each candidate, look up ``last_audit_at_by_item[item_id]`` from the
    scheduler store state.  If ``(now - last_audit_at) < cooldown_hours`` the
    candidate is skipped.  Items exactly at the cooldown boundary **are**
    eligible (exclusive comparison).  Items with no store entry are always
    eligible.

    Args:
        candidates: Work item dicts as returned by :func:`_query_candidates`.
        last_audit_by_item: Mapping of work item ID to ISO-8601 timestamp
            string, as persisted in the scheduler store under the key
            ``last_audit_at_by_item``.
        cooldown_hours: Minimum hours between audits for the same item.
        now: Current UTC datetime used for comparison.

    Returns:
        A list of candidates that have passed the cooldown check.
    """
    cooldown_delta = dt.timedelta(hours=cooldown_hours)
    eligible: List[Dict[str, Any]] = []

    for item in candidates:
        wid = str(item.get("id", ""))
        if not wid:
            continue

        last_audit_iso = last_audit_by_item.get(wid)
        if last_audit_iso:
            last_audit = _from_iso(last_audit_iso)
            if last_audit is not None and (now - last_audit) < cooldown_delta:
                continue

        eligible.append(item)

    return eligible


# ---------------------------------------------------------------------------
# Candidate selection & sorting
# ---------------------------------------------------------------------------


def _item_updated_ts(item: Dict[str, Any]) -> Optional[dt.datetime]:
    """Extract and parse the ``updated_at`` timestamp from a work item dict.

    Tries multiple key variants used by different ``wl`` output formats.
    Returns ``None`` if no valid timestamp is found.
    """
    for key in (
        "updatedAt",
        "updated_at",
        "last_updated_at",
        "updated_ts",
        "updated",
        "last_update_ts",
    ):
        val = item.get(key)
        if val:
            parsed = _from_iso(val)
            if parsed is not None:
                return parsed
            try:
                return dt.datetime.fromisoformat(val)
            except Exception:
                continue
    return None


def _select_candidate(
    candidates: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Sort candidates by ``updated_at`` ascending and return the oldest.

    Items with no ``updated_at`` timestamp are sorted first (most likely to
    be the oldest).  Returns ``None`` when the candidate list is empty.
    """
    if not candidates:
        return None

    sorted_candidates = sorted(
        candidates,
        key=lambda it: (
            _item_updated_ts(it) is not None,
            _item_updated_ts(it) or dt.datetime.fromtimestamp(0, dt.timezone.utc),
        ),
    )

    return sorted_candidates[0]
