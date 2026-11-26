"""Fixture isolation for the implement skill's own tests.

When this package's suite is itself run by ``implement.py finish`` the gate
exports ``GIT_INDEX_FILE`` pointing at *its* temporary index
(SA-0MVQHVGFR006W8LG). Every test here builds a hermetic fixture repository
and drives the gate itself, so inheriting that value would redirect fixture
``git`` commands (and ``test_cache`` fingerprints, which intentionally keep
``GIT_INDEX_FILE`` — F3, SA-0MVQHV9Y3001RNNW) at a foreign repository's index.

Each finish-gate test that needs an ambient index sets it itself
(``would_be_commit_index`` exports it and restores the previous value), so the
ambient value is removed for every test in this package.

Lock-timeout neutralisation (SA-0MVY0G5EH000LIMB, extended by
SA-0MWFJS65O005JMJF): when this package's suite is itself run by
``implement.py finish`` the gate exports
``IMPLEMENT_FINISH_LOCK_TIMEOUT`` / ``IMPLEMENT_START_LOCK_TIMEOUT`` so real
finish calls can wait out a concurrent holder. A test that takes the main-checkout
lock itself then calls ``phase_finish`` (which calls ``_finish_lock_timeout`` →
``_resolve_lock_timeout`` → ``os.environ.get``) inherits that ambient value and
sleeps for the full duration before refusing — because the lock it is waiting for
is held by the test's own fd, released only at test teardown. The inherited
timeout therefore turns a ~0.05 s test into a multi-hour hang (25 s with a
25-second timeout, verified).

Since ``finish`` now has a **positive** default bounded wait (15 min), simply
deleting the variables would be worse: every fail-fast refusal test would inherit
the production default and sleep for it. The fixture therefore *sets* both
variables to ``"0"`` — the explicit fail-fast/no-wait policy — so the
neutralisation is independent of the production default. A test that needs a
specific wait sets the variable itself in the test body; monkeypatch is LIFO, so
the in-test ``setenv`` wins after the autouse fixture and is undone at teardown.

The complete neutralisation set is the two lock-wait env vars read through
``_resolve_lock_timeout()`` in ``implement.py``:
``IMPLEMENT_FINISH_LOCK_TIMEOUT`` and ``IMPLEMENT_START_LOCK_TIMEOUT``.

Drive-marker neutralisation (SA-0MWJJTNT9002M2ME): when this package's suite
is itself run by a **driven child session's** finish-gate full-suite run, the
session's ``IMPLEMENT_DRIVE_ACTIVE=1`` marker is inherited into the suite
subprocess environment (``run_tests.py`` preserves every env var except git
repository overrides). ``phase_drive``'s recursion guard
(``implement.py:18404``, :data:`DRIVE_RECURSION_ENV`) then refuses to run, so
the drive/guard tests that invoke ``phase_drive`` directly break with a
``KeyError: 'failed_child'`` — they are red purely because of *who ran the
suite*, not because of any code change. The suite must be deterministic under
either ambient state (the parent-driven full-suite gate is exactly where a
real driven chain's audit verdict is computed). ``IMPLEMENT_KEEP_WORKTREE`` is
the sibling driven-session marker read in ``_should_defer_worktree_removal``;
finish-gate tests assert the deferral decision themselves via ``--keep-worktree``
/ explicit ``setenv`` and must not inherit an ambient value either.

Tests that deliberately simulate a driven context set the marker themselves
inside the test body (``monkeypatch.setenv(implement.DRIVE_RECURSION_ENV,
"1")``) — monkeypatch is LIFO, so the in-test ``setenv`` wins after the
autouse ``delenv`` and is undone at teardown, never fighting the fixture.

Audited 2026-11-08: no test in ``skill/implement/tests/`` intentionally sets
these variables as test inputs (they represent caller policy, never test
inputs); ``IMPLEMENT_DRIVE_CHILD_TIMEOUT`` is a per-child session timeout (not
a lock wait) and is unread by tests; ``TEST_LOCK_TIMEOUT`` belongs to
``skill/test/scripts/run_tests.py`` and is unreachable from here (the gate
suite is stubbed). Child-lock test files exercise the per-work-item child lock
and patch ``_sync_parent_branch``, so they never contend the main-checkout lock.

Shared main-checkout lock (SA-0MWS8ICOC001DZX1): the finish-gate harnesses
(``test_finish_deferred_worktree_removal.py``,
``test_finish_no_diff_status.py``,
``test_finish_plain_flow_keep_worktree.py``) all pin their state's
``repo_root`` to the shared synthetic path ``/tmp/repo-root``, so they resolve
**one** main-checkout lock file and two files running concurrently race it —
the loser's ``phase_finish`` returned the lock refusal before its assertions
ran. Those harnesses now stub the lock boundary (the pattern already used by
``test_finish_interruption_recovery.py``); the lock's own behaviour stays
covered by ``test_finish_main_checkout_lock.py``. The
:func:`held_main_checkout_lock` fixture manufactures the contention for a
single test, so each harness proves it is isolated from a held lock.

Where a test *does* need a specific timeout value it sets it explicitly within
the test body, after the fixture has run. A test that needs the **production
default** (rather than neutralisation) deletes the variable in the test body
(``monkeypatch.delenv``) before reading it.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

import pytest

#: The synthetic main-checkout root the finish-gate harnesses pin their
#: ``ImplementState.repo_root`` to (``_mocked_phase_finish`` in
#: ``test_finish_deferred_worktree_removal.py``,
#: ``test_finish_no_diff_status.py`` and
#: ``test_finish_plain_flow_keep_worktree.py``). Because every harness uses the
#: same literal path, they all resolve the *same* main-checkout lock file
#: (SA-0MWS8ICOC001DZX1).
MAIN_CHECKOUT_LOCK_FIXTURE_ROOT = "/tmp/repo-root"


@pytest.fixture
def held_main_checkout_lock():
    """Hold the real finish lock at the shared synthetic root (SA-0MWS8ICOC001DZX1).

    ``phase_finish`` takes an exclusive ``flock`` on
    ``<repo_root>/.worklog/locks/main-checkout.lock`` and fails fast when it is
    held (the autouse neutralisation above pins
    ``IMPLEMENT_FINISH_LOCK_TIMEOUT=0``). The finish-gate harnesses in this
    package all pin ``repo_root`` to :data:`MAIN_CHECKOUT_LOCK_FIXTURE_ROOT`,
    so two test files running concurrently contend on the same lock file and
    the loser's ``phase_finish`` returns the lock refusal before its own
    assertions run. A test requesting this fixture manufactures exactly that
    contention for its whole duration: a harness that isolates the lock
    boundary passes, one that does not fails loudly.

    The lock file is shared with the host, so a lock already held by someone
    else counts as the same contention condition rather than an error; only a
    lock this fixture acquired itself is released on teardown.

    Returns:
        The lock path held for the duration of the test.
    """
    from skill.implement.scripts import implement

    lock_path = implement._main_checkout_lock_path(MAIN_CHECKOUT_LOCK_FIXTURE_ROOT)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            acquired = False
        else:
            acquired = True
        try:
            yield lock_path
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@pytest.fixture(autouse=True)
def _no_ambient_git_index_file(monkeypatch):
    """Remove an ambient ``GIT_INDEX_FILE`` for the duration of each test."""
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_lock_timeout(monkeypatch):
    """Neutralise ambient lock-wait timeouts for every test.

    Prevents ``IMPLEMENT_FINISH_LOCK_TIMEOUT`` / ``IMPLEMENT_START_LOCK_TIMEOUT``
    exported by an outer ``implement.py finish`` from poisoning test execution,
    and pins both to the explicit ``"0"`` (no-wait) policy so the positive
    production default can never turn a fail-fast refusal test into a
    15-minute sleep (SA-0MWFJS65O005JMJF). See the module docstring for the
    full mechanism (SA-0MVY0G5EH000LIMB).
    """
    monkeypatch.setenv("IMPLEMENT_FINISH_LOCK_TIMEOUT", "0")
    monkeypatch.setenv("IMPLEMENT_START_LOCK_TIMEOUT", "0")


@pytest.fixture(autouse=True)
def _no_ambient_drive_marker(monkeypatch):
    """Remove ambient driven-session markers for every test (SA-0MWJJTNT9002M2ME).

    A driven child session's finish-gate full-suite run inherits
    ``IMPLEMENT_DRIVE_ACTIVE=1`` into this suite's environment, which makes
    ``phase_drive``'s recursion guard refuse any direct drive test (the
    ``TestDriveRecordsSpawnFailureEvidence`` failures). Autouse removal makes
    the suite deterministic regardless of who runs it; tests that simulate a
    driven context re-set the marker inside the test body (LIFO, so they win).
    See the module docstring for the incident and mechanism.
    """
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)
    monkeypatch.delenv("IMPLEMENT_KEEP_WORKTREE", raising=False)


@pytest.fixture(autouse=True)
def isolated_implement_child_lock_store(tmp_path, monkeypatch):
    """Point ``implement.py``'s per-work-item lock store at a throwaway dir.

    ``phase_start`` takes a **real** per-work-item ``flock`` before any of
    its test-mocked steps, under ``<worklog>/locks/child-<id>.lock``. The
    synthetic ids used by these tests (``SA-TEST`` and friends) resolve to
    the repository's own worklog store, so an un-isolated suite wrote real
    lock files into the developer's ``.worklog/locks`` — and two concurrent
    pytest processes then raced the same id and one failed with "Work item
    SA-TEST is held by implement@pid <n>" (SA-0MVWJ6U3J003TLRZ).

    ``IMPLEMENT_CHILD_LOCK_DIR`` isolates every test here to its own store;
    reading it per call also covers the tests that load ``implement.py`` by
    file path instead of importing it. ``test_phase_start_child_lock.py``
    patches ``_child_lock_dir`` itself where it asserts the lock path, so its
    throwaway store still wins.

    Returns:
        The isolated worklog store directory for the current test.
    """
    store = tmp_path / "isolated-worklog"
    (store / "locks").mkdir(parents=True)
    monkeypatch.setenv("IMPLEMENT_CHILD_LOCK_DIR", str(store))
    return store


def build_child_worktree_fixture(
    worktree_root: Path,
    *,
    child_id: str = "SA-CHILD",
    title: str = "Child",
    status: str = "open",
    stage: str = "intake_complete",
    sort_index: int = 1,
    parent_id: str = "SA-PARENT",
    write_state_file: bool = True,
) -> tuple[dict[str, Any], Path | None]:
    """Build a child work-item dict plus a matching worktree state file.

    Reused by ``_classify_child`` / ``phase_parent`` fixture slices: a real
    ``.implement_state.json`` is written into ``<worktree_root>/wl-<id>/`` so
    the fixture exercises the same state-discovery path production uses,
    instead of hand-building an ad-hoc dict per test.

    Args:
        worktree_root: Directory under which the child's worktree is created.
        child_id: The child work-item id.
        title: The child work-item title.
        status: Lifecycle status (``open``, ``in-progress``, ``completed`` …).
        stage: Workflow stage (``intake_complete``, ``in_review`` …).
        sort_index: ``sortIndex`` used for deterministic ordering.
        parent_id: The parent work-item id (recorded for readability).
        write_state_file: When ``True`` (default) write
            ``.implement_state.json`` into the child worktree.

    Returns:
        ``(child_dict, state_file_path)`` — the dict is the shape
        ``wl show --children`` yields; ``state_file_path`` is ``None`` when
        ``write_state_file`` is ``False``.
    """
    from skill.implement.scripts.implement import STATE_FILE_NAME, ImplementState

    worktree = worktree_root / f"wl-{child_id}"
    worktree.mkdir(parents=True, exist_ok=True)
    child = {
        "id": child_id,
        "title": title,
        "status": status,
        "stage": stage,
        "sortIndex": sort_index,
        "parentId": parent_id,
    }
    state_path: Path | None = None
    if write_state_file:
        state = ImplementState(
            work_item_id=child_id,
            worktree_path=str(worktree),
            repo_root=str(worktree_root.parent),
            parent_branch="dev",
        )
        state_path = worktree / STATE_FILE_NAME
        state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    return child, state_path


@pytest.fixture
def child_worktree_fixture(tmp_path):
    """Factory fixture wrapping :func:`build_child_worktree_fixture`.

    Later ``_classify_child`` / ``phase_parent`` slices can request this
    fixture and call the returned callable to materialise a child dict and
    its worktree state file in one step.
    """

    def _build(**kwargs: Any) -> tuple[dict[str, Any], Path | None]:
        return build_child_worktree_fixture(tmp_path, **kwargs)

    return _build

