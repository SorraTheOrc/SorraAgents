#!/usr/bin/env python3
"""Shared status lifecycle helper for skills.

Provides a reusable context manager that manages work-item status and stage
transitions consistently across all skills (audit, implement, plan, etc.).

Usage::

    from skill.shared.status_lifecycle import StatusLifecycle

    with StatusLifecycle(
        work_item_id,
        assignee="agent_name",
        target_stage="in_review",
    ) as ctx:
        # ... do work ...
        # On normal exit: status → completed
        # On exception: status → original value (rollback)

The helper is idempotent — safe to call when the work item is already in the
target state. It logs all transitions and raises exceptions on ``wl`` command
failures so callers can handle them explicitly.

For testing or custom runners, an injectable ``runner`` callable can be passed::

    def my_runner(cmd: list[str]) -> subprocess.CompletedProcess:
        ...

    with StatusLifecycle(id, runner=my_runner):
        ...
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Callable, Optional

LOG = logging.getLogger("skill.shared.status_lifecycle")

# Type alias for an injectable command runner.
# Takes a command list, returns a CompletedProcess (like subprocess.run).
Runner = Callable[[list[str]], subprocess.CompletedProcess]


# ======================================================================
# Default runner
# ======================================================================


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Default command runner using ``subprocess.run``."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_wl_with_runner(runner: Runner, cmd: list[str]) -> dict:
    """Run a ``wl`` command via an injectable runner and return parsed JSON.

    Args:
        runner: A callable that takes a command list and returns a CompletedProcess.
        cmd: The command as a list of strings.

    Returns:
        The parsed JSON response dict.

    Raises:
        RuntimeError: If the command fails or returns invalid JSON.
    """
    LOG.debug("Running: %s", " ".join(cmd))
    proc = runner(cmd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed ({' '.join(cmd)}): {proc.stderr.strip()}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from wl: {exc}") from exc
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            f"Worklog command failed: {data.get('error', 'unknown error')}"
        )
    return data


# Backwards-compatible convenience wrapper using the default runner
def _run_wl(cmd: list[str]) -> dict:
    """Run a ``wl`` command using the default subprocess runner.

    See :func:`_run_wl_with_runner` for details.
    """
    return _run_wl_with_runner(_default_runner, cmd)


# ======================================================================
# StatusLifecycle context manager
# ======================================================================


class StatusLifecycle:
    """Context manager for work-item status lifecycle management.

    Captures original status on entry, sets ``in_progress``, and:

    - **Normal exit:** transitions to ``completed`` (optionally advancing stage)
    - **Exception exit:** restores original status (rollback)
    - **Idempotent:** safe to call when work item is already in the target state

    Args:
        work_item_id: The work item ID (e.g. ``SA-XXXX``).
        assignee: Optional assignee name. Set on entry; cleared on failure exit.
        target_stage: Optional stage value (e.g. ``in_review``) to set
            on successful exit.
        runner: Optional injectable command runner for testing.
            Must have signature ``(cmd: list[str]) -> subprocess.CompletedProcess``.

    Raises:
        RuntimeError: If a ``wl`` command fails. Callers can catch and handle.

    Example::

        with StatusLifecycle("SA-0ABC123"):
            run_audit()

        # Status is now ``completed``

        with StatusLifecycle("SA-0ABC123", target_stage="in_review"):
            implement()

        # Status is now ``completed``, stage is ``in_review``
    """

    def __init__(
        self,
        work_item_id: str,
        *,
        assignee: Optional[str] = None,
        target_stage: Optional[str] = None,
        runner: Optional[Runner] = None,
    ) -> None:
        self._work_item_id = work_item_id
        self._assignee = assignee
        self._target_stage = target_stage
        self._runner = runner or _default_runner
        self._original_status: str = "open"  # safe default
        self._did_set_in_progress: bool = False

    # ------------------------------------------------------------------
    # Public helpers (usable outside context manager too)
    # ------------------------------------------------------------------

    @staticmethod
    def show(work_item_id: str, runner: Optional[Runner] = None) -> dict:
        """Fetch a work item via ``wl show`` and return the parsed JSON.

        Args:
            work_item_id: The work item ID.
            runner: Optional injectable runner for testing.

        Returns:
            The parsed JSON dict from ``wl show``.

        Raises:
            RuntimeError: If the ``wl`` command fails.
        """
        r = runner or _default_runner
        return _run_wl_with_runner(r, ["wl", "show", work_item_id, "--json"])

    @staticmethod
    def update_status(
        work_item_id: str,
        status: str,
        stage: Optional[str] = None,
        assignee: Optional[str] = None,
        runner: Optional[Runner] = None,
    ) -> dict:
        """Update a work item's status (and optionally stage/assignee).

        Args:
            work_item_id: The work item ID.
            status: New status value (e.g. ``open``, ``in_progress``, ``completed``).
            stage: Optional new stage value.
            assignee: Optional new assignee value.
            runner: Optional injectable runner for testing.

        Returns:
            The parsed JSON dict from ``wl update``.

        Raises:
            RuntimeError: If the ``wl`` command fails.
        """
        cmd = ["wl", "update", work_item_id, "--status", status, "--json"]
        if stage is not None:
            cmd.extend(["--stage", stage])
        if assignee is not None:
            cmd.extend(["--assignee", assignee])
        r = runner or _default_runner
        return _run_wl_with_runner(r, cmd)

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "StatusLifecycle":
        """Capture original status, set ``in_progress`` (and optional assignee)."""
        # Capture original status
        try:
            data = self.show(self._work_item_id, runner=self._runner)
            wi = data.get("workItem", {})
            self._original_status = wi.get("status", "open")
            LOG.info(
                "Captured original status for %s: %s",
                self._work_item_id,
                self._original_status,
            )
        except RuntimeError:
            LOG.warning(
                "Could not fetch work item %s; defaulting original status to 'open'",
                self._work_item_id,
            )
            self._original_status = "open"

        # Set in_progress
        try:
            kwargs: dict = {"status": "in_progress", "runner": self._runner}
            if self._assignee is not None:
                kwargs["assignee"] = self._assignee
            self.update_status(self._work_item_id, **kwargs)
            self._did_set_in_progress = True
            LOG.info(
                "Set status=in_progress for %s (original: %s)",
                self._work_item_id,
                self._original_status,
            )
        except RuntimeError:
            LOG.error(
                "Failed to set in_progress for %s",
                self._work_item_id,
            )
            raise

        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> Optional[bool]:
        """On success: set completed. On exception: restore original status."""
        if exc_type is not None:
            # Exception path — restore original status
            self._restore_original()
            # Do NOT suppress the exception
            return None

        # Success path — transition to completed
        try:
            kwargs: dict = {"status": "completed", "runner": self._runner}
            if self._target_stage is not None:
                kwargs["stage"] = self._target_stage
            self.update_status(self._work_item_id, **kwargs)
            LOG.info(
                "Set status=completed for %s (stage=%s)",
                self._work_item_id,
                self._target_stage or "unchanged",
            )
        except RuntimeError:
            LOG.error(
                "Failed to set completed for %s",
                self._work_item_id,
            )
            raise

        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _restore_original(self) -> None:
        """Restore the original status (and clear assignee on failure)."""
        if not self._did_set_in_progress:
            return  # Nothing was changed, nothing to restore

        try:
            kwargs: dict = {"status": self._original_status, "runner": self._runner}
            # Clear assignee on failure to release the item
            if self._assignee is not None:
                kwargs["assignee"] = ""
            self.update_status(self._work_item_id, **kwargs)
            extra = ", assignee cleared" if self._assignee is not None else ""
            LOG.info(
                "Restored status=%s for %s (failure exit%s)",
                self._original_status,
                self._work_item_id,
                extra,
            )
        except RuntimeError:
            LOG.warning(
                "Failed to restore status=%s for %s (suppressed)",
                self._original_status,
                self._work_item_id,
            )
