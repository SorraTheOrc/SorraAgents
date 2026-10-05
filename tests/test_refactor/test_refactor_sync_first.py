#!/usr/bin/env python3
"""Tests: refactor.py syncs the worklog before any worklog interaction.

Covers WL-0MUAD8U24001ZK5K AC1 (sync is the first worklog operation, before
any read/comment/work-item creation), AC4 (a failed sync is non-fatal) and
AC6 (``--no-sync`` skips the sync). The ordering is asserted at the public
seam: ``_run_with_sync`` invokes the sync before the pipeline runner that
performs the worklog reads/creations.
"""  # noqa: EXE001
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from refactor.scripts import refactor as refactor_mod


def _report() -> dict:
    """A minimal, JSON-serialisable refactor report."""
    return {
        "success": True,
        "session_files": {"changed": [], "untracked": [], "all_files": []},
        "auto_fix": {"fixes_applied": False, "fixed_findings": []},
        "smells_detected": [],
        "pre_existing_smells": [],
        "session_introduced_smells": [],
        "remediation": {
            "work_items_created": [],
            "comments_injected": 0,
            "comment_errors": 0,
        },
        "summary": {
            "files_analyzed": 0,
            "auto_fixed": 0,
            "total_smells": 0,
            "session_introduced": 0,
            "pre_existing": 0,
            "work_items_created": 0,
            "comments_injected": 0,
        },
    }


class TestSyncOrdering:
    """AC1: the sync runs before the pipeline (reads, comments, creation)."""

    def test_sync_runs_before_pipeline(self):
        order: list[str] = []

        def fake_sync(**_kwargs):
            order.append("sync")
            return {"status": "synced"}

        def fake_pipeline():
            order.append("pipeline")
            return _report()

        refactor_mod._run_with_sync(
            fake_pipeline, no_sync=False, sync_fn=fake_sync
        )

        assert order == ["sync", "pipeline"]

    def test_main_syncs_before_building_pipeline(self):
        order: list[str] = []

        def fake_sync(**_kwargs):
            order.append("sync")
            return {"status": "synced"}

        def fake_pipeline_runner():
            order.append("pipeline")
            return _report()

        original_sync = refactor_mod.sync_worklog
        original_builder = refactor_mod._build_pipeline_runner
        refactor_mod.sync_worklog = fake_sync
        refactor_mod._build_pipeline_runner = lambda _args: fake_pipeline_runner
        try:
            rc = refactor_mod._main(["--json"])
        finally:
            refactor_mod.sync_worklog = original_sync
            refactor_mod._build_pipeline_runner = original_builder

        assert rc == 0
        assert order == ["sync", "pipeline"]


class TestFailureTolerance:
    """AC4: a failed/skipped sync never blocks the pipeline."""

    def test_failed_sync_does_not_block_pipeline(self):
        ran: list[str] = []

        def failing_sync(**_kwargs):
            return {"status": "failed", "error": "offline"}

        def pipeline():
            ran.append("pipeline")
            return _report()

        result = refactor_mod._run_with_sync(pipeline, sync_fn=failing_sync)

        assert ran == ["pipeline"]
        assert result["success"] is True


class TestNoSyncEscapeHatch:
    """AC6: ``--no-sync`` is accepted and threaded through to the helper."""

    def test_parse_args_accepts_no_sync(self):
        args = refactor_mod.parse_args(["--no-sync"])
        assert args.no_sync is True

    def test_no_sync_is_forwarded_to_sync_helper(self):
        captured: dict = {}

        def fake_sync(**kwargs):
            captured.update(kwargs)
            return {"status": "skipped", "reason": "no_sync"}

        refactor_mod._run_with_sync(lambda: _report(), no_sync=True, sync_fn=fake_sync)

        assert captured["no_sync"] is True

    def test_main_accepts_no_sync_flag(self):
        captured: dict = {}

        def fake_sync(**kwargs):
            captured.update(kwargs)
            return {"status": "skipped", "reason": "no_sync"}

        original_sync = refactor_mod.sync_worklog
        original_builder = refactor_mod._build_pipeline_runner
        refactor_mod.sync_worklog = fake_sync
        refactor_mod._build_pipeline_runner = lambda _args: _report
        try:
            rc = refactor_mod._main(["--no-sync", "--json"])
        finally:
            refactor_mod.sync_worklog = original_sync
            refactor_mod._build_pipeline_runner = original_builder

        assert rc == 0
        assert captured["no_sync"] is True
