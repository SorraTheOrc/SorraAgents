"""Unit tests for the shared per-work-item lock (SA-0MVQ5A8WC0096BDL).

Test-first slice: these tests define the contract that
``skill/shared/work_item_lock.py`` (SA-0MVQ5AE7L005M0NR) must satisfy. They
cover the parent design (SA-0MVOABYW00004978) ACs:

- AC1: acquire succeeds non-blocking on a free lock at
  ``<worklog>/locks/child-<work-item-id>.lock`` and the lock file records
  owner metadata (agent, pid, session id, started-at, token).
- AC2: a second acquire on the same held lock fails fast (returns a held
  result) without raising and without writing to the lock file.
- AC3: the context manager releases the lock on normal exit **and** on a
  raised exception, so a fresh acquire then succeeds.
- AC4: a holder killed by ``SIGTERM`` releases the kernel lock; a subsequent
  acquire succeeds without manual cleanup (kernel-guaranteed ``flock``
  release, never a manual file delete).
- AC5: a lock file whose recorded owner pid is dead at acquisition time is
  reclaimed, and a warning naming the reclaimed holder is logged.
- AC6: token-based self-ownership — an acquire carrying the matching
  ``IMPLEMENT_CHILD_LOCK_TOKEN`` is a re-entrant no-op (never a false block);
  a *mismatched* token never grants ownership.

Contract pinned here (the API the implementation slice must provide)
--------------------------------------------------------------------
``shared.work_item_lock`` public surface:

- ``ENV_LOCK_TOKEN`` — the environment variable name, exactly
  ``"IMPLEMENT_CHILD_LOCK_TOKEN"``.
- ``WorkItemLock(work_item_id, *, agent, session_id, worklog_dir=None)`` —
  ``worklog_dir`` is the worklog **store** directory (``.worklog``); the
  lock file lives at ``<worklog_dir>/locks/child-<work_item_id>.lock``.
  When omitted it is resolved from the work item id. The token is read from
  ``ENV_LOCK_TOKEN`` when set, otherwise generated per holder.
- ``.acquire() -> LockResult`` — non-blocking, never raises on contention.
- ``.release()`` — releases the kernel lock; a no-op for a re-entrant handle
  (a re-entrant acquire is self-owned, not a second holder).
- ``LockResult`` attributes: ``acquired: bool``, ``owner: dict | None`` (the
  metadata written/read for *this* holder), ``held_by: dict | None`` (the
  metadata of the blocking holder when not acquired), ``reentrant: bool``,
  ``reclaimed: dict | None`` (metadata of a dead-pid holder reclaimed by this
  acquire), ``token: str | None``, ``path: Path``.
- Context manager: ``with WorkItemLock(...) as result:`` acquires and returns
  the ``LockResult``; on contention it raises ``WorkItemLockHeld`` (carrying
  ``.result``) rather than entering — fail closed, never a second writer.

Lock-file format is JSON with at least the keys ``agent``, ``pid``,
``session_id``, ``started_at`` (ISO-8601, timezone-aware) and ``token``.

The suite skips cleanly (``importorskip``) until the lock module lands in
SA-0MVQ5AE7L005M0NR, so this file can be committed green in advance of the
implementation (same convention as
``skill/shared/tests/test_concurrency_guard.py``).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT_FOR_TESTS = REPO_ROOT / "skill"
if str(_SKILLS_ROOT_FOR_TESTS) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT_FOR_TESTS))

import logging

import pytest

LOCK_MODULE = "shared.work_item_lock"

pytest.importorskip(LOCK_MODULE)

from shared.work_item_lock import (
    ENV_LOCK_TOKEN,
    WorkItemLock,
    WorkItemLockHeld,
)

ITEM_ID = "SA-0MVQ5A8WC0096BDL"
METADATA_KEYS = {"agent", "pid", "session_id", "started_at", "token"}
LOCK_LOGGER_NAME = "shared.work_item_lock"

#: Upper bound for a fail-fast contention check. Generous enough to never
#: flake on a loaded host, tight enough to catch a polling/sleeping
#: implementation (the contract forbids waiting).
FAIL_FAST_BOUND_SECONDS = 2.0

#: Worker shim for the SIGTERM case: acquire, announce, then sleep until the
#: parent kills us. The kernel releases the ``flock`` on process death.
SIGTERM_HOLDER_WORKER = r"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["LOCK_TEST_REPO_ROOT"])
sys.path.insert(0, os.environ["LOCK_TEST_SKILLS_ROOT"])

from shared.work_item_lock import WorkItemLock

lock = WorkItemLock(
    os.environ["LOCK_TEST_ITEM_ID"],
    agent=os.environ["LOCK_TEST_AGENT"],
    session_id=os.environ["LOCK_TEST_SESSION"],
    worklog_dir=Path(os.environ["LOCK_TEST_WORKLOG_DIR"]),
)
result = lock.acquire()
print(json.dumps({"acquired": result.acquired, "pid": os.getpid()}), flush=True)
time.sleep(120)
"""


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_lock_token_env(monkeypatch):
    """Keep every test hermetic: no inherited driver/child lock token."""
    monkeypatch.delenv(ENV_LOCK_TOKEN, raising=False)


