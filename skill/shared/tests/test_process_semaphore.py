"""Unit tests for skill/shared/process_semaphore.py (SA-0MSAK2P3J0065POO).

Direct API-level tests complementing the cross-process guard suite
(skill/shared/tests/test_concurrency_guard.py). Focus on the public
Semaphore API: constructor resolution, acquire/release semantics, env-var
ceiling, timeout/fail-fast, and cleanup. Cross-process behaviour is covered
by the guard suite.
"""


import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT_FOR_TESTS = REPO_ROOT / "skill"
if str(_SKILLS_ROOT_FOR_TESTS) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT_FOR_TESTS))
import pytest
from shared.process_semaphore import (
    DEFAULT_MAX_WORKERS,
    ENV_LOCK_DIR,
    ENV_MAX_WORKERS,
    Semaphore,
)


@pytest.fixture(autouse=True)
def _isolate_lock_dir(tmp_path, monkeypatch):
    """Point every test at a unique lock dir and clear env overrides."""
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "locks"))
    monkeypatch.delenv(ENV_MAX_WORKERS, raising=False)


def test_default_max_workers():
    """No explicit arg and no env var -> documented default."""
    sem = Semaphore("unit-default")
    assert sem.max_workers == DEFAULT_MAX_WORKERS
    assert sem.max_workers == 5


def test_explicit_max_workers_wins_over_env(monkeypatch):
    """Explicit arg takes priority over AUDIT_MAX_CONCURRENCY."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "1")
    sem = Semaphore("unit-explicit", max_workers=4)
    assert sem.max_workers == 4


def test_env_ceiling_respected(monkeypatch):
    """AUDIT_MAX_CONCURRENCY overrides the default ceiling."""
    monkeypatch.setenv(ENV_MAX_WORKERS, "3")
    sem = Semaphore("unit-env")
    assert sem.max_workers == 3


def test_invalid_max_workers_rejected():
    """max_workers < 1 must raise ValueError."""
    with pytest.raises(ValueError):
        Semaphore("unit-bad", max_workers=0)


def test_acquire_and_release_roundtrip():
    """acquire() returns True and release() is idempotent."""
    sem = Semaphore("unit-roundtrip", max_workers=1, timeout=5)
    assert sem.acquire() is True
    assert sem._held_fd is not None
    sem.release()
    assert sem._held_fd is None
    sem.release()  # second release is a no-op


def test_reentrant_acquire_is_noop():
    """acquire() while already held returns True without taking another slot."""
    sem = Semaphore("unit-reentrant", max_workers=1, timeout=5)
    with sem:
        assert sem.acquire() is True
    # One release from the context manager suffices.
    assert sem._held_fd is None


def test_context_manager_releases_on_success():
    """`with` block releases the slot on normal exit."""
    sem = Semaphore("unit-ctx", max_workers=1, timeout=5)
    with sem:
        assert sem._held_fd is not None
    assert sem._held_fd is None


def test_context_manager_releases_on_exception():
    """`with` block releases the slot even when an exception propagates."""
    sem = Semaphore("unit-ctx-exc", max_workers=1, timeout=5)
    with pytest.raises(RuntimeError), sem:
        raise RuntimeError("boom")
    assert sem._held_fd is None


def test_same_name_shares_slot_pool():
    """Two Semaphores with the same name share the same slot directory."""
    a = Semaphore("unit-shared", max_workers=2, timeout=5)
    b = Semaphore("unit-shared", max_workers=2, timeout=5)
    assert a._slot_dir == b._slot_dir
