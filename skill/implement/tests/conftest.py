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

Lock-timeout neutralisation (SA-0MVY0G5EH000LIMB): when this package's suite
is itself run by ``implement.py finish`` the gate exports
``IMPLEMENT_FINISH_LOCK_TIMEOUT`` / ``IMPLEMENT_START_LOCK_TIMEOUT`` so real
finish calls can wait out a concurrent holder. A test that takes the main-checkout
lock itself then calls ``phase_finish`` (which calls ``_finish_lock_timeout`` →
``_resolve_lock_timeout`` → ``os.environ.get``) inherits that ambient value and
sleeps for the full duration before refusing — because the lock it is waiting for
is held by the test's own fd, released only at test teardown. The inherited
timeout therefore turns a ~0.05 s test into a multi-hour hang (25 s with a
25-second timeout, verified).

The complete neutralisation set is the two lock-wait env vars read through
``_resolve_lock_timeout()`` in ``implement.py``:
``IMPLEMENT_FINISH_LOCK_TIMEOUT`` and ``IMPLEMENT_START_LOCK_TIMEOUT``.

Audited 2026-11-08: no test in ``skill/implement/tests/`` intentionally sets
these variables as test inputs (they represent caller policy, never test
inputs); ``IMPLEMENT_DRIVE_CHILD_TIMEOUT`` is a per-child session timeout (not
a lock wait) and is unread by tests; ``TEST_LOCK_TIMEOUT`` belongs to
``skill/test/scripts/run_tests.py`` and is unreachable from here (the gate
suite is stubbed). Child-lock test files exercise the per-work-item child lock
and patch ``_sync_parent_branch``, so they never contend the main-checkout lock.

Where a test *does* need a specific timeout value it sets it explicitly within
the test body, after the fixture has run.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_git_index_file(monkeypatch):
    """Remove an ambient ``GIT_INDEX_FILE`` for the duration of each test."""
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_lock_timeout(monkeypatch):
    """Neutralise ambient lock-wait timeouts for every test.

    Prevents ``IMPLEMENT_FINISH_LOCK_TIMEOUT`` / ``IMPLEMENT_START_LOCK_TIMEOUT``
    exported by an outer ``implement.py finish`` from poisoning test execution.
    See the module docstring for the full mechanism (SA-0MVY0G5EH000LIMB).
    """
    monkeypatch.delenv("IMPLEMENT_FINISH_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("IMPLEMENT_START_LOCK_TIMEOUT", raising=False)


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