@pytest.fixture
def worklog_dir(tmp_path: Path) -> Path:
    """A temporary worklog store directory (``<project>/.worklog``)."""
    return tmp_path / ".worklog"


def _locks_dir(worklog_dir: Path) -> Path:
    return worklog_dir / "locks"


def _lock_path(worklog_dir: Path, item_id: str = ITEM_ID) -> Path:
    return _locks_dir(worklog_dir) / f"child-{item_id}.lock"


def _read_metadata(path: Path) -> dict:
    """Read the lock file's owner metadata as written by the holder."""
    return json.loads(path.read_text(encoding="utf-8"))


def _make_lock(
    worklog_dir: Path, *, agent: str = "implement", session: str = "sess"
) -> WorkItemLock:
    return WorkItemLock(
        ITEM_ID,
        agent=agent,
        session_id=session,
        worklog_dir=worklog_dir,
    )


def _reaped_pid() -> int:
    """Return the pid of an already-exited (and reaped) process.

    A reaped pid is reliably dead for ``os.kill(pid, 0)`` lookups, which is
    what stale-holder detection must use. Pid reuse between the reaped child
    and the acquire is vanishingly unlikely on this host.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


# ---------------------------------------------------------------------------
# AC1 — non-blocking acquire records owner metadata
# ---------------------------------------------------------------------------


def test_lock_file_lives_under_worklog_locks_dir(worklog_dir: Path):
    """The lock file path is ``<worklog>/locks/child-<id>.lock``."""
    lock = _make_lock(worklog_dir)
    result = lock.acquire()
    try:
        assert result.acquired is True
        assert result.path == _lock_path(worklog_dir)
        assert result.path.parent == _locks_dir(worklog_dir)
        assert result.path.is_file()
    finally:
        lock.release()


def test_acquire_records_owner_metadata(worklog_dir: Path):
    """AC1: a free lock is acquired and the file records the owner metadata."""
    lock = _make_lock(worklog_dir, agent="implement", session="sess-owner")
    result = lock.acquire()
    try:
        assert result.acquired is True
        assert result.reentrant is False
        assert result.reclaimed is None

        metadata = _read_metadata(result.path)
        missing = METADATA_KEYS - set(metadata)
        assert not missing, f"lock file metadata is missing keys: {sorted(missing)}"
        assert metadata["agent"] == "implement"
        assert metadata["pid"] == os.getpid()
        assert metadata["session_id"] == "sess-owner"
        assert isinstance(metadata["token"], str) and metadata["token"]

        started_at = datetime.fromisoformat(metadata["started_at"])
        assert started_at.tzinfo is not None, "started_at must be timezone-aware ISO-8601"
        skew = abs((datetime.now(timezone.utc) - started_at).total_seconds())
        assert skew < 300, f"started_at is not the acquisition time (skew {skew}s)"

        # The result surfaces exactly what was persisted for this holder.
        assert result.owner == metadata
        assert result.token == metadata["token"]
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# AC2 — second acquire fails fast, read-only
# ---------------------------------------------------------------------------


def test_second_acquire_is_held_without_raising_or_writing(worklog_dir: Path):
    """AC2: contention returns a held result, writes nothing, and waits not."""
    first = _make_lock(worklog_dir, agent="implement", session="sess-first")
    first_result = first.acquire()
    try:
        path = first_result.path
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        second = _make_lock(worklog_dir, agent="intruder", session="sess-second")
        started = time.monotonic()
        second_result = second.acquire()
        elapsed = time.monotonic() - started

        assert second_result.acquired is False
        assert second_result.reentrant is False
        assert second_result.held_by == first_result.owner
        assert second_result.held_by["agent"] == "implement"
        assert second_result.held_by["pid"] == os.getpid()
        assert second_result.held_by["token"] == first_result.token

        assert path.read_bytes() == before_bytes, (
            "a losing acquire must not write the lock file"
        )
        assert path.stat().st_mtime_ns == before_mtime, (
            "a losing acquire must not touch the lock file"
        )
        assert elapsed < FAIL_FAST_BOUND_SECONDS, f"contention waited {elapsed:.2f}s; must fail fast"

        # Releasing a handle that never acquired must not free the real holder.
        second.release()
        third_result = _make_lock(worklog_dir, agent="third", session="sess-third").acquire()
        assert third_result.acquired is False, "loser's release must not drop the holder's lock"
        assert third_result.held_by["token"] == first_result.token
    finally:
        first.release()


def test_context_manager_raises_when_lock_is_held(worklog_dir: Path):
    """Fail closed: entering a held lock raises instead of running the block."""
    holder = _make_lock(worklog_dir, agent="holder", session="sess-holder")
    holder_result = holder.acquire()
    try:
        intruder = _make_lock(worklog_dir, agent="intruder", session="sess-intruder")
        with pytest.raises(WorkItemLockHeld) as excinfo:
            with intruder:
                pytest.fail("a held lock must never be entered")

        held = excinfo.value.result
        assert held.acquired is False
        assert held.held_by["agent"] == "holder"
        assert _read_metadata(holder_result.path) == holder_result.owner
    finally:
        holder.release()


# ---------------------------------------------------------------------------
# AC3 — context-manager release (normal exit and exception)
# ---------------------------------------------------------------------------


def test_context_manager_releases_on_normal_exit(worklog_dir: Path):
    """AC3: leaving the ``with`` block frees the lock for a fresh acquire."""
    with _make_lock(worklog_dir, agent="implement", session="sess-ctx") as result:
        assert result.acquired is True
        assert _read_metadata(result.path)["session_id"] == "sess-ctx"

    fresh = _make_lock(worklog_dir, agent="implement", session="sess-next")
    fresh_result = fresh.acquire()
    try:
        assert fresh_result.acquired is True
        assert fresh_result.reentrant is False
        assert _read_metadata(fresh_result.path)["session_id"] == "sess-next"
    finally:
        fresh.release()


def test_context_manager_releases_on_exception(worklog_dir: Path):
    """AC3: an exception inside the block still releases the lock."""
    with pytest.raises(RuntimeError, match="boom"):
        with _make_lock(worklog_dir, agent="implement", session="sess-exc") as result:
            assert result.acquired is True
            raise RuntimeError("boom")

    fresh = _make_lock(worklog_dir, agent="implement", session="sess-after-exc")
    fresh_result = fresh.acquire()
    try:
        assert fresh_result.acquired is True
        assert fresh_result.held_by is None
    finally:
        fresh.release()


# ---------------------------------------------------------------------------
# AC4 — SIGTERM-killed holder releases the kernel lock
# ---------------------------------------------------------------------------


def test_sigterm_killed_holder_releases_kernel_lock(worklog_dir: Path):
    """AC4: a SIGTERM-killed holder's lock is reclaimed without file cleanup."""
    env = dict(os.environ)
    env.update(
        {
            "LOCK_TEST_REPO_ROOT": str(REPO_ROOT),
            "LOCK_TEST_SKILLS_ROOT": str(_SKILLS_ROOT_FOR_TESTS),
            "LOCK_TEST_ITEM_ID": ITEM_ID,
            "LOCK_TEST_AGENT": "killed-holder",
            "LOCK_TEST_SESSION": "sess-sigterm",
            "LOCK_TEST_WORKLOG_DIR": str(worklog_dir),
        }
    )
    env.pop(ENV_LOCK_TOKEN, None)
    path = _lock_path(worklog_dir)

    proc = subprocess.Popen(
        [sys.executable, "-c", SIGTERM_HOLDER_WORKER],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        readable, _, _ = select.select([proc.stdout], [], [], 60)
        assert readable, "holder worker did not report acquisition within 60s"
        announcement = proc.stdout.readline()
        if not announcement.strip():
            proc.wait(timeout=30)
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            pytest.fail(f"holder worker never acquired the lock: {stderr}")
        payload = json.loads(announcement)
        assert payload["acquired"] is True
        holder_pid = payload["pid"]

        # The holder really does own the lock while it lives.
        assert _read_metadata(path)["pid"] == holder_pid
        intruder = _make_lock(worklog_dir, agent="intruder", session="sess-before-term")
        intruder_result = intruder.acquire()
        assert intruder_result.acquired is False
        assert intruder_result.held_by["pid"] == holder_pid

        proc.terminate()  # SIGTERM
        proc.wait(timeout=60)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=60)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

    # Kernel-guaranteed release: acquire succeeds with no manual file cleanup.
    assert path.is_file(), "the lock file must not need manual deletion after a SIGTERM"
    successor = _make_lock(worklog_dir, agent="successor", session="sess-after-term")
    result = successor.acquire()
    try:
        assert result.acquired is True
        assert result.path == path
        assert result.owner["pid"] == os.getpid()
    finally:
        successor.release()


