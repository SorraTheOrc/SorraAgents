#!/usr/bin/env python3
"""Tests for the shared worklog sync helper (WL-0MUAD8U24001ZK5K).

Covers the shared ``sync_worklog`` contract used by the refactor and standup
skills:

  AC3 — the sync is lock-aware (``wl sync --if-idle``) and single-flight, so
        a contended lock yields a ``skipped`` result instead of blocking.
  AC4 — a failed sync is non-fatal: the helper returns a ``failed`` status and
        never raises, so callers continue with local data.
  AC6 — ``no_sync=True`` (the ``--no-sync`` escape hatch) short-circuits
        without invoking ``wl`` at all.
  AC7 — when no ``.worklog`` context is resolvable the sync is skipped
        gracefully rather than erroring.
"""  # noqa: EXE001
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from shared import worklog_sync as worklog_sync_mod
from shared.worklog_sync import sync_worklog


def _proc(returncode: int = 0, payload: dict | None = None, stderr: str = ""):
    """Build a ``subprocess.CompletedProcess`` carrying a JSON wl payload."""
    stdout = json.dumps(payload if payload is not None else {"success": True})
    return subprocess.CompletedProcess(["wl"], returncode, stdout, stderr)


def _recording_runner(proc):
    """Return ``(runner, calls)`` where *runner* records each command."""
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(list(cmd))
        return proc

    return runner, calls


class TestIfIdleSingleFlight:
    """AC3: the sync is issued with ``--if-idle`` and honours a skipped result."""

    def test_issues_if_idle_sync(self):
        runner, calls = _recording_runner(_proc())
        result = sync_worklog(runner=runner, reap_stale_claims=False)

        assert result["status"] == "synced"
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "wl"
        assert "sync" in cmd
        assert "--if-idle" in cmd
        assert "--json" in cmd

    def test_contended_lock_reports_skipped(self):
        """AC3: a held lock yields ``skipped`` (another sync in progress)."""
        payload = {
            "success": True,
            "skipped": True,
            "reason": "another sync is already in progress",
        }
        runner, _calls = _recording_runner(_proc(payload=payload))
        result = sync_worklog(runner=runner)

        assert result["status"] == "skipped"
        assert "in progress" in result["reason"]


class TestFailureTolerance:
    """AC4: a failed sync logs a warning and returns — it never raises."""

    def test_non_zero_exit_is_failed_not_raised(self):
        runner, _calls = _recording_runner(
            _proc(returncode=1, payload={"success": False, "error": "offline"})
        )
        result = sync_worklog(runner=runner)

        assert result["status"] == "failed"
        assert "offline" in result["error"]

    def test_runner_exception_is_failed_not_raised(self):
        def runner(_cmd):
            raise OSError("wl binary not found")

        result = sync_worklog(runner=runner)

        assert result["status"] == "failed"
        assert "wl binary not found" in result["error"]


class TestNoSyncEscapeHatch:
    """AC6: ``no_sync`` never touches ``wl``."""

    def test_no_sync_skips_without_invoking_wl(self):
        runner, calls = _recording_runner(_proc())
        result = sync_worklog(runner=runner, no_sync=True)

        assert result == {"status": "skipped", "reason": "no_sync"}
        assert calls == []


class TestNoWorklogContext:
    """AC7: an unresolvable worklog context is skipped, not an error."""

    def test_no_context_skips_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Patch the reference the helper actually calls; an initialized store
        # cannot be resolved here (the cwd has no .worklog).
        monkeypatch.setattr(worklog_sync_mod, "worklog_dir_flag", list)

        result = sync_worklog()

        assert result == {"status": "skipped", "reason": "no worklog context"}


class TestStaleClaimReaperHook:
    """The sync path is where the TTL-bounded stale-claim reaper runs.

    Hooking the reaper here makes it reachable without any herdr/ContextHub
    change (SA-0MW0F91KN004J06U AC3), and the hook is best-effort: it can
    never downgrade a successful sync.
    """

    def test_runs_by_default_after_a_successful_sync(self):
        runner, calls = _recording_runner(_proc())

        result = sync_worklog(runner=runner)

        assert result["status"] == "synced"
        assert result["reaper"]["success"] is True
        assert result["reaper"]["released_count"] == 0
        # The first command is the sync; the sweep follows it.
        assert "sync" in calls[0]
        assert any("list" in call for call in calls[1:])

    def test_can_be_disabled_per_call(self):
        runner, calls = _recording_runner(_proc())

        result = sync_worklog(runner=runner, reap_stale_claims=False)

        assert "reaper" not in result
        assert len(calls) == 1

    def test_environment_switch_disables_the_hook(self, monkeypatch):
        monkeypatch.setenv("WORKLOG_CLAIM_REAPER", "0")
        runner, calls = _recording_runner(_proc())

        result = sync_worklog(runner=runner)

        assert "reaper" not in result
        assert len(calls) == 1

    def test_a_reaper_failure_never_downgrades_the_sync(self):
        def runner(cmd):
            if "sync" in cmd:
                return _proc()
            raise OSError("wl list exploded")

        result = sync_worklog(runner=runner)

        assert result["status"] == "synced"
        assert result["reaper"]["success"] is False
        assert "exploded" in result["reaper"]["error"]


class TestExplicitWorklogDir:
    """The explicit ``worklog_dir`` is forwarded to the ``wl`` invocation."""

    def test_explicit_dir_is_forwarded(self, tmp_path):
        runner, calls = _recording_runner(_proc())
        sync_worklog(worklog_dir=str(tmp_path), runner=runner)

        cmd = calls[0]
        idx = cmd.index("--worklog-dir")
        assert cmd[idx + 1] == str(tmp_path)

    def test_work_item_id_resolves_owning_store(self, tmp_path, monkeypatch):
        """A cross-repo work item syncs the store that owns it."""
        runner, calls = _recording_runner(_proc())
        monkeypatch.setattr(
            worklog_sync_mod, "resolve_worklog_dir", lambda _id: tmp_path
        )

        sync_worklog(work_item_id="WL-0MUAD8U24001ZK5K", runner=runner)

        cmd = calls[0]
        idx = cmd.index("--worklog-dir")
        assert cmd[idx + 1] == str(tmp_path)
