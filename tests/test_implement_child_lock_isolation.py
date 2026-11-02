"""Per-work-item child-lock store isolation for implement tests.

``phase_start`` takes a **real** per-work-item ``flock`` at
``<worklog>/locks/child-<id>.lock`` before any of its test-mocked steps. The
root-level tests drive it with synthetic ids (``SA-TEST``, ``SA-TEST123``,
``SA-TEST456``, …) whose ``SA`` prefix resolves to this repository's own
worklog store, so an un-isolated suite wrote real lock files into the
developer's ``.worklog/locks`` — and two concurrent pytest processes raced
them, one failing with "Work item SA-TEST is held by implement@pid <n>"
(SA-0MVWJ6U3J003TLRZ).

``tests/conftest.py`` sets ``IMPLEMENT_CHILD_LOCK_DIR`` to a per-test
throwaway directory for every test in this package (read per call, so it also
covers modules that load ``implement.py`` by file path). This module pins the
convention at the observable resolution point: the lock store must resolve to
the isolated directory, never the live worklog store.
"""

from __future__ import annotations

import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_child_lock_store_resolves_to_the_isolated_directory(
    isolated_implement_child_lock_store,
):
    """``_child_lock_dir`` honours the override the conftest installs."""
    from skill.implement.scripts import implement

    resolved = implement._child_lock_dir("SA-TEST")

    assert resolved == isolated_implement_child_lock_store
    assert resolved != _REPO_ROOT / ".worklog"
    assert resolved is not None and resolved.is_dir()


def test_acquired_child_lock_is_never_written_to_the_live_worklog_store(
    isolated_implement_child_lock_store,
):
    """A real acquisition lands in the isolated store, not the live one."""
    from skill.implement.scripts import implement

    item_id = f"SA-ISO{uuid.uuid4().hex[:8].upper()}"
    lock_name = f"child-{item_id}.lock"

    lock = implement._new_child_lock(item_id)
    assert lock.path == isolated_implement_child_lock_store / "locks" / lock_name

    result = lock.acquire()
    try:
        assert result.acquired is True, result
        assert lock.path.is_file(), "the isolated store must hold the lock file"
        assert not (_REPO_ROOT / ".worklog" / "locks" / lock_name).exists(), (
            "a real lock acquisition must never write into the live worklog "
            "store — that is what let concurrent pytest processes race each "
            "other's synthetic ids"
        )
    finally:
        lock.release()
