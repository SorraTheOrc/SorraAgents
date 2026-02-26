"""Dispatch interface — pluggable fire-and-forget agent session spawning.

Defines an abstract ``Dispatcher`` protocol and three implementations:

- ``OpenCodeRunDispatcher``: spawns ``opencode run`` as a detached subprocess.
- ``ContainerDispatcher``: acquires a pool container and spawns ``opencode run``
  inside it via ``distrobox enter``.
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

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
# Container dispatcher (pool-based Podman/Distrobox dispatch)
# ---------------------------------------------------------------------------

# Default timeout (seconds) for the distrobox-enter subprocess.
_DEFAULT_CONTAINER_DISPATCH_TIMEOUT = 30


def _global_ampa_dir() -> Path:
    """Return the global AMPA state directory.

    Mirrors the JS ``globalAmpaDir()`` in ampa.mjs:
    ``$XDG_CONFIG_HOME/opencode/.worklog/ampa`` (falls back to
    ``~/.config/opencode/.worklog/ampa``).
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "opencode" / ".worklog" / "ampa"


def _pool_state_path() -> Path:
    """Return the path to the pool state file."""
    return _global_ampa_dir() / "pool-state.json"


def _read_pool_state() -> dict[str, Any]:
    """Read the pool state from disk.

    Returns an empty dict when the file doesn't exist or is invalid.
    """
    p = _pool_state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_pool_state(state: dict[str, Any]) -> None:
    """Persist the pool state to disk (atomic-ish write)."""
    p = _pool_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


_POOL_PREFIX = "ampa-pool-"
_POOL_SIZE = 3
_POOL_MAX_INDEX = _POOL_SIZE * 3  # 9


