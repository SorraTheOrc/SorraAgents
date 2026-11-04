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
- Default: 6 concurrent workers when neither is provided **and the live
  proxy slot status is unavailable**.

For audit pi-call workloads the effective ceiling is resolved by
``audit_runner._audit_semaphore_max_workers()`` (SA-0MVF1NKZQ009P3ZG AC3):
an explicit argument or ``AUDIT_MAX_CONCURRENCY`` is authoritative, and
otherwise the **live proxy slot capacity** is consulted first
(``/llama/local/status`` → ``max(1, min(available_slots, total_slots))``,
fail-open) so a 2-slot proxy never queues six pi sessions behind two slots.
The static 6 here is the fallback when the slot query fails; it is
right-sized to the host's measured audit-slot capacity
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

Arrival-ordered turnstile (SA-0MVSJX5HS002DJHI)
-----------------------------------------
The slot pool alone grants a free slot to whichever waiter happens to poll
next, so under a herd a late arrival can overtake a waiter that has been
queuing for hours (measured: one gate waited 2 h 9 min before expiring with
``slot_busy``). Fairness is therefore **ordering**, not capacity: an
arrival-ordered turnstile sits in front of the pool and only the waiter
holding the **lowest outstanding ticket** may attempt a slot.

Protocol (all kernel-``flock`` based, no PID bookkeeping):

- A waiter allocates a strictly increasing ticket from a counter file (the
  counter lock is held only for the allocation) and, **while still holding
  it**, creates and flocks its own ticket file. A later allocator therefore
  never observes an announced-but-unlocked ticket.
- A waiter attempts the slot pool only when no lower-numbered ticket is
  *locked*. Lower ticket files that are not locked belong to a waiter that
  already acquired, timed out or died; they are reclaimed (unlinked).
- The ticket is released when the waiter acquires a slot, times out, raises,
  or dies: the kernel drops the ``flock`` on process exit (including
  ``SIGKILL``), so a killed waiter's turn is reclaimed within one retry
  cadence — no stale tickets and no PID bookkeeping.
- **Bound:** a waiter at queue position *N* acquires within roughly *N* x the
  expected slot-hold time (plus one retry cadence), because every waiter
  ahead of it is served first. The bound is per namespace.

Compatibility guarantees:

- **Fail-fast callers** (``timeout=0``, as ``audit_runner`` uses) bypass the
  turnstile entirely: an immediate attempt must still succeed when a slot is
  free and must raise immediately when the pool is full — a turnstile must
  never deadlock a non-queuing caller.
- **Nested-run isolation** is preserved: the turnstile lives under the
  namespace directory, so a run isolated with :func:`nested_semaphore_dir`
  (``PI_SEMAPHORE_DIR``) never queues in the host-wide turnstile.
