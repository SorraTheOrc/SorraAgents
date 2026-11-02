"""Per-repository exclusive lock backed by ``fcntl.flock``.

Serialises the windows that mutate a repository's **shared** branch refs so two
runs in one checkout can never interleave their ref updates
(SA-0MVPZG3IQ009IJMO, parent SA-0MVNQURZM001TKJY). In ``implement.py`` those
windows are:

- ``finish``: commit → fast-forward local parent branch → push → post-push
  parent-branch sync (all on the shared main checkout);
- ``start``: the pre-worktree parent-branch refresh, which fast-forwards the
  same shared local parent ref.

Without the lock the 2026-10-25 incident shape is possible: finish A moves the
shared local ``dev`` to a commit it never published, finish B (or a start
refresh) then moves ``dev`` past it, and A's commit becomes reachable only from
its feature branch while both runs report success.

Design
------
One regular lock file per repository (default
``<repo-root>/.worklog/locks/main-checkout.lock``, matching the per-work-item
lock convention in :mod:`skill.shared.work_item_lock`). Ownership is the
kernel's exclusive advisory ``flock`` on that file's **open file description**:

- **Crash-safe.** The kernel releases the ``flock`` when the holding file
  descriptor is closed — including on ``SIGKILL``, an unhandled exception or a
  hard crash. There is therefore no stale-lock problem and no lease to expire.
- **Metadata is never trusted.** The holder metadata written into the file
  (work item id, pid, ISO start time, repo root, host) exists **only** to make
  a refusal actionable. A lock file left behind by a dead holder is never
  consulted to decide ownership, so it cannot block a later run.
- **Bounded wait.** ``timeout=0`` (the default) is a single non-blocking
  attempt: the caller fails fast. ``timeout > 0`` retries until the deadline
  and then returns an actionable timeout message naming the lock path, the
  holder and the recovery step. An indefinite wait is rejected outright
  (``timeout=None`` raises ``ValueError``) so no caller can hang forever.
- **Per-repository scope.** The lock file lives inside the checkout, so runs in
  different repositories on the same host never contend.

Usage
-----
.. code-block:: python

    from shared.repo_lock import RepoLock

    lock = RepoLock(repo_root)
    acquired, refusal = lock.acquire("SA-0MVPZG3IQ009IJMO", timeout=0)
    if not acquired:
        raise RuntimeError(refusal)  # already actionable
    try:
        mutate_shared_refs()
    finally:
        lock.release()

    # Or as a context manager (raises ``RepoLockTimeout`` on refusal):
    with RepoLock(repo_root) as lock:
        mutate_shared_refs()
"""

from __future__ import annotations

import errno
import json
import os
import socket
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    from typing import Self
else:  # pragma: no cover - Python < 3.11
    Self = Any  # type: ignore[misc]

try:  # pragma: no cover - non-POSIX fallback
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

#: Lock-file path relative to the repository root. Kept identical to the
#: historical ``implement.py`` main-checkout lock so an in-flight run started
#: before this helper existed is still excluded.
DEFAULT_LOCK_RELPATH = os.path.join(".worklog", "locks", "main-checkout.lock")

#: Poll cadence while waiting for a held lock (seconds).
RETRY_DELAY_SECONDS = 0.05