def _existing_pool_containers() -> set[str]:
    """Return the set of pool container names that currently exist in Podman.

    Uses ``podman ps -a`` with a name filter — mirrors the JS helper.
    """
    try:
        result = subprocess.run(  # noqa: S603, S607
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                f"name={_POOL_PREFIX}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return set()
        return {n for n in result.stdout.strip().split("\n") if n}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return set()


def _list_available_pool() -> list[str]:
    """List pool containers that exist in Podman and are NOT claimed.

    Mirrors the JS ``listAvailablePool`` function.
    """
    state = _read_pool_state()
    existing = _existing_pool_containers()
    available: list[str] = []
    for i in range(_POOL_MAX_INDEX):
        name = f"{_POOL_PREFIX}{i}"
        if name in existing and name not in state:
            available.append(name)
    return available


def _claim_pool_container(
    work_item_id: str,
    branch: str,
) -> str | None:
    """Claim a pool container for *work_item_id*.

    Writes the claim to ``pool-state.json`` and returns the container name,
    or ``None`` when no pool containers are available.
    """
    available = _list_available_pool()
    if not available:
        return None
    name = available[0]
    state = _read_pool_state()
    state[name] = {
        "workItemId": work_item_id,
        "branch": branch,
        "claimedAt": datetime.now(timezone.utc).isoformat(),
    }
    _save_pool_state(state)
    return name


def _release_pool_container(container_name: str) -> None:
    """Release the claim on *container_name* in ``pool-state.json``."""
    state = _read_pool_state()
    state.pop(container_name, None)
    _save_pool_state(state)


class ContainerDispatcher:
    """Acquires a pool container and spawns ``opencode run`` inside it.

    The dispatcher:

    1. Claims an available pool container from ``pool-state.json``.
    2. Launches ``distrobox enter <container> -- opencode run "<prompt>"`` as a
       detached subprocess (new session, DEVNULL stdio).
    3. Returns a ``DispatchResult`` with ``container_id`` set to the container
       name and ``pid`` set to the child process PID.

    On failure the pool claim is released before returning a failed result.

    Args:
        project_root: Project root directory passed into the container via the
            ``AMPA_PROJECT_ROOT`` environment variable.
        branch: Git branch name written into the pool claim record.
        env: Extra environment variables for the subprocess.  The container
            environment variables (``AMPA_CONTAINER_NAME``, etc.) are merged
            on top.
        clock: Callable returning the current UTC datetime (override in tests).
        timeout: Subprocess timeout in seconds.  Overridden by the
            ``AMPA_CONTAINER_DISPATCH_TIMEOUT`` environment variable.
    """

    def __init__(
        self,
        project_root: str | None = None,
        branch: str = "",
        env: dict[str, str] | None = None,
        clock: Any = None,
        timeout: int | None = None,
    ) -> None:
        self._project_root = project_root or os.getcwd()
        self._branch = branch
        self._env = env
        self._clock = clock or _utc_now
        # Timeout: env-var > constructor arg > default.
        env_timeout = os.environ.get("AMPA_CONTAINER_DISPATCH_TIMEOUT")
        if env_timeout is not None:
            try:
                self._timeout = int(env_timeout)
            except ValueError:
                self._timeout = timeout or _DEFAULT_CONTAINER_DISPATCH_TIMEOUT
        else:
            self._timeout = timeout or _DEFAULT_CONTAINER_DISPATCH_TIMEOUT

    # -- Pool helpers (thin wrappers so they can be patched in tests) -------

    @staticmethod
    def _list_available() -> list[str]:
        return _list_available_pool()

    @staticmethod
    def _claim(work_item_id: str, branch: str) -> str | None:
        return _claim_pool_container(work_item_id, branch)

    @staticmethod
    def _release(container_name: str) -> None:
        _release_pool_container(container_name)

    # -- Dispatch -----------------------------------------------------------

    def dispatch(
        self,
        command: str,
        work_item_id: str,
    ) -> DispatchResult:
        """Acquire a container and spawn the agent session inside it.

        The command is wrapped as::

            distrobox enter <container> -- <command>

        The child process inherits the current environment, extended with
        container-specific variables (``AMPA_CONTAINER_NAME``,
        ``AMPA_WORK_ITEM_ID``, ``AMPA_BRANCH``, ``AMPA_PROJECT_ROOT``).
        """
        ts = self._clock()

        # 1. Acquire a pool container ------------------------------------
        container_name = self._claim(work_item_id, self._branch)
        if container_name is None:
            LOG.warning("No pool containers available for %s", work_item_id)
            return DispatchResult(
                success=False,
                command=command,
                work_item_id=work_item_id,
                timestamp=ts,
                error="No pool containers available",
            )

        LOG.info("Claimed container %s for %s", container_name, work_item_id)

        # 2. Build the distrobox command ---------------------------------
        distrobox_cmd = f"distrobox enter {container_name} -- {command}"

        # Merge container env vars on top of caller-supplied / inherited env.
        spawn_env = dict(self._env) if self._env else dict(os.environ)
        spawn_env.update(
            {
                "AMPA_CONTAINER_NAME": container_name,
                "AMPA_WORK_ITEM_ID": work_item_id,
                "AMPA_BRANCH": self._branch,
                "AMPA_PROJECT_ROOT": self._project_root,
            }
        )

        # 3. Spawn the detached subprocess --------------------------------
        try:
            proc = subprocess.Popen(  # noqa: S603
                distrobox_cmd,
                shell=True,  # noqa: S602
                cwd=self._project_root,
                env=spawn_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            LOG.error(
                "Container dispatch spawn failed for %s (%s): %s",
                work_item_id,
                container_name,
                exc,
            )
            # Release the claim so the container goes back to the pool.
            self._release(container_name)
            return DispatchResult(
                success=False,
                command=distrobox_cmd,
                work_item_id=work_item_id,
                timestamp=ts,
                error=f"{type(exc).__name__}: {exc}",
                container_id=container_name,
            )

        LOG.info(
            "Container dispatch successful: %s -> container=%s pid=%d",
            work_item_id,
            container_name,
            proc.pid,
        )
        return DispatchResult(
            success=True,
            command=distrobox_cmd,
            work_item_id=work_item_id,
            timestamp=ts,
            pid=proc.pid,
            container_id=container_name,
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
