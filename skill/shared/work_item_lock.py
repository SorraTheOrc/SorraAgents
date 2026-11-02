"""Shared per-work-item exclusive lock backed by ``fcntl.flock``.

Serialises the critical section of one work item — work-item claim plus
worktree creation/resume (:func:`implement.phase_start`) — across independent
``implement.py`` processes (``drive``, ``parent`` and manual ``start``). It
exists because two concurrent ``drive`` invocations could previously spawn two
child sessions for the **same** child in the **same** worktree; the two
sessions then wrote the same files concurrently and one removed the worktree
under the other (SA-0MVOABYW00004978).

Design
------
Each work item gets one regular lock file at
``<worklog>/locks/child-<work-item-id>.lock``. A holder takes a **non-blocking**
exclusive advisory ``flock`` on it for the lifetime of its critical section:

- The kernel is authoritative: ``flock`` is released automatically when the
  holding file descriptor is closed — on normal exit, on exception, on
  ``SIGINT``/``SIGTERM`` and on ``SIGKILL``/crash. There is no wall-clock TTL
  that could expire under a legitimately long child session, and no polling.
- The lock file additionally records the holder's metadata (``agent``,
  ``pid``, ``session_id``, ``started_at``, ``token``). It is diagnostic: a
  blocked acquirer reports it as ``held_by``, and a recorded owner whose pid is
  dead is **reclaimed** at acquisition with a logged warning naming the holder
  (the kernel already released the lock; only the stale metadata needs
  replacing).
- Ownership is never inferred from pid equality. A holder chain (driver →
  ``parent``/``start`` subprocesses → driven child session) propagates one
  explicit token through :data:`ENV_LOCK_TOKEN`; an acquire whose token matches
  the lock file's token is a **re-entrant no-op** and is never falsely blocked.

Fail closed
-----------
Any ambiguous state — unreadable/corrupt lock metadata, an unexpected lock
state — yields a *held* outcome (:class:`LockResult` with ``acquired=False``)
and is never a second concurrent writer. A corrupt lock file whose owner
leaves no live holder therefore requires operator attention; the warning
names the file so it can be inspected and, only when no live holder exists,
removed.

Usage
-----
.. code-block:: python

    from shared.work_item_lock import ENV_LOCK_TOKEN, WorkItemLock

    lock = WorkItemLock("SA-0MVOABYW00004978", agent="implement", session_id=sid)
    result = lock.acquire()
    if not result.acquired:
        print(f"held by {result.held_by}")
    else:
        try:
            ...  # claim + worktree create/resume
        finally:
            lock.release()

    # Prefer the context manager: it releases on normal exit and on exception.
    # On contention it raises WorkItemLockHeld (carrying .result) instead of
    # entering the block.
    try:
        with WorkItemLock(item_id, agent="implement", session_id=sid) as held:
            ...
    except WorkItemLockHeld as exc:
        print(f"held by {exc.result.held_by}")

The driver exports ``lock_result.token`` as ``IMPLEMENT_CHILD_LOCK_TOKEN`` in
the spawned child environment so the driven child session re-enters its own
lock instead of blocking on it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import types
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    from typing import Self
else:  # pragma: no cover - older Python
    Self = Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

LOG = logging.getLogger(__name__)

#: Environment variable carrying the lock ownership token along the holder
#: chain (driver → ``parent``/``start`` subprocesses → driven child session).
ENV_LOCK_TOKEN = "IMPLEMENT_CHILD_LOCK_TOKEN"

#: Subdirectory of the worklog store that holds per-work-item lock files.
LOCK_DIR_NAME = "locks"

#: Filename prefix for a per-work-item lock file.
LOCK_FILE_PREFIX = "child-"

_METADATA_KEYS = ("agent", "pid", "session_id", "started_at", "token")


@dataclass
class LockResult:
    """Outcome of :meth:`WorkItemLock.acquire`.

    Attributes:
        acquired: True when this handle owns the critical section (a real
            kernel ``flock`` **or** a re-entrant no-op).
        owner: Metadata recorded for *this* holder (the file contents on a
            fresh acquire, or the holder's metadata on a re-entrant no-op).
            ``None`` when not acquired and the holder's metadata was
            unreadable.
        held_by: Metadata of the blocking holder when ``acquired`` is False,
            else the current owner metadata (also set for a re-entrant
            result). ``None`` when the holder's metadata is unreadable.
        reentrant: True when acquisition was a token-based re-entrant no-op
            rather than a new kernel ``flock``.
        reclaimed: Metadata of a dead-pid holder this acquire reclaimed, or
            ``None``.
        token: The ownership token of this handle.
        path: Absolute path of the work item's lock file.
    """

    acquired: bool
    owner: dict | None
    held_by: dict | None
    reentrant: bool
    reclaimed: dict | None
    token: str | None
    path: Path


class WorkItemLockHeld(RuntimeError):
    """Raised by the context manager when another live holder owns the lock."""

    def __init__(self, result: LockResult) -> None:
        self.result = result
        holder = result.held_by or {}
        identity = "unknown holder"
        if holder:
            identity = (
                f"{holder.get('agent', '?')}@{holder.get('pid', '?')} "
                f"(session {holder.get('session_id', '?')}, "
                f"since {holder.get('started_at', '?')})"
            )
        super().__init__(
            f"work item lock is held by {identity}: {result.path}"
        )


def lock_file_path(work_item_id: str, worklog_dir: str | os.PathLike[str]) -> Path:
    """Return ``<worklog_dir>/locks/child-<work_item_id>.lock``."""
    return (
        Path(worklog_dir)
        / LOCK_DIR_NAME
        / f"{LOCK_FILE_PREFIX}{work_item_id}.lock"
    )


def _resolve_worklog_dir(work_item_id: str) -> Path:
    """Resolve the worklog store (``.worklog``) that owns *work_item_id*.

    Reuses the shared ``wl`` flag resolution (:func:`resolve_worklog_dir` in
    ``shared.status_lifecycle``) so the lock never lands inside a git
    worktree — a worktree can be removed mid-flight while its lock must
    outlive it.
    """
    try:
        from shared.status_lifecycle import resolve_worklog_dir
    except ImportError:  # pragma: no cover - package layout fallback
        from status_lifecycle import resolve_worklog_dir  # type: ignore[no-redef]

    resolved = resolve_worklog_dir(work_item_id)
    if resolved is None:
        raise RuntimeError(
            "cannot resolve the worklog store for work item "
            f"{work_item_id!r}; pass worklog_dir explicitly "
            f"(expected <project>/.worklog)"
        )
    return Path(resolved)


def _pid_alive(pid: Any) -> bool:
    """Return True when *pid* names a live process.

    A reaped process is reliably reported dead. ``PermissionError`` means the
    process exists but is owned by someone else, i.e. alive. Any other
    ``OSError`` is ambiguous and treated as alive (fail closed — never reclaim
    a holder we cannot prove dead).
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_metadata(path: Path) -> tuple[dict | None, bool]:
    """Read the lock file's metadata.

    Returns:
        ``(metadata, readable)``. ``metadata`` is ``None`` for a missing or
        empty file. ``readable`` is False when the file is non-empty but
        cannot be parsed as a JSON object — an ambiguous state that must fail
        closed rather than overwrite a possible live holder.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    if not raw.strip():
        return None, True
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, False
    if not isinstance(data, dict):
        return None, False
    return data, True


def _is_valid_metadata(metadata: dict) -> bool:
    """True when *metadata* carries every documented owner key."""
    return all(key in metadata for key in _METADATA_KEYS)


def _write_metadata(fd: int, metadata: dict) -> None:
    """Overwrite the lock file through *fd* with *metadata* as JSON."""
    payload = json.dumps(metadata, sort_keys=True).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)


class WorkItemLock:
    """A non-blocking, flock-backed exclusive lock for one work item.

    Args:
        work_item_id: The work item whose critical section is serialised.
        agent: Agent name recorded in the lock file (diagnostics).
        session_id: Session identifier recorded in the lock file.
        worklog_dir: The worklog **store** directory (``.worklog``). When
            omitted it is resolved from *work_item_id*; the lock file always
            lives at ``<worklog_dir>/locks/child-<work_item_id>.lock``.

    The ownership token is taken from :data:`ENV_LOCK_TOKEN` when set (a
    holder chain propagating its ownership), otherwise generated per handle.

    Raises:
        RuntimeError: On a non-POSIX platform without ``fcntl``.
    """

    def __init__(
        self,
        work_item_id: str,
        *,
        agent: str,
        session_id: str,
        worklog_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX
            raise RuntimeError("work_item_lock requires fcntl (POSIX)")

        self.work_item_id = work_item_id
        self.agent = agent
        self.session_id = session_id
        self.token = os.environ.get(ENV_LOCK_TOKEN) or uuid.uuid4().hex

        resolved_dir = (
            Path(worklog_dir) if worklog_dir is not None
            else _resolve_worklog_dir(work_item_id)
        )
        self.path = lock_file_path(work_item_id, resolved_dir)

        self._fd: int | None = None
        self._reentrant = False
        self._last_result: LockResult | None = None

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def _owner_metadata(self) -> dict:
        return {
            "agent": self.agent,
            "pid": os.getpid(),
            "session_id": self.session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "token": self.token,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> LockResult:
        """Try once, non-blocking, to take the work item's lock.

        Never raises on contention and never waits. Returns a
        :class:`LockResult` whose ``acquired`` flag reports ownership.
        """
        if self._fd is not None or self._reentrant:
            # Already held by this handle (idempotent re-acquire).
            return self._last_result or LockResult(
                acquired=True,
                owner=None,
                held_by=None,
                reentrant=self._reentrant,
                reclaimed=None,
                token=self.token,
                path=self.path,
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # The lock is held by a live open file description. Check for
            # token-based re-entrancy before reporting contention.
            result = self._contended_result(fd)
            self._last_result = result
            return result

        # The kernel lock is ours. Inspect the previous holder's metadata:
        # reclaiming stale (dead-pid) metadata and replacing it is safe
        # because no other flock holder exists.
        metadata, readable = _read_metadata(self.path)
        if not readable or (metadata is not None and not _is_valid_metadata(metadata)):
            # Ambiguous state: fail closed. Release the lock and report held
            # rather than overwrite metadata we cannot interpret.
            LOG.warning(
                "work item lock %s has unreadable/corrupt metadata; failing "
                "closed. Inspect and, only if no live holder exists, remove it.",
                self.path,
            )
            os.close(fd)
            result = LockResult(
                acquired=False,
                owner=None,
                held_by=metadata,
                reentrant=False,
                reclaimed=None,
                token=self.token,
                path=self.path,
            )
            self._last_result = result
            return result

        reclaimed = None
        if metadata is not None:
            recorded_pid = metadata.get("pid")
            if not _pid_alive(recorded_pid):
                reclaimed = metadata
                LOG.warning(
                    "reclaimed stale work item lock %s: previous holder "
                    "%s (pid %s) is no longer alive; kernel lock was free.",
                    self.path,
                    metadata.get("agent", "?"),
                    recorded_pid,
                )

        owner = self._owner_metadata()
        try:
            _write_metadata(fd, owner)
        except OSError:
            # Cannot record ownership: fail closed rather than proceed with a
            # lock whose state cannot be diagnosed.
            LOG.warning(
                "work item lock %s: failed to record owner metadata; "
                "releasing and failing closed.",
                self.path,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            result = LockResult(
                acquired=False,
                owner=None,
                held_by=metadata,
                reentrant=False,
                reclaimed=None,
                token=self.token,
                path=self.path,
            )
            self._last_result = result
            return result

        self._fd = fd
        result = LockResult(
            acquired=True,
            owner=owner,
            held_by=None,
            reentrant=False,
            reclaimed=reclaimed,
            token=self.token,
            path=self.path,
        )
        self._last_result = result
        return result

    def _contended_result(self, fd: int) -> LockResult:
        """Build the result for a failed ``flock``; closes *fd*."""
        metadata, readable = _read_metadata(self.path)
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - defensive
            pass

        if not readable:
            LOG.warning(
                "work item lock %s is held but its metadata is unreadable; "
                "failing closed (holder identity unknown).",
                self.path,
            )
            return LockResult(
                acquired=False,
                owner=None,
                held_by=None,
                reentrant=False,
                reclaimed=None,
                token=self.token,
                path=self.path,
            )

        # Token-based self-ownership: the holder chain propagated its token,
        # so this acquire is a re-entrant no-op — never a false block. Pid
        # equality alone is deliberately NOT an ownership signal.
        if metadata is not None and metadata.get("token") == self.token:
            return LockResult(
                acquired=True,
                owner=metadata,
                held_by=metadata,
                reentrant=True,
                reclaimed=None,
                token=self.token,
                path=self.path,
            )

        return LockResult(
            acquired=False,
            owner=None,
            held_by=metadata,
            reentrant=False,
            reclaimed=None,
            token=self.token,
            path=self.path,
        )

    def release(self) -> None:
        """Release the kernel lock (idempotent, safe on a losing handle).

        A re-entrant handle never owned the kernel lock, so releasing it is a
        no-op — it must never drop the real holder's lock.
        """
        if self._reentrant:
            self._reentrant = False
            return

        fd = self._fd
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - defensive
            pass
        finally:
            try:
                os.close(fd)
            finally:
                self._fd = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> LockResult:
        result = self.acquire()
        if not result.acquired:
            raise WorkItemLockHeld(result)
        if result.reentrant:
            self._reentrant = True
        return result

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.release()


__all__ = [
    "ENV_LOCK_TOKEN",
    "LOCK_DIR_NAME",
    "LockResult",
    "WorkItemLock",
    "WorkItemLockHeld",
    "lock_file_path",
]
