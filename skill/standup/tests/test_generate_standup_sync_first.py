#!/usr/bin/env python3
"""Tests: generate_standup.py syncs the worklog before fetching data.

Covers WL-0MUAD8U24001ZK5K AC2 (sync before the first ``wl next`` / ``wl list``
fetch), AC4 (a failed sync is non-fatal) and AC6 (``--no-sync`` escape hatch).
Ordering is asserted at the ``_run_with_sync`` seam: the sync runs before the
report generator that issues the Herdr selection-list fetch.
"""  # noqa: EXE001
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

_MODULE_NAME = "generate_standup_sync_first"


def _reload_standup():
    """Import a fresh generate_standup module from the repo skill tree."""
    spec_path = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestSyncOrdering:
    """AC2: the sync runs before the report data fetch."""

    def test_sync_runs_before_report(self):
        mod = _reload_standup()
        order: list[str] = []

        def fake_sync(**_kwargs):
            order.append("sync")
            return {"status": "synced"}

        def fake_report():
            order.append("report")
            return {}

        mod._run_with_sync(fake_report, no_sync=False, sync_fn=fake_sync)

        assert order == ["sync", "report"]

    def test_main_syncs_before_generate_report(self, monkeypatch, tmp_path):
        mod = _reload_standup()
        order: list[tuple] = []

        def fake_sync(**kwargs):
            order.append(("sync", kwargs))
            return {"status": "synced"}

        def fake_generate_report(**_kwargs):
            order.append(("report", None))
            return {}

        monkeypatch.setattr(mod, "sync_worklog", fake_sync)
        monkeypatch.setattr(mod, "generate_report", fake_generate_report)
        monkeypatch.setattr(mod, "format_report", lambda _data, _count=None: "report body")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "generate_standup.py",
                "--no-sync",
                "--worklog-dir",
                str(tmp_path),
                "--output-path",
                str(tmp_path / "out.md"),
            ],
        )

        rc = mod.main()

        assert rc == 0
        assert [entry[0] for entry in order] == ["sync", "report"]
        assert order[0][1]["no_sync"] is True


class TestFailureTolerance:
    """AC4: a failed/skipped sync never blocks the report."""

    def test_failed_sync_does_not_block_report(self):
        mod = _reload_standup()
        ran: list[str] = []

        def failing_sync(**_kwargs):
            return {"status": "failed", "error": "offline"}

        def report():
            ran.append("report")
            return {"ok": True}

        result = mod._run_with_sync(report, sync_fn=failing_sync)

        assert ran == ["report"]
        assert result == {"ok": True}


class TestNoSyncEscapeHatch:
    """AC6: ``--no-sync`` is threaded through to the helper."""

    def test_no_sync_is_forwarded(self):
        mod = _reload_standup()
        captured: dict = {}

        def fake_sync(**kwargs):
            captured.update(kwargs)
            return {"status": "skipped", "reason": "no_sync"}

        mod._run_with_sync(dict, worklog_dir="/tmp/x/.worklog", no_sync=True, sync_fn=fake_sync)

        assert captured["no_sync"] is True
        assert captured["worklog_dir"] == "/tmp/x/.worklog"