# ---------------------------------------------------------------------------
# AC5 — stale dead-pid reclamation with a warning
# ---------------------------------------------------------------------------


def test_stale_dead_pid_lock_is_reclaimed_with_warning(worklog_dir: Path, caplog):
    """AC5: a dead recorded owner is reclaimed and named in a logged warning."""
    dead_pid = _reaped_pid()
    stale_metadata = {
        "agent": "crashed-driver",
        "pid": dead_pid,
        "session_id": "sess-dead",
        "started_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        "token": "dead-holder-token",
    }
    locks = _locks_dir(worklog_dir)
    locks.mkdir(parents=True)
    path = _lock_path(worklog_dir)
    path.write_text(json.dumps(stale_metadata), encoding="utf-8")

    lock = _make_lock(worklog_dir, agent="implement", session="sess-reclaimer")
    with caplog.at_level(logging.WARNING, logger=LOCK_LOGGER_NAME):
        result = lock.acquire()
    try:
        assert result.acquired is True, "a dead holder must never block a live acquirer"
        assert result.reclaimed == stale_metadata

        metadata = _read_metadata(path)
        assert metadata["pid"] == os.getpid()
        assert metadata["agent"] == "implement"
        assert metadata["token"] != "dead-holder-token"

        warnings = [
            record
            for record in caplog.records
            if record.name == LOCK_LOGGER_NAME and record.levelno >= logging.WARNING
        ]
        assert warnings, (
            "reclamation must log a warning via the shared.work_item_lock logger; "
            f"records seen: {[(r.name, r.levelname) for r in caplog.records]}"
        )
        assert any(
            "crashed-driver" in record.getMessage() and str(dead_pid) in record.getMessage()
            for record in warnings
        ), f"warning must name the reclaimed holder: {[r.getMessage() for r in warnings]}"
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# AC6 — token-based self-ownership (re-entrancy, never a false block)
# ---------------------------------------------------------------------------


