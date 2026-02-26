"""Dispatch interface — pluggable fire-and-forget agent session spawning.

Defines an abstract ``Dispatcher`` protocol and two implementations:

- ``OpenCodeRunDispatcher``: spawns ``opencode run`` as a detached subprocess.
- ``DryRunDispatcher``: records dispatch calls without spawning (for tests).

Usage::

    from ampa.engine.dispatch import OpenCodeRunDispatcher, DispatchResult

    dispatcher = OpenCodeRunDispatcher(cwd="/path/to/project")
    result = dispatcher.dispatch(
        command='opencode run "/intake WL-123 do not ask questions"',
        work_item_id="WL-123",
    )
    assert result.success
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

LOG = logging.getLogger("ampa.engine.dispatch")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Result of a dispatch attempt.

    Attributes:
        success: Whether the agent session was successfully spawned.
        pid: Process ID of the spawned session (for logging, not waiting).
        error: Error message if spawn failed.
        command: The shell command that was (or would have been) executed.
        work_item_id: The work item ID being dispatched.
        timestamp: When the dispatch occurred (UTC).
        container_id: Optional container identifier when the session was
            dispatched inside a container (e.g. Podman/Distrobox).
    """

    success: bool
    command: str
    work_item_id: str
    timestamp: datetime
    pid: int | None = None
    error: str | None = None
    container_id: str | None = None

    @property
    def summary(self) -> str:
        """One-line human-readable summary."""
        if self.success:
            parts = [f"Dispatched {self.work_item_id} (pid={self.pid}"]
            if self.container_id is not None:
                parts.append(f", container={self.container_id}")
            parts.append(f") at {self.timestamp.isoformat()}")
            return "".join(parts)
        return (
            f"Dispatch failed for {self.work_item_id}: {self.error} "
            f"at {self.timestamp.isoformat()}"
        )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Dispatcher(Protocol):
    """Protocol for fire-and-forget agent session dispatch.

    Implementations must spawn an independent agent session and return
    immediately — they must NOT wait for the session to complete.
    """

    def dispatch(
        self,
        command: str,
        work_item_id: str,
    ) -> DispatchResult:
        """Spawn an independent agent session.

        Args:
            command: The full shell command to execute
                (e.g. ``opencode run "/intake WL-123"``).
            work_item_id: The work item being dispatched (for logging).

        Returns:
            A ``DispatchResult`` indicating spawn success or failure.
        """
        ...


# ---------------------------------------------------------------------------
# OpenCode Run dispatcher (default production implementation)
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return the current UTC time (extracted for testability)."""
    return datetime.now(timezone.utc)


class OpenCodeRunDispatcher:
    """Spawns ``opencode run`` as a detached subprocess.

    The child process is started in a new session (``start_new_session=True``)
    so it survives the engine process exiting.  Stdout and stderr are
    redirected to ``/dev/null`` (or ``NUL`` on Windows) so the engine does not
    block on pipe buffers.

    Args:
        cwd: Working directory for the spawned process.  Defaults to the
            current working directory.
        env: Environment variables for the subprocess.  Defaults to inheriting
            the current environment.
        clock: Callable returning the current UTC datetime (override in tests).
    """

    def __init__(
        self,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        clock: Any = None,
    ) -> None:
        self._cwd = cwd
        self._env = env
        self._clock = clock or _utc_now

    def dispatch(
        self,
        command: str,
        work_item_id: str,
    ) -> DispatchResult:
        """Spawn an independent ``opencode run`` subprocess.

        The process is fully detached:
        - New session via ``start_new_session=True`` (POSIX ``setsid``).
        - Stdout/stderr sent to ``DEVNULL`` so no pipe buffers can block.
        - No waiting — returns immediately after ``Popen`` succeeds.

        Spawn errors (``FileNotFoundError``, ``PermissionError``, ``OSError``,
        etc.) are caught and returned as a failed ``DispatchResult`` rather
        than raised.
        """
        ts = self._clock()
        LOG.info(
            "Dispatching %s: %s (cwd=%s)",
            work_item_id,
            command,
            self._cwd or os.getcwd(),
        )
        try:
            proc = subprocess.Popen(  # noqa: S603 — shell execution is intentional
                command,
                shell=True,  # noqa: S602 — command strings require shell
                cwd=self._cwd,
                env=self._env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            LOG.error("Dispatch spawn failed (file not found): %s", exc)
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"FileNotFoundError: {exc}",
            )
        except PermissionError as exc:
            LOG.error("Dispatch spawn failed (permission denied): %s", exc)
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"PermissionError: {exc}",
            )
        except OSError as exc:
            LOG.error("Dispatch spawn failed (OS error): %s", exc)
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"OSError: {exc}",
            )

        LOG.info(
            "Dispatch successful: %s -> pid %d",
            work_item_id,
            proc.pid,
        )
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            timestamp=ts,
            pid=proc.pid,
        )


# ---------------------------------------------------------------------------
# Dry-run dispatcher (for testing / simulation)
# ---------------------------------------------------------------------------


@dataclass
class DispatchRecord:
    """A recorded dispatch call from ``DryRunDispatcher``."""

    command: str
    work_item_id: str
    timestamp: datetime


class DryRunDispatcher:
    """Records dispatch calls without spawning processes.

    Useful for scheduler simulation mode and unit tests.  Every call to
    ``dispatch()`` appends a ``DispatchRecord`` to the ``calls`` list and
    returns a successful ``DispatchResult`` with a synthetic PID.

    Args:
        clock: Callable returning the current UTC datetime (override in tests).
        fail_on: Optional set of work item IDs that should simulate spawn
            failure.  If the dispatched ``work_item_id`` is in this set,
            ``dispatch()`` returns a failed result.
    """

    def __init__(
        self,
        clock: Any = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self._clock = clock or _utc_now
        self._fail_on = fail_on or set()
        self.calls: list[DispatchRecord] = []
        self._next_pid = 10000

    def dispatch(
        self,
        command: str,
        work_item_id: str,
    ) -> DispatchResult:
        """Record the dispatch call and return a mock result."""
        ts = self._clock()
        self.calls.append(
            DispatchRecord(
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
            )
        )

        if work_item_id in self._fail_on:
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"Simulated spawn failure for {work_item_id}",
            )

        pid = self._next_pid
        self._next_pid += 1
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            timestamp=ts,
            pid=pid,
        )
