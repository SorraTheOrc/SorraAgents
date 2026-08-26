"""Unit tests: the audit phase-checkpoint store (SA-0MT6EZUS9004FJ9T).

Defines the checkpoint contract that the audit runner integration relies on:

1. A single JSON checkpoint file per issue, keyed by ``issue_id`` +
   ``git_head`` sha so a resume NEVER reuses results from a different HEAD
   (stale-checkpoint safety — AC: "never produce an incorrect verdict by
   reusing stale partial results from a different git HEAD or work item").
2. ``mark_started`` / ``mark_completed`` record per-phase status with
   timestamps + durations so an interrupted run can report exactly which
   phase was in flight (clear timeout reporting).
3. Accumulated pipeline state (ac_results / child_results / phase2 flags)
   merges across completed phases; ``accumulated_state`` serves the resume.
4. ``force`` starts a fresh checkpoint (``--force`` must never re-verify
   from a partial result of an earlier run).
5. Graceful degradation: corrupt/unreadable files, unreadable dirs, or
   write failures warn and never raise — checkpointing is best-effort.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from audit.scripts.checkpoint_store import (
    CHECKPOINT_FILE_SUFFIX,
    CHECKPOINT_VERSION,
    ENV_CHECKPOINT_DIR,
    PHASE_CHILDREN,
    PHASE_LABELS,
    PHASE_PARENT,
    PHASE_PHASE2,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    CheckpointStore,
    resolve_checkpoint_dir,
)

HEAD_BASE = "a" * 40
HEAD_OTHER = "b" * 40


def _store(tmp_path: Path, issue_id="SA-1", git_head=HEAD_BASE, **kw):
    return CheckpointStore(issue_id, git_head, tmp_path, **kw)


class TestResolveCheckpointDir:
    """AC: checkpoint location is configurable and survives restarts."""

    def test_default_under_owning_worklog(self):
        owning = Path("/repo/owner")
        resolved = resolve_checkpoint_dir(owning)
        assert resolved == owning / ".worklog" / "audit-checkpoints"

    def test_explicit_dir_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(ENV_CHECKPOINT_DIR, "/from/env")
        resolved = resolve_checkpoint_dir(Path("/repo"), explicit_dir="/from/flag")
        assert resolved == Path("/from/flag")

    def test_env_used_when_no_explicit(self, monkeypatch):
        monkeypatch.setenv(ENV_CHECKPOINT_DIR, "/from/env")
        resolved = resolve_checkpoint_dir(Path("/repo"))
        assert resolved == Path("/from/env")

    def test_empty_explicit_disables(self):
        assert resolve_checkpoint_dir(Path("/repo"), explicit_dir="") is None

    def test_empty_env_disables(self, monkeypatch):
        monkeypatch.setenv(ENV_CHECKPOINT_DIR, "")
        assert resolve_checkpoint_dir(Path("/repo")) is None

    def test_none_when_no_owning_root(self):
        assert resolve_checkpoint_dir(None) is None

    def test_env_constant_defined(self):
        assert ENV_CHECKPOINT_DIR == "AUDIT_CHECKPOINT_DIR"


class TestCheckpointStoreFresh:
    """A store with no prior file is fresh (no resume)."""

    def test_missing_file_is_fresh(self, tmp_path):
        store = _store(tmp_path)
        assert store.is_resuming is False
        assert store.completed_phases() == []
        assert store.phase_status(PHASE_PARENT) == STATUS_PENDING
        assert store.accumulated_state() == {}

    def test_path_is_per_issue_file(self, tmp_path):
        store = _store(tmp_path, issue_id="SA-42")
        assert store.path() == tmp_path / f"SA-42{CHECKPOINT_FILE_SUFFIX}"

    def test_lowercase_head_normalized(self, tmp_path):
        store = _store(tmp_path, git_head=HEAD_BASE.upper())
        assert store.git_head == HEAD_BASE


class TestCheckpointStoreLifecycle:
    """mark_started / mark_completed write progress the next run can read."""

    def test_mark_started_writes_in_progress_marker(self, tmp_path):
        store = _store(tmp_path)
        store.mark_started(PHASE_PARENT)
        assert store.phase_status(PHASE_PARENT) == STATUS_IN_PROGRESS
        assert tmp_path.joinpath(store.path().name).exists()
        data = json.loads(store.path().read_text())
        assert data["phases"][PHASE_PARENT]["status"] == STATUS_IN_PROGRESS

    def test_mark_completed_records_elapsed_and_state(self, tmp_path):
        with mock.patch("audit.scripts.checkpoint_store.time.time",
                        side_effect=[100.0, 162.5]):
            store = _store(tmp_path)
            store.mark_started(PHASE_PARENT)
            store.mark_completed(PHASE_PARENT, {"ac_results": [{"v": "met"}]})
        assert store.phase_status(PHASE_PARENT) == STATUS_COMPLETED
        assert store.accumulated_state()["ac_results"] == [{"v": "met"}]
        entry = store._data["phases"][PHASE_PARENT]
        assert entry["elapsed_s"] == pytest.approx(62.5)

    def test_state_accumulates_across_phases(self, tmp_path):
        store = _store(tmp_path)
        store.mark_started(PHASE_PARENT)
        store.mark_completed(PHASE_PARENT, {"ac_results": ["parent"]})
        store.mark_started(PHASE_CHILDREN)
        store.mark_completed(
            PHASE_CHILDREN,
            {"child_results": ["child"], "ac_results": ["parent-updated"]},
        )
        state = store.accumulated_state()
        assert state["ac_results"] == ["parent-updated"]
        assert state["child_results"] == ["child"]

    def test_reopen_roundtrips_completed_phases(self, tmp_path):
        store = _store(tmp_path)
        store.mark_started(PHASE_PARENT)
        store.mark_completed(PHASE_PARENT, {"ac_results": [{"v": "met"}]})
        reopened = _store(tmp_path)
        assert reopened.is_resuming is True
        assert reopened.phase_status(PHASE_PARENT) == STATUS_COMPLETED
        assert reopened.accumulated_state()["ac_results"] == [{"v": "met"}]


class TestCheckpointStoreValidation:
    """Stale/foreign/corrupt checkpoints are never resumed."""

    def test_mismatched_issue_id_treated_fresh(self, tmp_path):
        store = _store(tmp_path, issue_id="SA-1")
        store.mark_completed(PHASE_PARENT, {"ac_results": []})
        other = _store(tmp_path, issue_id="SA-2")
        assert other.is_resuming is False

    def test_mismatched_git_head_treated_fresh(self, tmp_path):
        store = _store(tmp_path, git_head=HEAD_BASE)
        store.mark_completed(PHASE_PARENT, {"ac_results": []})
        other = _store(tmp_path, git_head=HEAD_OTHER)
        assert other.is_resuming is False
        assert other.completed_phases() == []

    def test_corrupt_file_treated_fresh(self, tmp_path):
        store = _store(tmp_path)
        store.path().write_text("{not json!!!", encoding="utf-8")
        fresh = _store(tmp_path)
        assert fresh.is_resuming is False
        assert fresh.completed_phases() == []

    def test_wrong_version_treated_fresh(self, tmp_path):
        store = _store(tmp_path)
        store.mark_completed(PHASE_PARENT, {"ac_results": []})
        data = json.loads(store.path().read_text())
        data["version"] = CHECKPOINT_VERSION + 1
        store.path().write_text(json.dumps(data), encoding="utf-8")
        fresh = _store(tmp_path)
        assert fresh.is_resuming is False

    def test_force_starts_fresh_ignoring_existing(self, tmp_path):
        store = _store(tmp_path)
        store.mark_completed(PHASE_PARENT, {"ac_results": ["old"]})
        forced = _store(tmp_path, force=True)
        assert forced.is_resuming is False
        assert forced.accumulated_state() == {}
        # An aggressive --force run overwrites the file on the first write
        forced.mark_started(PHASE_PARENT)
        assert forced.phase_status(PHASE_PARENT) == STATUS_IN_PROGRESS


class TestCheckpointStoreFailureReporting:
    """Clear reporting of what completed / what was interrupted."""

    def test_interrupted_phase_reported(self, tmp_path):
        store = _store(tmp_path)
        store.mark_started(PHASE_PARENT)
        store.mark_completed(PHASE_PARENT, {"ac_results": []})
        store.mark_started(PHASE_CHILDREN)  # killed here before completion
        reopened = _store(tmp_path)
        assert reopened.completed_phases() == [PHASE_PARENT]
        assert reopened.in_progress_phase() == PHASE_CHILDREN
        assert reopened.interrupted_phase() == PHASE_CHILDREN

    def test_summary_lists_phase_labels(self, tmp_path):
        store = _store(tmp_path)
        store.mark_started(PHASE_PARENT)
        store.mark_completed(PHASE_PARENT, {"ac_results": []})
        summary = store.summary()
        assert PHASE_LABELS[PHASE_PARENT] in summary
        assert "done" in summary


class TestCheckpointStoreGracefulDegradation:
    """Checkpointing failures warn and never raise (best-effort)."""

    def test_unreadable_write_warns_and_does_not_raise(
        self, tmp_path, capsys
    ):
        store = _store(tmp_path)
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        (blocked / "x").write_text("", encoding="utf-8")  # placeholder
        store2 = _store(blocked / "sub" / "dir", issue_id="SA-1")
        # A directory path that cannot be created (parent is a file).
        store2.dir = tmp_path / "not-a-dir"
        store2.dir.write_text("file", encoding="utf-8")
        store2.mark_completed(PHASE_PARENT, {"ac_results": []})  # no raise
        assert "Warning" in capsys.readouterr().err

    def test_clear_removes_file(self, tmp_path):
        store = _store(tmp_path)
        store.mark_completed(PHASE_PARENT, {"ac_results": []})
        assert store.path().exists()
        store.clear()
        assert not store.path().exists()
        # Clearing an already-missing file is a no-op.
        store.clear()