- **Staggered-wait jitter** (SA-0MVEDOF7A004TSWD) still applies to the retry
  cadence and deadline; ordering complements it rather than replacing it.

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
#: Subdirectory of a semaphore's slot directory that holds the arrival-ordered
#: turnstile (ticket files + the allocation counter).
_TURNSTILE_SUBDIR = "turnstile"
#: Name of the ticket-allocation counter file inside the turnstile directory.
_TURNSTILE_COUNTER = "next"
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
        self._turnstile_dir = self._slot_dir / _TURNSTILE_SUBDIR
        self._held_fd: int | None = None
        #: The ticket number taken by the most recent ``acquire()`` (None when
        #: the acquire was fail-fast or re-entrant). A read-only diagnostic:
        #: the fairness contract is that lower tickets are granted first, and
        #: this lets a caller (or a test) attribute an acquisition to its
        #: arrival position without adding a second timer or log channel.
        self.last_ticket: int | None = None
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
        self._turnstile_dir.mkdir(parents=True, exist_ok=True)
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
    # Arrival-ordered turnstile (SA-0MVSJX5HS002DJHI)
    # ------------------------------------------------------------------

    def _allocate_ticket(self) -> tuple[int, int]:
        """Take the next monotonic ticket and hold its ``flock``.

        The counter lock serialises number allocation; the ticket file is
        created **and locked** before the counter lock is released, so a
        later allocator can never observe its own higher ticket next to an
        announced-but-unlocked lower one.

        Returns:
            ``(ticket_number, ticket_fd)`` — the caller owns *ticket_fd* and
            must pass it to :meth:`_release_ticket`.
        """
        counter_path = self._turnstile_dir / _TURNSTILE_COUNTER
        counter_fd = os.open(counter_path, os.O_CREAT | os.O_RDWR, 0o644)
        ticket_fd: int | None = None
        try:
            fcntl.flock(counter_fd, fcntl.LOCK_EX)
            os.lseek(counter_fd, 0, os.SEEK_SET)
            raw = os.read(counter_fd, 64).decode("ascii", "ignore").strip()
            try:
                next_ticket = int(raw)
            except ValueError:  # missing/corrupt counter — recover from files
                next_ticket = 0
            # Never reuse a number still named by a surviving ticket file, so
            # a crash mid-allocation cannot make two waiters share a ticket.
            for name in os.listdir(self._turnstile_dir):
                if name.isdigit():
                    next_ticket = max(next_ticket, int(name) + 1)
            os.lseek(counter_fd, 0, os.SEEK_SET)
            os.ftruncate(counter_fd, 0)
            os.write(counter_fd, str(next_ticket + 1).encode("ascii"))
            ticket_path = self._turnstile_dir / str(next_ticket)
            ticket_fd = os.open(ticket_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(ticket_fd, fcntl.LOCK_EX)
            return next_ticket, ticket_fd
        finally:
            try:
                fcntl.flock(counter_fd, fcntl.LOCK_UN)
            finally:
                os.close(counter_fd)

    def _release_ticket(self, ticket: int, ticket_fd: int) -> None:
        """Release a ticket: unlock, close and unlink it (idempotent)."""
        try:
            try:
                fcntl.flock(ticket_fd, fcntl.LOCK_UN)
            finally:
                os.close(ticket_fd)
        finally:
            try:
                os.unlink(self._turnstile_dir / str(ticket))
            except OSError:
                pass

    def _lower_ticket_active(self, ticket: int) -> bool:
        """Whether any lower-numbered ticket is still held by a waiter.

        A lower ticket file that can be locked is no longer held (its waiter
        acquired, timed out or died — the kernel releases ``flock`` on process
        death), so it is reclaimed here. Returns True as soon as an active
        lower ticket is found; otherwise False once the outstanding set has
        been scanned.
        """
        try:
            names = os.listdir(self._turnstile_dir)
        except OSError:  # pragma: no cover - namespace removed concurrently
            return False
        for name in names:
            if not name.isdigit():
                continue
            other = int(name)
            if other >= ticket:
                continue
            path = self._turnstile_dir / name
            try:
                fd = os.open(path, os.O_RDWR)
            except OSError:
                continue  # reclaimed by its owner mid-scan
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return True  # an older waiter still holds its turn
                raise
            # Not held: the owner acquired, timed out or died — reclaim it.
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive
                pass
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:  # pragma: no cover - raced with another reclaimer
                pass
        return False

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
        self.last_ticket = None

        # Fail-fast callers (audit_runner's timeout=0) are exempt from the
        # turnstile: an immediate attempt must still succeed when a slot is
        # free, and must raise immediately when the pool is full — queueing it
        # behind outstanding tickets would deadlock a non-waiting caller.
        if effective_timeout == 0:
            return self._fail_fast_acquire(effective_timeout)

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

        ticket, ticket_fd = self._allocate_ticket()
        self.last_ticket = ticket
        try:
            while True:
                # Only the lowest outstanding ticket may attempt the pool, so
                # a later arrival can never overtake an earlier waiter and the
                # wait is bounded by the queue position (SA-0MVSJX5HS002DJHI).
                if not self._lower_ticket_active(ticket):
                    fd = self._try_acquire_slot()
                    if fd is not None:
                        self._held_fd = fd
                        return True

                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"semaphore '{self.name}' busy: no slot free within "
                        f"{effective_timeout}s (max_workers={self.max_workers}, "
                        f"ticket={ticket})"
                    )
                if self.jitter > 0:
                    time.sleep(_jittered_retry_delay(self._rng))
                else:
                    time.sleep(_RETRY_DELAY_SECONDS)
        finally:
            self._release_ticket(ticket, ticket_fd)

    def _fail_fast_acquire(self, effective_timeout: float | None) -> bool:
        """Single immediate slot attempt for a ``timeout=0`` caller."""
        fd = self._try_acquire_slot()
        if fd is not None:
            self._held_fd = fd
            return True
        raise TimeoutError(
            f"semaphore '{self.name}' busy: no slot free within "
            f"{effective_timeout}s (max_workers={self.max_workers})"
        )

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
