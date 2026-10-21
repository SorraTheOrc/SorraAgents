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
- Default: 6 concurrent workers when neither is provided.

The default is right-sized to the host's measured audit-slot capacity
(SA-0MUSZJRB9006ZCHE): the F7 harness (``skill/shared/validate_fanout.py``)
drained a burst of 8 audits in 18.7s at ceiling 3 vs 12.9s at ceiling 6,
with a batch-attributable load delta <= 1.63 on the 16-core host and no swap
growth. Six is 2x the 3-slot local LLM proxy pool, so one inference-bound and
one local/tool-bound pi call can overlap per proxy slot without piling on
further proxy contention.

Nested-run isolation
--------------------
``PI_SEMAPHORE_DIR`` names the base lock directory. A run that already holds
one of the host-wide ``"test"`` slots spawns *nested* runs — pytest tests that
invoke ``run_tests.py`` in-process, or spawn it as a subprocess. Those nested
runs must not contend for the host-wide pool: the outer run keeps its slot
until the nested run finishes, so a nested acquire can wait out the whole
``TEST_LOCK_TIMEOUT`` and deadlock under concurrent agent load
(SA-0MUY6U05F008RG86). :func:`nested_semaphore_dir` returns an isolated base
directory for such invocations; top-level runs never call it, so the host-wide
ceiling (``TEST_MAX_CONCURRENCY``) is preserved for genuine parallel suites.

Timeout semantics
-----------------
- ``timeout=None`` — block indefinitely until a slot frees.
- ``timeout=0`` — fail fast: raise ``TimeoutError`` immediately if full.
- ``timeout>0`` — bounded wait; raise ``TimeoutError`` at the deadline.

