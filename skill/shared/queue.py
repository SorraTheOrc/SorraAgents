"""Shared file-based bounded priority queue for cross-process coordination.

Bounded priority queue backed by ``fcntl.flock`` directory locking.
Provides priority-ordered, FIFO-within-tier enqueue/dequeue with bounded
capacity and crash-safe semantics (kernel flock guarantees no stale locks).

Design
------
A shared directory where each waiter writes a JSON file
``{item_id}.json`` containing ``{"priority": <int>, "timestamp": <float>,
"item_id": <str>}``.  Directory listing sorted by (priority, timestamp)
gives dequeue order.  Flock-based locking of the directory ensures no stale
locks and cross-process safety.

Configuration
-------------
- ``max_depth`` (explicit argument) — highest priority.
- ``QUEUE_MAX_DEPTH`` environment variable — override (default ceiling
  documented below).
- ``QUEUE_TTL_SECONDS`` environment variable — orphan-entry TTL override.
- Default capacity: 50 entries; default TTL: 86400s (24 hours).

Usage
-----
.. code-block:: python

    from shared.queue import PriorityQueue, Priority

    pq = PriorityQueue("audit", max_depth=50, timeout=30)
    pq.enqueue(item_id, Priority.CRITICAL)
    entry = pq.dequeue(timeout=10)  # blocks until item available

The ``enqueue`` call blocks (or times out) when the queue is full.
The ``dequeue`` call blocks (or times out) when the queue is empty.
TTL-based cleanup prunes stale entries automatically on read.

Priority levels
---------------
- ``CRITICAL`` (0) — highest urgency
- ``HIGH`` (1)
- ``MEDIUM`` (2)
- ``LOW`` (3) — lowest urgency

Within the same priority level, items are served in FIFO (age) order.

API
---
.. code-block:: python

    from shared.queue import PriorityQueue, Priority

    pq = PriorityQueue("myqueue")
    pq.enqueue("job-1", Priority.HIGH)
    entry = pq.dequeue(timeout=5)
    if entry:
        print(entry.item_id)  # "job-1"
    pq.close()  # optional; directory locks released on GC

.. versionadded:: 0.1.0 (SA-0MTG5N7BN003D95L)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

if sys.version_info >= (3, 11):
    from typing import Self
else:
    Self = Any  # pragma: no cover

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_DEPTH = 50
DEFAULT_TTL_SECONDS = 86400  # 24 hours
ENV_LOCK_DIR = "PI_SEMAPHORE_DIR"  # shared with process_semaphore
ENV_MAX_DEPTH = "QUEUE_MAX_DEPTH"
ENV_TTL_SECONDS = "QUEUE_TTL_SECONDS"
_RETRY_DELAY_SECONDS = 0.05
_QUEUE_FILE_EXT = ".json"

_NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Enums / dataclasses
# ---------------------------------------------------------------------------


class Priority:
    """Discrete priority levels for queue ordering.

    Numeric values follow the convention that **lower numbers = higher
    urgency**.  Sorting by ``(priority_value, timestamp)`` yields the
    correct dequeue order.
    """

    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3

    _REVERSE: ClassVar[dict[int, str]] = {
        0: "CRITICAL",
        1: "HIGH",
        2: "MEDIUM",
        3: "LOW",
    }

    @classmethod
    def to_str(cls, value: int) -> str:
        return cls._REVERSE.get(value, f"UNKNOWN({value})")

    @classmethod
    def from_str(cls, name: str) -> int:
        mapping = {v: k for k, v in cls._REVERSE.items()}
        val = mapping.get(name.upper())
        if val is None:
            raise ValueError(f"Unknown priority: {name!r}")
        return val


# Keep backward-compatible alias for callers that use int values
PRIORITY_MAP = {
    Priority.CRITICAL: 0,
    Priority.HIGH: 1,
    Priority.MEDIUM: 2,
    Priority.LOW: 3,
}


@dataclass
class QueueEntry:
    """A single item in the priority queue."""

    item_id: str
    priority: int  # numeric priority value
    timestamp: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "priority": self.priority,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueEntry:
        return cls(
            item_id=data["item_id"],
            priority=data["priority"],
            timestamp=data["timestamp"],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_lock_dir() -> Path:
    """Resolve the base lock directory (configurable via env for tests)."""
    override = os.environ.get(ENV_LOCK_DIR)
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    return base / "pi-semaphores"


def _sanitize_name(name: str) -> str:
    """Make a queue name safe for use as a directory name."""
    cleaned = _NAME_SAFE.sub("_", name).strip("_")
    return cleaned or "default"


def _queue_file(item_id: str) -> str:
    """Return the hash-derived filename for a queue entry.

    The real ``item_id`` is stored inside the JSON payload; the filename is
    a truncated sha256 so arbitrary caller ids (which may contain path
    separators or other special characters) can never escape the queue
    directory or collide with control files.
    """
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return f"{digest[:16]}{_QUEUE_FILE_EXT}"


# ---------------------------------------------------------------------------
# PriorityQueue
# ---------------------------------------------------------------------------


class PriorityQueue:
    """A file-based, cross-process bounded priority queue.

    Items are stored as JSON files in a shared directory.  Dequeue sorts
    entries by ``(priority, timestamp)`` to produce FIFO-within-tier
    priority ordering.  Flock-based locking of the directory ensures
    cross-process safety with no stale locks on crash.

    Args:
        name: Logical name of the queue (shared across processes).
        max_depth: Maximum number of items allowed in the queue.  Resolved
            as: explicit argument > ``QUEUE_MAX_DEPTH`` env var > default (50).
        timeout: Default wait time in seconds for ``enqueue`` (when full)
            and ``dequeue`` (when empty).  ``None`` = block indefinitely,
            ``0`` = fail fast.
        ttl_seconds: Seconds after which an enqueued item is considered
            stale and pruned.  Resolved as: explicit argument >
            ``QUEUE_TTL_SECONDS`` env var > default (86400s / 24h).

    Raises:
        RuntimeError: If ``fcntl`` is not available (non-POSIX platform).
        ValueError: If ``max_depth`` < 1.
    """

    def __init__(
        self,
        name: str,
        max_depth: int | None = None,
        timeout: float | None = 30.0,
        ttl_seconds: float | None = None,
    ) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX
            raise RuntimeError("PriorityQueue requires fcntl (POSIX)")

        self.name = _sanitize_name(name)
        self._queue_dir = _default_lock_dir() / self.name
        self.timeout = timeout
        self._lock_fd: int | None = None

        # Resolve max_depth
        if max_depth is not None:
            self.max_depth = int(max_depth)
        else:
            env_val = os.environ.get(ENV_MAX_DEPTH)
            self.max_depth = int(env_val) if env_val else DEFAULT_MAX_DEPTH
        if self.max_depth < 1:
            raise ValueError("max_depth must be >= 1")

        # Resolve TTL
        if ttl_seconds is not None:
            self.ttl_seconds = float(ttl_seconds)
        else:
            env_val = os.environ.get(ENV_TTL_SECONDS)
            self.ttl_seconds = float(env_val) if env_val else DEFAULT_TTL_SECONDS

        # Ensure the directory exists
        self._queue_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Directory locking (cross-process safety)
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> None:
        """Take an exclusive flock on the queue directory.

        The lock is held for the duration of operations that mutate the
        directory (enqueue/dequeue).  flock guarantees are released on
        process exit, so no stale locks after a crash.
        """
        lock_path = self._queue_dir / ".lock"
        lock_path.touch(exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self._lock_fd = fd

    def _release_lock(self) -> None:
        """Release the directory lock.  Idempotent."""
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(self._lock_fd)
            finally:
                self._lock_fd = None

    # ------------------------------------------------------------------
    # TTL / Stale entry cleanup
    # ------------------------------------------------------------------

    def _prune_stale(self) -> None:
        """Remove entries older than ``ttl_seconds`` from the queue."""
        now = time.monotonic()
        for entry_file in self._queue_dir.glob(f"*{_QUEUE_FILE_EXT}"):
            try:
                with open(entry_file, "r") as f:
                    data = json.load(f)
                timestamp = data.get("timestamp", 0)
                if now - timestamp > self.ttl_seconds:
                    entry_file.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError, KeyError):
                # Corrupt entry — treat as stale and remove
                entry_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # List / sort entries
    # ------------------------------------------------------------------

    def _list_entries(self) -> list[QueueEntry]:
        """Return all valid (non-stale) entries sorted by (priority, timestamp)."""
        self._prune_stale()
        entries: list[QueueEntry] = []
        for entry_file in self._queue_dir.glob(f"*{_QUEUE_FILE_EXT}"):
            try:
                with open(entry_file, "r") as f:
                    data = json.load(f)
                entry = QueueEntry.from_dict(data)
                entries.append(entry)
            except (json.JSONDecodeError, OSError, KeyError):
                # Corrupt entry — remove silently
                entry_file.unlink(missing_ok=True)

        # Sort by (priority, timestamp) — lower priority value first,
        # then earlier timestamp first
        entries.sort(key=lambda e: (e.priority, e.timestamp))
        return entries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Return the current number of items in the queue."""
        entries = self._list_entries()
        return len(entries)

    def enqueue(
        self,
        item_id: str,
        priority: int,
        timeout: float | None = None,
        timestamp: float | None = None,
    ) -> QueueEntry:
        """Add an item to the queue, waiting if at capacity.

        Args:
            item_id: Unique identifier for this queue item.
            priority: Numeric priority value (lower = more urgent).
                Use ``Priority.CRITICAL``, ``Priority.HIGH``,
                ``Priority.MEDIUM``, or ``Priority.LOW``.
            timeout: Override the instance timeout.  ``None`` = use
                instance default, ``0`` = fail fast.
            timestamp: Override the entry timestamp (for testing).

        Returns:
            The ``QueueEntry`` that was enqueued.

        Raises:
            TimeoutError: If the queue is full and ``timeout`` expires.
        """
        effective_timeout = self.timeout if timeout is None else timeout

        deadline = None
        if effective_timeout is not None:
            # Even timeout=0 gets an immediate deadline; fail-fast raise below.
            deadline = time.monotonic() + float(effective_timeout)

        while True:
            with self._locked():
                entries = self._list_entries()

                # Check if item already exists (idempotent enqueue)
                for entry in entries:
                    if entry.item_id == item_id:
                        return entry

                if len(entries) >= self.max_depth:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"queue '{self.name}' full: capacity {self.max_depth} "
                            f"reached (max_depth={self.max_depth})"
                        )
                    # Release the lock before waiting so a concurrent
                    # dequeue can free a slot; retry after a short delay.
                else:
                    # Capacity available — write the entry and return.
                    ts = timestamp if timestamp is not None else time.monotonic()
                    entry = QueueEntry(
                        item_id=item_id,
                        priority=priority,
                        timestamp=ts,
                    )
                    entry_file = self._queue_dir / _queue_file(item_id)
                    with open(entry_file, "w") as f:
                        json.dump(entry.to_dict(), f)
                    return entry

            # Queue was full — wait briefly outside the lock and retry.
            time.sleep(_RETRY_DELAY_SECONDS)

    def dequeue(self, timeout: float | None = None) -> QueueEntry | None:
        """Remove and return the highest-priority item from the queue.

        Items are dequeued in priority order (lower numeric value first),
        with FIFO ordering within each priority tier.

        Args:
            timeout: Override the instance timeout.  ``None`` = use
                instance default, ``0`` = return immediately if empty.

        Returns:
            The ``QueueEntry`` if one was available, or ``None`` if the
            queue was empty when ``timeout`` expired.

        Notes:
            An empty-queue timeout is a benign "nothing to do" condition
            and returns ``None`` (never raises).  This distinguishes it
            from ``enqueue``-to-full saturation, which raises
            ``TimeoutError``.
        """
        effective_timeout = self.timeout if timeout is None else timeout

        # Fail fast (timeout <= 0): check once, return None if empty.
        if effective_timeout is not None and float(effective_timeout) <= 0:
            with self._locked():
                entries = self._list_entries()
                if entries:
                    winner = entries[0]
                    entry_file = self._queue_dir / _queue_file(winner.item_id)
                    try:
                        entry_file.unlink(missing_ok=True)
                    except OSError:
                        pass  # race: another process grabbed it
                    return winner
            return None

        deadline = (
            None
            if effective_timeout is None
            else time.monotonic() + float(effective_timeout)
        )

        while True:
            with self._locked():
                entries = self._list_entries()
                if entries:
                    # Remove the first entry (highest priority, earliest)
                    winner = entries[0]
                    entry_file = self._queue_dir / _queue_file(winner.item_id)
                    try:
                        entry_file.unlink(missing_ok=True)
                    except OSError:
                        pass  # race: another process grabbed it
                    return winner

            # Queue is empty — wait a little, then return None on deadline
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(_RETRY_DELAY_SECONDS)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Self:
        self._acquire_lock()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self._release_lock()

    # ------------------------------------------------------------------
    # Internal: locked context manager
    # ------------------------------------------------------------------

    def _locked(self) -> Any:
        """Context manager that holds the directory lock for the block."""
        if self._lock_fd is not None:
            # Already locked by an outer context — no-op
            class _NoLock:
                def __enter__(self__self__):
                    pass

                def __exit__(self__self__, *a):
                    pass

            return _NoLock()
        return _DirectoryLock(self)

    def close(self) -> None:
        """Release the lock and clean up.  Safe to call multiple times."""
        self._release_lock()


class _DirectoryLock:
    """Private context manager for directory flock."""

    __slots__ = ("_pq",)

    def __init__(self, pq: PriorityQueue) -> None:
        self._pq = pq

    def __enter__(self) -> Self:
        self._pq._acquire_lock()
        return self

    def __exit__(self, *a) -> None:
        self._pq._release_lock()

    def __del__(self) -> None:
        self._pq._release_lock()