def test_env_token_name_is_the_documented_lock_token():
    """AC6: ownership is propagated by the pinned env var name."""
    assert ENV_LOCK_TOKEN == "IMPLEMENT_CHILD_LOCK_TOKEN"


def test_matching_token_acquire_is_reentrant_noop(worklog_dir: Path, monkeypatch):
    """AC6: the token in the lock file grants a re-entrant, non-blocking no-op."""
    holder = _make_lock(worklog_dir, agent="drive", session="sess-drive")
    holder_result = holder.acquire()
    try:
        monkeypatch.setenv(ENV_LOCK_TOKEN, holder_result.token)
        child = _make_lock(worklog_dir, agent="child", session="sess-child")
        child_result = child.acquire()
        try:
            assert child_result.acquired is True
            assert child_result.reentrant is True
            assert child_result.held_by == holder_result.owner
            # Re-entrancy is a no-op: the recorded owner is unchanged.
            assert _read_metadata(holder_result.path) == holder_result.owner
        finally:
            child.release()

        # Releasing the re-entrant handle must leave the real holder's lock intact.
        # The probe carries no ownership token, so it must be refused.
        monkeypatch.delenv(ENV_LOCK_TOKEN, raising=False)
        probe = _make_lock(worklog_dir, agent="probe", session="sess-probe")
        probe_result = probe.acquire()
        assert probe_result.acquired is False
        assert probe_result.reentrant is False
        assert probe_result.held_by["agent"] == "drive"
        assert probe_result.held_by["token"] == holder_result.token
    finally:
        holder.release()


def test_mismatched_token_does_not_grant_ownership(worklog_dir: Path, monkeypatch):
    """AC6: any token other than the recorded one is still a holder."""
    holder = _make_lock(worklog_dir, agent="drive", session="sess-drive")
    holder_result = holder.acquire()
    try:
        monkeypatch.setenv(ENV_LOCK_TOKEN, "not-the-holder-token")
        impostor = _make_lock(worklog_dir, agent="impostor", session="sess-impostor")
        result = impostor.acquire()
        assert result.acquired is False
        assert result.reentrant is False
        assert result.held_by["agent"] == "drive"
        assert result.held_by["token"] == holder_result.token
    finally:
        holder.release()
