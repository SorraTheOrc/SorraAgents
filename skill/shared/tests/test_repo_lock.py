"""Unit tests for ``skill/shared/repo_lock.py`` (SA-0MVPZG3IQ009IJMO).

The per-repository lock serialises the ``implement.py`` windows that mutate a
repository's shared branch refs. Its contract is:

- ownership is the kernel ``flock`` — never the lock file's existence or its
  holder metadata (so a file left by a crashed run cannot block anyone),
- a **bounded** wait: ``timeout=0`` fails fast, ``timeout>0`` retries to a
  deadline and then returns an actionable message naming the lock, the holder
  and the recovery step — never an indefinite hang,
- the lock is released on every exit path, including exceptions.

Integration coverage of the ``implement.py`` wrappers (``phase_finish``
refusal, SIGKILL crash release, per-checkout scope) lives in
``skill/implement/tests/test_finish_main_checkout_lock.py`` and the dev-ref
window scenarios in ``tests/test_implement_finish_concurrent_orphan.py``; this
module pins the helper's own API so those layers can stay behaviour-focused.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

from shared.repo_lock import (
    DEFAULT_LOCK_RELPATH,
    RepoLock,
    RepoLockTimeout,
    acquire_repo_lock,
    describe_holder,
    release_repo_lock,
)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "main"
    root.mkdir()
    return root


def test_lock_file_lives_inside_the_repository(repo_root: Path):
    """The lock is per-repository: its file sits in the checkout it guards."""
    lock = RepoLock(repo_root)
    assert lock.path == repo_root / DEFAULT_LOCK_RELPATH
    assert DEFAULT_LOCK_RELPATH == os.path.join(
        ".worklog", "locks", "main-checkout.lock"
    )


def test_fail_fast_default_does_not_wait(repo_root: Path):
    """``timeout=0`` refuses immediately with an actionable message."""
    holder = RepoLock(repo_root)
    fd, refusal = holder.acquire("WL-HOLDER")
    assert fd is not None and refusal is None
    try:
        contender = RepoLock(repo_root)
        started = time.monotonic()
        fd2, refusal2 = contender.acquire("WL-CONTENDER")
        elapsed = time.monotonic() - started

        assert fd2 is None
        assert elapsed < 1.0, "a fail-fast acquire must not wait"
        assert refusal2 is not None
        assert str(contender.path) in refusal2
        assert "WL-HOLDER" in refusal2
        assert "re-run" in refusal2.lower()
    finally:
        holder.release()


def test_bounded_wait_acquires_once_the_holder_releases(repo_root: Path):
    """A positive timeout waits (bounded) for the holder instead of refusing."""
    holder = RepoLock(repo_root)
    fd, _ = holder.acquire("WL-HOLDER")
    assert fd is not None

    releaser = threading.Timer(0.2, holder.release)
    releaser.start()
    try:
        contender = RepoLock(repo_root)
        fd2, refusal = contender.acquire("WL-CONTENDER", timeout=10)
        assert fd2 is not None, refusal
        assert refusal is None
        contender.release()
    finally:
        releaser.cancel()
        holder.release()


def test_bounded_wait_times_out_with_actionable_message(repo_root: Path):
    """An expired bounded wait names the lock, the holder and the deadline."""
    holder = RepoLock(repo_root)
    fd, _ = holder.acquire("WL-HOLDER")
    assert fd is not None
    try:
        contender = RepoLock(repo_root)
        started = time.monotonic()
        fd2, refusal = contender.acquire("WL-CONTENDER", timeout=0.3)
        elapsed = time.monotonic() - started

        assert fd2 is None
        assert 0.3 <= elapsed < 5.0, (
            f"the wait must be bounded by the configured timeout; took {elapsed}s"
        )
        assert refusal is not None
        assert "Timed out after 0.3s" in refusal
        assert str(contender.path) in refusal
        assert "WL-HOLDER" in refusal
        assert "concurrent finish" in refusal
        assert "re-run" in refusal.lower()
    finally:
        holder.release()


def test_bounded_wait_reports_progress_naming_the_holder(repo_root: Path):
    """A positive timeout emits progress naming the lock, the bound and holder.

    The first contention message is immediate (an agent watching a long
    ``finish`` call sees who it is waiting for), heartbeats are throttled by
    ``progress_interval`` and a final line records the acquisition
    (SA-0MWFJS65O005JMJF).
    """
    holder = RepoLock(repo_root)
    fd, _ = holder.acquire("WL-HOLDER")
    assert fd is not None

    releaser = threading.Timer(0.3, holder.release)
    releaser.start()
    messages: list[str] = []
    try:
        contender = RepoLock(repo_root)
        fd2, refusal = contender.acquire(
            "WL-CONTENDER",
            timeout=10,
            progress=messages.append,
            progress_interval=0.05,
        )
        assert fd2 is not None, refusal
        contender.release()
    finally:
        releaser.cancel()
        holder.release()

    assert messages, "a waiting acquire must report progress"
    first = messages[0]
    assert "Waiting for the repository lock" in first
    assert str(RepoLock(repo_root).path) in first
    assert "up to 10s" in first
    assert "WL-HOLDER" in first, "progress must name the current holder"
    assert any("Still waiting" in m for m in messages), messages
    assert messages[-1].startswith("Acquired the repository lock"), messages


def test_progress_is_not_emitted_without_a_wait(repo_root: Path):
    """A fail-fast acquisition never reports wait progress."""
    messages: list[str] = []
    lock = RepoLock(repo_root)
    fd, refusal = lock.acquire("WL-FIRST", progress=messages.append)
    assert fd is not None, refusal
    try:
        # A contended *fail-fast* attempt has no positive bound to wait out.
        fd2, refusal2 = RepoLock(repo_root).acquire(
            "WL-SECOND", timeout=0, progress=messages.append
        )
        assert fd2 is None and refusal2 is not None
    finally:
        lock.release()
    assert messages == [], messages


def test_a_failing_progress_callback_never_breaks_the_wait(repo_root: Path):
    """Progress is diagnostic: a raising callback must not fail the acquire."""
    holder = RepoLock(repo_root)
    fd, _ = holder.acquire("WL-HOLDER")
    assert fd is not None

    def _broken(_message: str) -> None:
        raise RuntimeError("logger exploded")

    releaser = threading.Timer(0.2, holder.release)
    releaser.start()
    try:
        contender = RepoLock(repo_root)
        fd2, refusal = contender.acquire(
            "WL-CONTENDER",
            timeout=10,
            progress=_broken,
            progress_interval=0.05,
        )
        assert fd2 is not None, refusal
        contender.release()
    finally:
        releaser.cancel()
        holder.release()


def test_wait_is_interruptible_and_leaks_no_descriptor(repo_root: Path):
    """An interrupt during the wait propagates and leaves no lock behind.

    ``SIGINT``/``SIGTERM`` handlers (and a plain ``KeyboardInterrupt``) run
    between retries. The wait must surface the interrupt instead of swallowing
    it, close the descriptor it opened for the attempt, and leave the lock
    acquirable once the holder releases — never an orphaned file descriptor or
    a half-taken lock (SA-0MWFJS65O005JMJF signal-safety AC).

    The interrupt is raised in a **forked child**, never in the pytest main
    thread: an asynchronous ``KeyboardInterrupt`` in the test process could
    be delivered between tests and abort the whole suite.
    """
    holder = RepoLock(repo_root)
    fd, _ = holder.acquire("WL-HOLDER")
    assert fd is not None

    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - child process
        try:
            # The forked child inherits whatever SIGINT/SIGTERM handlers the
            # host pytest process has installed — several implement-skill
            # tests install ``implement._signal_handler``, which calls
            # ``os._exit(2)`` instead of raising. ``_thread.interrupt_main()``
            # *invokes a Python SIGINT handler* when one is registered, so
            # restore the default handler to model a plain caller receiving a
            # KeyboardInterrupt.
            signal.signal(signal.SIGINT, signal.default_int_handler)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            interrupted = False

            def _start_interrupt(_message: str) -> None:
                """Arm the interrupt only once the wait is really running.

                Starting it before ``acquire`` races a loaded machine: the
                timer could fire while the child is still being scheduled and
                abort it outside the wait.
                """
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    threading.Timer(0.2, _interrupt_main).start()

            contender = RepoLock(repo_root)
            try:
                contender.acquire(
                    "WL-INTERRUPTED", timeout=30, progress=_start_interrupt
                )
            except KeyboardInterrupt:
                # The wait surfaced the interrupt (it must not swallow it).
                os._exit(0)
            os._exit(9)  # the wait completed — the interrupt was swallowed
        except BaseException:  # noqa: BLE001 - child teardown
            os._exit(7)

    try:
        started = time.monotonic()
        _, status = os.waitpid(child_pid, 0)
        elapsed = time.monotonic() - started
        assert os.waitstatus_to_exitcode(status) == 0, (
            "the interrupted wait did not surface the KeyboardInterrupt "
            f"(exit {os.waitstatus_to_exitcode(status)})"
        )
        assert elapsed < 10, f"the interrupt must abort the wait; took {elapsed}s"
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(child_pid, 0)
        except ChildProcessError:
            pass
        holder.release()

    fd2, refusal = RepoLock(repo_root).acquire("WL-AFTER")
    assert fd2 is not None, refusal
    release_repo_lock(fd2)


def _interrupt_main() -> None:
    """Raise ``KeyboardInterrupt`` in the main thread (signal-equivalent)."""
    import _thread

    _thread.interrupt_main()


def test_indefinite_wait_is_rejected(repo_root: Path):
    """``timeout=None`` is refused: a lock must never hang a run forever."""
    lock = RepoLock(repo_root)
    with pytest.raises(ValueError, match="indefinite"):
        lock.acquire("WL-ANY", timeout=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout must be >= 0"):
        lock.acquire("WL-ANY", timeout=-1)


def test_leftover_metadata_never_decides_ownership(repo_root: Path):
    """A lock file written by a crashed run must not block a later acquire.

    Ownership is the ``flock`` only: a dead holder's metadata (impossible pid,
    stale timestamp) is diagnostic text, never a lease to expire.
    """
    lock = RepoLock(repo_root)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps(
            {
                "work_item_id": "WL-CRASHED",
                "pid": 999_999,
                "started_at": "2026-01-01T00:00:00+00:00",
                "repo_root": str(repo_root),
                "host": "crashed-host",
            }
        ),
        encoding="utf-8",
    )

    fd, refusal = lock.acquire("WL-RECLAIM")
    assert fd is not None, refusal
    lock.release()

    # The new holder's metadata replaced the stale record.
    assert lock.read_holder()["work_item_id"] == "WL-RECLAIM"


def test_context_manager_releases_on_exception(repo_root: Path):
    """The lock is released even when the guarded block raises."""
    with pytest.raises(RuntimeError, match="boom"), RepoLock(repo_root) as lock:
        assert lock.held
        raise RuntimeError("boom")

    assert not lock.held
    fd, refusal = RepoLock(repo_root).acquire("WL-AFTER")
    assert fd is not None, refusal
    release_repo_lock(fd)


def test_context_manager_raises_actionable_timeout(repo_root: Path):
    """``with`` refuses (never blocks) when another run holds the lock."""
    holder = RepoLock(repo_root)
    fd, _ = holder.acquire("WL-HOLDER")
    assert fd is not None
    try:
        with pytest.raises(RepoLockTimeout) as excinfo, RepoLock(repo_root):
            pytest.fail("the guarded block must not run")
        assert str(holder.path) in excinfo.value.message
        assert "WL-HOLDER" in excinfo.value.message
    finally:
        holder.release()


def test_functional_api_roundtrip(repo_root: Path):
    """``acquire_repo_lock``/``release_repo_lock`` mirror the class API."""
    fd, refusal = acquire_repo_lock(repo_root, "WL-FUNC")
    assert fd is not None, refusal
    try:
        fd2, refusal2 = acquire_repo_lock(repo_root, "WL-FUNC-2")
        assert fd2 is None
        assert refusal2 is not None and "WL-FUNC" in refusal2
    finally:
        release_repo_lock(fd)

    fd3, refusal3 = acquire_repo_lock(repo_root, "WL-FUNC-3")
    assert fd3 is not None, refusal3
    release_repo_lock(fd3)


def test_describe_holder_names_every_metadata_field():
    """Refusal text names the holder's identifying metadata."""
    description = describe_holder(
        {
            "work_item_id": "WL-ITEM",
            "pid": 4242,
            "started_at": "2026-01-01T00:00:00+00:00",
            "repo_root": "/repo",
            "host": "host-1",
        }
    )
    for fragment in ("WL-ITEM", "4242", "2026-01-01", "/repo", "host-1"):
        assert fragment in description

    assert "unknown" in describe_holder(None)
