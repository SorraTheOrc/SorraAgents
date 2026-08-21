"""Shared cross-process counting semaphore backed by ``fcntl.flock``.

Bounds the number of concurrent heavy processes (pi/audit invocations,
`wl sync`, vitest runs) across independent processes and sessions using
advisory file locks — see fan-out investigation SA-0MSAEKOQE009TEB4.

Design
------
A counting semaphore is modelled as a **pool of slot lock files** in a
dedicated lock directory. Each slot is a regular file; a process acquires a
slot by taking an exclusive advisory ``flock`` on it (non-blocking). Because
``flock`` locks are released automatically by the kernel when the holding
file descriptor is closed — including on process exit, ``SIGKILL``, error,
or crash — there are **no stale locks** and no PID bookkeeping.

Configuration
-------------
- ``max_workers`` (explicit argument) — highest priority.
- ``AUDIT_MAX_CONCURRENCY`` environment variable — override for audit-style
  workloads (default ceiling documented below).
- Default: 5 concurrent workers when neither is provided.

Timeout semantics
-----------------
- ``timeout=None`` — block indefinitely until a slot frees.
- ``timeout=0`` — fail fast: raise ``TimeoutError`` immediately if full.
- ``timeout>0`` — bounded wait; raise ``TimeoutError`` at the deadline.

Usage
-----
.. code-block:: python

    from shared.process_semaphore import Semaphore

    with Semaphore("audit", max_workers=2, timeout=30):
        run_heavy_work()

The context manager releases the slot on normal exit **and** on exception.
"""

from __future__ import annotations

import errno
import os
import re
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    from typing import Self
else:
    Self = Any  # pragma: no cover

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

DEFAULT_MAX_WORKERS = 5
ENV_MAX_WORKERS = "AUDIT_MAX_CONCURRENCY"
ENV_LOCK_DIR = "PI_SEMAPHORE_DIR"
_RETRY_DELAY_SECONDS = 0.05

_NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _default_lock_dir() -> Path:
    """Resolve the base lock directory (configurable via env for tests)."""
    override = os.environ.get(ENV_LOCK_DIR)
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    return base / "pi-semaphores"


def _sanitize_name(name: str) -> str:
    """Make a semaphore name safe for use as a directory name."""
    cleaned = _NAME_SAFE.sub("_", name).strip("_")
    return cleaned or "default"


class Semaphore:
    """A flock-based counting semaphore usable across processes.

    Args:
        name: Logical name of the semaphore (shared across processes).
        max_workers: Maximum concurrent holders. Resolved as: explicit
            argument > ``AUDIT_MAX_CONCURRENCY`` env var > default (2).
        timeout: Bounded wait in seconds (``None`` = block indefinitely,
            ``0`` = fail fast). Default 30.
    """

    def __init__(
        self,
        name: str,
        max_workers: int | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX
            raise RuntimeError("process_semaphore requires fcntl (POSIX)")

        self.name = name
        self._slot_dir = _default_lock_dir() / _sanitize_name(name)
        self._held_fd: int | None = None
        self.timeout = timeout

        if max_workers is not None:
            self.max_workers = int(max_workers)
        else:
            env_val = os.environ.get(ENV_MAX_WORKERS)
            self.max_workers = int(env_val) if env_val else DEFAULT_MAX_WORKERS
        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")

    # ------------------------------------------------------------------
    # Slot management
    # ------------------------------------------------------------------

    def _slot_path(self, index: int) -> Path:
        return self._slot_dir / str(index)

    def _ensure_slots(self) -> None:
        """Create slot lock files up to ``max_workers`` (idempotent)."""
        self._slot_dir.mkdir(parents=True, exist_ok=True)
        for i in range(self.max_workers):
            path = self._slot_path(i)
            try:
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
                os.close(fd)
            except OSError:  # pragma: no cover - race with another creator
                pass

    def _try_acquire_slot(self) -> int | None:
        """Try to flock one free slot; return its fd, or None if all busy."""
        for i in range(self.max_workers):
            path = self._slot_path(i)
            try:
                fd = os.open(path, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    continue  # slot busy; try next
                raise
            return fd
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, timeout: float | None | Any = None) -> bool:
        """Acquire a slot, waiting up to *timeout* seconds.

        Args:
            timeout: Override the instance timeout (``None`` = block
                indefinitely, ``0`` = fail fast).

        Returns:
            True on success.

        Raises:
            TimeoutError: If no slot frees within the deadline.
        """
        if self._held_fd is not None:
            return True  # already held (re-entrant no-op)

        effective_timeout = self.timeout if timeout is None else timeout
        self._ensure_slots()

        deadline = (
            None
            if effective_timeout is None
            else time.monotonic() + float(effective_timeout)
        )

        while True:
            fd = self._try_acquire_slot()
            if fd is not None:
                self._held_fd = fd
                return True

            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"semaphore '{self.name}' busy: no slot free within "
                    f"{effective_timeout}s (max_workers={self.max_workers})"
                )
            time.sleep(_RETRY_DELAY_SECONDS)

    def release(self) -> None:
        """Release the held slot. Idempotent (safe to call after failure)."""
        fd = self._held_fd
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(fd)
            finally:
                self._held_fd = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.release()