class RepoLockTimeout(RuntimeError):
    """Raised by :meth:`RepoLock.__enter__` when the lock cannot be acquired.

    Carries the actionable refusal message (lock path, holder, recovery step)
    produced by :meth:`RepoLock.acquire`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def describe_holder(holder: dict[str, Any] | None) -> str:
    """Return a human-readable description of a lock holder.

    Every metadata field present in the lock file is included so a refusal
    names the concurrent actor (work item, pid, start time, repo, host).
    Degrades to a generic "unknown" description when the file carries no
    readable metadata — which is never an error, because the metadata is
    diagnostic only.
    """
    if not holder:
        return "unknown (lock file present but no holder metadata)"
    parts: list[str] = []
    wid = holder.get("work_item_id")
    parts.append(f"work item {wid}" if wid else "unknown work item")
    if holder.get("pid"):
        parts.append(f"pid {holder['pid']}")
    if holder.get("started_at"):
        parts.append(f"started {holder['started_at']}")
    if holder.get("repo_root"):
        parts.append(f"repo {holder['repo_root']}")
    if holder.get("host"):
        parts.append(f"host {holder['host']}")
    return ", ".join(parts)


class RepoLock:
    """Exclusive, crash-safe, per-repository lock.

    Args:
        repo_root: Path to the repository (checkout) the lock belongs to.
        relpath: Lock-file path relative to *repo_root*. Defaults to
            :data:`DEFAULT_LOCK_RELPATH`; a sibling helper may pass its own so
            independent windows get independent locks.

    Attributes:
        path: Absolute path of the lock file (created lazily on acquisition).
    """

    def __init__(
        self,
        repo_root: str | os.PathLike[str],
        relpath: str = DEFAULT_LOCK_RELPATH,
    ) -> None:
        self.repo_root = str(repo_root)
        self.path = Path(self.repo_root) / relpath
        self._held_fd: int | None = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def held(self) -> bool:
        """True while this instance holds the lock."""
        return self._held_fd is not None

    def read_holder(self) -> dict[str, Any] | None:
        """Read the holder metadata (best-effort; diagnostic only)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def refusal_message(
        self, holder_id: str, description: str, timeout: float
    ) -> str:
        """Build the actionable refusal message for a held lock.

        Names the lock path, the current holder and the recovery step, and (for
        a bounded wait that expired) states the deadline that was exceeded.
        """
        if timeout and timeout > 0:
            wait = (
                f"Timed out after {timeout:g}s waiting for the repository lock "
                f"{self.path} (holder: {description})."
            )
        else:
            wait = (
                f"Another run holds the repository lock {self.path} "
                f"(holder: {description})."
            )
        return (
            f"{wait} Two runs that mutate the shared branch refs of one "
            f"checkout must never overlap. Aborting without modifying the "
            f"checkout or the work item — check for a concurrent finish in "
            f"this repository, then re-run once the holder completes "
            f"(requested by {holder_id})."
        )

    def acquire(
        self, holder_id: str, timeout: float = 0.0
    ) -> tuple[int | None, str | None]:
        """Try to take the exclusive lock.

        Args:
            holder_id: Identifier recorded in the lock file and named in the
                refusal message (e.g. the work item id).
            timeout: Bounded wait in seconds. ``0`` (default) fails fast with a
                single non-blocking attempt; ``> 0`` retries until the deadline.

        Returns:
            ``(fd, None)`` when acquired — the caller releases with
            :meth:`release` — or ``(None, refusal_message)`` when the lock is
            held by another run (now, or for the whole bounded wait).

        Raises:
            ValueError: If *timeout* is negative or ``None``. An indefinite wait
                is deliberately unsupported: a lock must never hang a run.
        """
        if fcntl is None:  # pragma: no cover - non-POSIX
            raise RuntimeError("repo_lock requires fcntl (POSIX)")
        if timeout is None:
            raise ValueError(
                "repo_lock does not support an indefinite wait; pass a finite "
                "timeout (0 = fail fast)"
            )
        timeout = float(timeout)
        if timeout < 0:
            raise ValueError("timeout must be >= 0")

        if self._held_fd is not None:
            return self._held_fd, None  # re-entrant no-op for this instance

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return None, f"cannot create lock directory {self.path.parent}: {exc}"
        try:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            return None, f"cannot open lock file {self.path}: {exc}"

        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, AttributeError) as exc:
                busy = isinstance(exc, OSError) and exc.errno in (
                    errno.EACCES,
                    errno.EAGAIN,
                )
                if not busy:
                    os.close(fd)
                    return None, f"flock failed on {self.path}: {exc}"
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None, self.refusal_message(
                        holder_id, describe_holder(self.read_holder()), timeout
                    )
                time.sleep(RETRY_DELAY_SECONDS)

        self._held_fd = fd
        self._write_holder(fd, holder_id)
        return fd, None

    def _write_holder(self, fd: int, holder_id: str) -> None:
        """Record holder metadata for refusal messages (best-effort)."""
        holder = {
            "work_item_id": holder_id,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "repo_root": self.repo_root,
            "host": socket.gethostname(),
        }
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps(holder, indent=2).encode("utf-8"))
        except OSError:  # metadata is diagnostic only; the flock is the lock
            pass

    def release(self) -> None:
        """Release the lock. Idempotent (safe after a failed acquisition)."""
        fd = self._held_fd
        if fd is None:
            return
        self._held_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, AttributeError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Self:
        """Acquire fail-fast, raising :class:`RepoLockTimeout` when held."""
        acquired, refusal = self.acquire(self.repo_root, timeout=0.0)
        if not acquired:
            raise RepoLockTimeout(refusal or "repository lock unavailable")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Release the lock on every exit path, including exceptions."""
        self.release()


def acquire_repo_lock(
    repo_root: str | os.PathLike[str],
    holder_id: str,
    timeout: float = 0.0,
    relpath: str = DEFAULT_LOCK_RELPATH,
) -> tuple[int | None, str | None]:
    """Functional form of :meth:`RepoLock.acquire`.

    Returns ``(fd, None)`` on success or ``(None, refusal_message)`` when the
    lock is held. The returned descriptor is released with
    :func:`release_repo_lock`.
    """
    return RepoLock(repo_root, relpath=relpath).acquire(holder_id, timeout=timeout)


def release_repo_lock(fd: int | None) -> None:
    """Release a descriptor returned by :func:`acquire_repo_lock` (idempotent)."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except (OSError, AttributeError):
        pass
    try:
        os.close(fd)
    except OSError:
        pass