Staggered-wait jitter (SA-0MVEDOF7A004TSWD)
-------------------------------------------
Bounded-wait consumers under a herd (the test skill's ``"test"`` semaphore)
pass ``jitter`` (a fraction of the timeout, default ``0.0`` = off) so
concurrent waiters do not all expire at one shared wall-clock instant and
re-enter the pool in lockstep. With ``jitter>0`` each waiter's deadline is
reduced by a uniform random fraction of the timeout (never extended past the
configured bound) and each retry sleeps a jittered cadence. ``jitter=0.0``
(the default) keeps historical byte-identical behaviour — audit
semaphores do not opt in.

Usage
-----
.. code-block:: python

    from shared.process_semaphore import Semaphore

    with Semaphore("audit", max_workers=2, timeout=30):
        run_heavy_work()

    # Staggered bounded wait (test skill):
    from shared.process_semaphore import Semaphore

    with Semaphore("test", max_workers=2, timeout=600, jitter=0.1):
        run_suite()

The context manager releases the slot on normal exit **and** on exception.
"""

from __future__ import annotations

import errno
import os
import random
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

DEFAULT_MAX_WORKERS = 6
ENV_MAX_WORKERS = "AUDIT_MAX_CONCURRENCY"
ENV_LOCK_DIR = "PI_SEMAPHORE_DIR"
#: Subdirectory (under a caller-provided base) that names an isolated
#: namespace for a nested (already-paced) run invocation.
NESTED_SEMAPHORE_SUBDIR = "pi-semaphores"
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


def nested_semaphore_dir(base_dir: str | os.PathLike[str]) -> Path:
    """Return an isolated semaphore base directory for a nested run.

    A run that already holds the host-wide ``"test"`` slot must not block its
    own nested runs behind that slot: the outer run keeps the slot until the
    nested run finishes, so a nested acquire can wait out the entire
    ``TEST_LOCK_TIMEOUT`` and deadlock under concurrent agent load
    (SA-0MUY6U05F008RG86). Callers (test fixtures, harnesses) pass a per-test
    temporary directory and export the result as ``PI_SEMAPHORE_DIR`` for the
    nested invocation — the nested run then bounds itself in its own
    namespace instead of the host-wide pool. Top-level runs never call this
    helper, so the host-wide ceiling (``TEST_MAX_CONCURRENCY``) is unchanged
    for genuine parallel suites.

    Args:
        base_dir: The caller's isolation root (e.g. pytest ``tmp_path``).

    Returns:
        ``<base_dir>/pi-semaphores`` — slot files are created lazily by
        :class:`Semaphore` (the directory itself need not exist yet).
    """
    return Path(base_dir) / NESTED_SEMAPHORE_SUBDIR


def jittered_deadline(
    start_time: float,
    timeout: float,
    jitter: float,
    rng: random.Random,
) -> float:
    """Return a per-waiter staggered deadline within the configured bound.

    Bounded-wait callers (e.g. the test skill's ``test`` semaphore) let a
    herd of concurrent waiters expire at one shared wall-clock instant, so
    they all report failure and retry together — a thundering herd that
    keeps re-entering the pool in lockstep (SA-0MVEDOF7A004TSWD). *jitter*
    is the maximum *fraction of the timeout* reduced per waiter: with
    ``jitter=0.1`` the deadline lands uniformly in the final 10% of the
    bound (``[start + 0.9*T, start + T]``), so waiters that enqueue at the
    same moment expire at different instants. A jitter of ``0.0`` returns
    the exact bound (historical byte-identical behaviour); larger waiters
    are never delayed past the bound — the offset only ever staggers
    earlier, satisfying the "never extends the deadline" invariant.

    Args:
        start_time: Monotonic timestamp when the wait began.
        timeout: The configured bounded wait (seconds).
        jitter: Stagger fraction in ``[0, 1)`` of *timeout*.
        rng: Random source (inject a seeded ``random.Random`` in tests).

    Returns:
        The absolute deadline (monotonic seconds).
    """
    if jitter <= 0:
        return start_time + timeout
    return start_time + timeout * (1.0 - rng.random() * jitter)


def _jittered_retry_delay(rng: random.Random) -> float:
    """A per-attempt sleep spread within +/-50% of the base cadence.

    Fixed-cadence polling keeps every waiter waking at the same instant,
    compounding the lockstep herd. The uniform ``[0.5, 1.5] * base`` spread
    desynchronises the retries so waiters do not storm the slot pool in
    lockstep (SA-0MVEDOF7A004TSWD AC1).
    """
    return _RETRY_DELAY_SECONDS * (0.5 + rng.random())


class Semaphore:
    """A flock-based counting semaphore usable across processes.

    Args:
        name: Logical name of the semaphore (shared across processes).
        max_workers: Maximum concurrent holders. Resolved as: explicit
            argument > ``AUDIT_MAX_CONCURRENCY`` env var > default (6).
        timeout: Bounded wait in seconds (``None`` = block indefinitely,
            ``0`` = fail fast). Default 30.
        jitter: Per-waiter stagger fraction of the bounded wait in
            ``[0, 1)`` (0.0 = off, historical behaviour). See
            :func:`jittered_deadline`.
        rng: Random source for the jitter (a seeded ``random.Random`` for
            deterministic tests; default module-level ``random``).
    """

    def __init__(
        self,
        name: str,
        max_workers: int | None = None,
        timeout: float | None = 30.0,
        jitter: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX
            raise RuntimeError("process_semaphore requires fcntl (POSIX)")

        self.name = name
        self._slot_dir = _default_lock_dir() / _sanitize_name(name)
        self._held_fd: int | None = None
        self.timeout = timeout

        self.jitter = float(jitter)
        if not (0.0 <= self.jitter < 1.0):
            raise ValueError("jitter must be in [0, 1)")
        self._rng = rng if rng is not None else random

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

        if effective_timeout is None:
            deadline: float | None = None
        else:
            now = time.monotonic()
            # Per-waiter stagger within the configured bound: a herd of
            # concurrent waiters no longer expires at one shared wall-clock
            # instant, so they do not all report failure and retry in
            # lockstep (SA-0MVEDOF7A004TSWD AC1/AC2). The jittered deadline
            # never exceeds the configured bound — it only staggers earlier.
            deadline = jittered_deadline(now, float(effective_timeout), self.jitter, self._rng)

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
            if self.jitter > 0:
                time.sleep(_jittered_retry_delay(self._rng))
            else:
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
