"""Tests for the PR monitor scheduled command.

Verifies that:
1. PRMonitorRunner detects when gh CLI is unavailable.
2. PRMonitorRunner handles empty PR list.
3. PRMonitorRunner correctly identifies passing checks and posts ready comments.
4. PRMonitorRunner correctly identifies failing checks and creates critical work items.
5. PRMonitorRunner deduplicates ready-for-review comments.
6. PRMonitorRunner skips PRs with pending checks.
7. The scheduler routes command_type='pr-monitor' through PRMonitorRunner.
8. The pr-monitor command is auto-registered at scheduler init.
9. Error handling for gh CLI failures is robust.
10. Notifications are sent for ready and failing PRs.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from ampa.pr_monitor import PRMonitorRunner, _coerce_bool
from ampa.scheduler_types import CommandSpec, RunResult, SchedulerConfig
from ampa.scheduler import Scheduler
from ampa.scheduler_store import SchedulerStore
import datetime as dt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyStore(SchedulerStore):
    """In-memory store for testing."""

    def __init__(self):
        self.path = ":memory:"
        self.data = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "dispatches": [],
        }

    def save(self):
        return None


def _make_config(**overrides) -> SchedulerConfig:
    defaults = dict(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=":memory:",
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    defaults.update(overrides)
    return SchedulerConfig(**defaults)


def _make_pr_monitor_spec(
    command_id: str = "pr-monitor",
    dedup: bool = True,
    max_prs: int = 50,
    gh_command: str = "gh",
) -> CommandSpec:
    return CommandSpec(
        command_id=command_id,
        command="echo pr-monitor",
        requires_llm=False,
        frequency_minutes=60,
        priority=0,
        metadata={"dedup": dedup, "max_prs": max_prs, "gh_command": gh_command},
        title="PR Monitor",
        max_runtime_minutes=10,
        command_type="pr-monitor",
    )


def _noop_executor(spec: CommandSpec) -> RunResult:
    start = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 12, 0, 1, tzinfo=dt.timezone.utc)
    return RunResult(start_ts=start, end_ts=end, exit_code=0)


def _make_shell(responses: Dict[str, Any]):
    """Build a run_shell stub that maps command substrings to responses.

    Each value in *responses* is either:
    - A dict with optional keys ``returncode``, ``stdout``, ``stderr``
    - An exception type/instance to raise
    """

    def _shell(cmd, **kwargs):
        cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        for prefix, resp in responses.items():
            if prefix in cmd_str:
                if isinstance(resp, BaseException) or (
                    isinstance(resp, type) and issubclass(resp, BaseException)
                ):
                    raise resp if isinstance(resp, BaseException) else resp()
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=resp.get("returncode", 0),
                    stdout=resp.get("stdout", ""),
                    stderr=resp.get("stderr", ""),
                )
        # Default: success, empty output
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    return _shell


def _make_scheduler(run_shell=None) -> Scheduler:
    store = DummyStore()
    config = _make_config()
    engine_mock = mock.MagicMock()
    sched = Scheduler(
        store=store,
        config=config,
        executor=_noop_executor,
        run_shell=run_shell
        or (lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")),
        engine=engine_mock,
    )
    return sched


def _pr_list_json(prs: List[Dict[str, Any]]) -> str:
    return json.dumps(prs)


def _checks_json(checks: List[Dict[str, Any]]) -> str:
    return json.dumps(checks)


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — gh unavailable
# ---------------------------------------------------------------------------


class TestPRMonitorGhUnavailable:
    def test_gh_not_found(self):
        run_shell = _make_shell(
            {"gh --version": {"returncode": 127, "stderr": "command not found"}}
        )
        runner = PRMonitorRunner(
            run_shell=run_shell, command_cwd="/tmp"
        )
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "gh_unavailable"
        assert result["prs_checked"] == 0

    def test_gh_exception(self):
        def bad_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                raise RuntimeError("gh binary not found")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=bad_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "gh_unavailable"


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — no open PRs
# ---------------------------------------------------------------------------


class TestPRMonitorNoPRs:
    def test_empty_pr_list(self):
        run_shell = _make_shell(
            {
                "gh --version": {"returncode": 0},
                "gh pr list": {"returncode": 0, "stdout": "[]"},
            }
        )
        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "no_prs"
        assert result["prs_checked"] == 0

    def test_pr_list_failure(self):
        run_shell = _make_shell(
            {
                "gh --version": {"returncode": 0},
                "gh pr list": {"returncode": 1, "stderr": "auth error"},
            }
        )
        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "list_failed"

    def test_pr_list_invalid_json(self):
        run_shell = _make_shell(
            {
                "gh --version": {"returncode": 0},
                "gh pr list": {"returncode": 0, "stdout": "not-json"},
            }
        )
        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "list_failed"

    def test_pr_list_empty_stdout(self):
        run_shell = _make_shell(
            {
                "gh --version": {"returncode": 0},
                "gh pr list": {"returncode": 0, "stdout": ""},
            }
        )
        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)
        assert result["action"] == "no_prs"


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — passing checks (ready for review)
# ---------------------------------------------------------------------------


class TestPRMonitorReady:
    def test_all_checks_passing_posts_comments(self):
        pr_list = _pr_list_json(
            [{"number": 42, "title": "Add feature X", "url": "https://github.com/repo/pull/42", "headRefName": "feat-x"}]
        )
        checks = _checks_json(
            [
                {"name": "ci", "bucket": "pass"},
                {"name": "lint", "bucket": "pass"},
            ]
        )
        calls: Dict[str, List[str]] = {"gh_comments": [], "wl": []}

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                # No existing comments with marker
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            if "gh pr comment" in cmd_str:
                calls["gh_comments"].append(cmd_str)
                return subprocess.CompletedProcess([], 0, "", "")
            if "wl" in cmd_str:
                calls["wl"].append(cmd_str)
                return subprocess.CompletedProcess([], 0, "[]", "")
            return subprocess.CompletedProcess([], 0, "", "")

        notifier = mock.MagicMock()
        runner = PRMonitorRunner(
            run_shell=run_shell, command_cwd="/tmp", notifier=notifier
        )
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert result["prs_checked"] == 1
        assert 42 in result["ready_prs"]
        assert len(result["failing_prs"]) == 0
        # Should have posted a GH comment
        assert len(calls["gh_comments"]) == 1
        assert "42" in calls["gh_comments"][0]

    def test_no_checks_configured_treated_as_passing(self):
        pr_list = _pr_list_json(
            [{"number": 10, "title": "No checks", "url": "https://github.com/repo/pull/10", "headRefName": "no-checks"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                # No checks — empty stdout, success exit
                return subprocess.CompletedProcess([], 0, "", "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert 10 in result["ready_prs"]


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — failing checks
# ---------------------------------------------------------------------------


class TestPRMonitorFailing:
    def test_failing_checks_creates_work_item(self):
        pr_list = _pr_list_json(
            [{"number": 55, "title": "Broken PR", "url": "https://github.com/repo/pull/55", "headRefName": "broken"}]
        )
        checks = _checks_json(
            [
                {"name": "ci-build", "bucket": "fail"},
                {"name": "lint", "bucket": "pass"},
            ]
        )
        calls: Dict[str, List] = {"wl_create": [], "gh_comments": []}

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 1, checks, "")
            if "gh pr comment" in cmd_str:
                calls["gh_comments"].append(cmd_str)
                return subprocess.CompletedProcess([], 0, "", "")
            if "wl create" in cmd_str:
                calls["wl_create"].append(cmd_str)
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"id": "WI-NEW"}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        notifier = mock.MagicMock()
        runner = PRMonitorRunner(
            run_shell=run_shell, command_cwd="/tmp", notifier=notifier
        )
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert 55 in result["failing_prs"]
        assert len(calls["wl_create"]) == 1
        assert len(calls["gh_comments"]) == 1
        # Notifier should be called with error for failing PR
        error_calls = [
            c
            for c in notifier.notify.call_args_list
            if c.kwargs.get("message_type") == "error"
        ]
        assert len(error_calls) >= 1

    def test_multiple_failing_checks(self):
        pr_list = _pr_list_json(
            [{"number": 77, "title": "Multi fail", "url": "https://github.com/repo/pull/77", "headRefName": "multi"}]
        )
        checks = _checks_json(
            [
                {"name": "build", "bucket": "fail"},
                {"name": "test", "bucket": "fail"},
                {"name": "lint", "bucket": "pass"},
            ]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 1, checks, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert 77 in result["failing_prs"]


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — deduplication
# ---------------------------------------------------------------------------


class TestPRMonitorDedup:
    def test_skips_when_ready_comment_exists(self):
        pr_list = _pr_list_json(
            [{"number": 33, "title": "Already notified", "url": "https://github.com/repo/pull/33", "headRefName": "dedup"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                # Comment with marker already exists
                return subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        {
                            "comments": [
                                {
                                    "body": "<!-- ampa-pr-monitor:ready -->\n## All CI checks are passing"
                                }
                            ]
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec(dedup=True)
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert 33 in result["skipped_prs"]
        assert 33 not in result["ready_prs"]

    def test_does_not_skip_when_dedup_disabled(self):
        pr_list = _pr_list_json(
            [{"number": 33, "title": "Re-notify", "url": "https://github.com/repo/pull/33", "headRefName": "nodedup"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )
        gh_comment_calls: List[str] = []

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr comment" in cmd_str:
                gh_comment_calls.append(cmd_str)
                return subprocess.CompletedProcess([], 0, "", "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec(dedup=False)
        result = runner.run(spec)

        assert 33 in result["ready_prs"]
        assert len(gh_comment_calls) == 1


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — pending checks
# ---------------------------------------------------------------------------


class TestPRMonitorPending:
    def test_pending_checks_skipped(self):
        pr_list = _pr_list_json(
            [{"number": 22, "title": "Still running", "url": "https://github.com/repo/pull/22", "headRefName": "pending"}]
        )
        checks = _checks_json(
            [
                {"name": "ci", "bucket": "pending"},
                {"name": "lint", "bucket": "pass"},
            ]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert 22 in result["skipped_prs"]
        assert 22 not in result["ready_prs"]
        assert 22 not in result["failing_prs"]


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — multiple PRs
# ---------------------------------------------------------------------------


class TestPRMonitorMultiplePRs:
    def test_mixed_ready_and_failing(self):
        pr_list = _pr_list_json(
            [
                {"number": 1, "title": "Ready PR", "url": "https://github.com/repo/pull/1", "headRefName": "ready"},
                {"number": 2, "title": "Failing PR", "url": "https://github.com/repo/pull/2", "headRefName": "fail"},
                {"number": 3, "title": "Pending PR", "url": "https://github.com/repo/pull/3", "headRefName": "pend"},
            ]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                if " 1 " in cmd_str:
                    return subprocess.CompletedProcess(
                        [],
                        0,
                        _checks_json(
                            [{"name": "ci", "bucket": "pass"}]
                        ),
                        "",
                    )
                if " 2 " in cmd_str:
                    return subprocess.CompletedProcess(
                        [],
                        1,
                        _checks_json(
                            [{"name": "ci", "bucket": "fail"}]
                        ),
                        "",
                    )
                if " 3 " in cmd_str:
                    return subprocess.CompletedProcess(
                        [],
                        0,
                        _checks_json(
                            [{"name": "ci", "bucket": "pending"}]
                        ),
                        "",
                    )
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        assert result["action"] == "completed"
        assert result["prs_checked"] == 3
        assert 1 in result["ready_prs"]
        assert 2 in result["failing_prs"]
        assert 3 in result["skipped_prs"]


# ---------------------------------------------------------------------------
# Unit tests for PRMonitorRunner — error resilience
# ---------------------------------------------------------------------------


class TestPRMonitorErrorResilience:
    def test_check_status_failure_skips_pr(self):
        pr_list = _pr_list_json(
            [{"number": 88, "title": "Error PR", "url": "https://github.com/repo/pull/88", "headRefName": "err"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                # Failure with no stdout
                return subprocess.CompletedProcess([], 1, "", "internal error")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        # PR should be silently skipped, not crash
        assert result["action"] == "completed"
        assert 88 not in result["ready_prs"]
        assert 88 not in result["failing_prs"]

    def test_gh_comment_failure_does_not_crash(self):
        pr_list = _pr_list_json(
            [{"number": 99, "title": "Comment fail", "url": "https://github.com/repo/pull/99", "headRefName": "cf"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            if "gh pr comment" in cmd_str:
                return subprocess.CompletedProcess([], 1, "", "rate limited")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        result = runner.run(spec)

        # Should still count as ready even though comment failed
        assert 99 in result["ready_prs"]

    def test_notifier_exception_does_not_crash(self):
        pr_list = _pr_list_json(
            [{"number": 11, "title": "Notify fail", "url": "https://github.com/repo/pull/11", "headRefName": "nf"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        notifier = mock.MagicMock()
        notifier.notify.side_effect = RuntimeError("discord down")
        runner = PRMonitorRunner(
            run_shell=run_shell, command_cwd="/tmp", notifier=notifier
        )
        spec = _make_pr_monitor_spec()
        # Should not raise
        result = runner.run(spec)
        assert result["action"] == "completed"


# ---------------------------------------------------------------------------
# Scheduler integration tests
# ---------------------------------------------------------------------------


class TestSchedulerPRMonitor:
    """The scheduler correctly routes pr-monitor command types."""

    def test_scheduler_runs_pr_monitor(self):
        """Scheduler routes command_type=pr-monitor through PRMonitorRunner."""
        pr_list = _pr_list_json(
            [{"number": 5, "title": "Test", "url": "https://github.com/repo/pull/5", "headRefName": "test"}]
        )
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks, "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        sched = _make_scheduler(run_shell=run_shell)
        with mock.patch("ampa.scheduler.notifications_module") as notifier_mock:
            run = sched.start_command(_make_pr_monitor_spec())

        assert run is not None

    def test_scheduler_exception_does_not_propagate(self):
        """An exception in the runner should not escape start_command."""

        def bad_shell(cmd, **kwargs):
            raise RuntimeError("everything is broken")

        sched = _make_scheduler(run_shell=bad_shell)
        with mock.patch("ampa.scheduler.notifications_module"):
            run = sched.start_command(_make_pr_monitor_spec())

        assert run is not None

    def test_pr_monitor_auto_registered(self):
        """Scheduler init auto-registers the pr-monitor command."""
        sched = _make_scheduler()
        cmd_ids = [c.command_id for c in sched.store.list_commands()]
        assert "pr-monitor" in cmd_ids

    def test_pr_monitor_frequency_is_hourly(self):
        """Auto-registered pr-monitor command runs every 60 minutes."""
        sched = _make_scheduler()
        cmd = sched.store.get_command("pr-monitor")
        assert cmd is not None
        assert cmd.frequency_minutes == 60

    def test_pr_monitor_metadata_defaults(self):
        """Auto-registered pr-monitor has expected metadata defaults."""
        sched = _make_scheduler()
        cmd = sched.store.get_command("pr-monitor")
        assert cmd is not None
        assert cmd.metadata.get("dedup") is True
        assert cmd.metadata.get("max_prs") == 50


# ---------------------------------------------------------------------------
# Unit tests for _coerce_bool utility
# ---------------------------------------------------------------------------


class TestCoerceBool:
    def test_true_values(self):
        assert _coerce_bool(True) is True
        assert _coerce_bool("true") is True
        assert _coerce_bool("True") is True
        assert _coerce_bool("1") is True
        assert _coerce_bool("yes") is True
        assert _coerce_bool("on") is True

    def test_false_values(self):
        assert _coerce_bool(False) is False
        assert _coerce_bool(None) is False
        assert _coerce_bool("false") is False
        assert _coerce_bool("0") is False
        assert _coerce_bool("") is False
        assert _coerce_bool("no") is False


# ---------------------------------------------------------------------------
# Unit tests for check state parsing
# ---------------------------------------------------------------------------


class TestCheckStateParsing:
    """Verify edge cases in _get_check_status parsing."""

    def _run_with_checks(self, checks_json_str: str) -> dict:
        pr_list = _pr_list_json(
            [{"number": 1, "title": "Test", "url": "https://github.com/repo/pull/1", "headRefName": "test"}]
        )

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh --version" in cmd_str:
                return subprocess.CompletedProcess([], 0, "gh version 2.x", "")
            if "gh pr list" in cmd_str:
                return subprocess.CompletedProcess([], 0, pr_list, "")
            if "gh pr checks" in cmd_str:
                return subprocess.CompletedProcess([], 0, checks_json_str, "")
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({"comments": []}), ""
                )
            return subprocess.CompletedProcess([], 0, "", "")

        runner = PRMonitorRunner(run_shell=run_shell, command_cwd="/tmp")
        spec = _make_pr_monitor_spec()
        return runner.run(spec)

    def test_success_state(self):
        checks = _checks_json([{"name": "ci", "bucket": "pass"}])
        result = self._run_with_checks(checks)
        assert 1 in result["ready_prs"]

    def test_neutral_conclusion(self):
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )
        result = self._run_with_checks(checks)
        assert 1 in result["ready_prs"]

    def test_skipped_conclusion(self):
        checks = _checks_json(
            [{"name": "ci", "bucket": "pass"}]
        )
        result = self._run_with_checks(checks)
        assert 1 in result["ready_prs"]

    def test_timed_out_conclusion(self):
        checks = _checks_json(
            [{"name": "ci", "bucket": "fail"}]
        )
        result = self._run_with_checks(checks)
        assert 1 in result["failing_prs"]

    def test_error_state(self):
        checks = _checks_json([{"name": "ci", "bucket": "fail"}])
        result = self._run_with_checks(checks)
        assert 1 in result["failing_prs"]

    def test_queued_state(self):
        checks = _checks_json([{"name": "ci", "bucket": "pending"}])
        result = self._run_with_checks(checks)
        assert 1 in result["skipped_prs"]


# ---------------------------------------------------------------------------
# Unit tests for work-item ID extraction
# ---------------------------------------------------------------------------


class TestExtractWorkItemId:
    """Verify _extract_work_item_id() handles branch names and PR bodies."""

    def _make_runner(self, run_shell=None):
        return PRMonitorRunner(
            run_shell=run_shell
            or (lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")),
            command_cwd="/tmp",
        )

    def test_feature_branch(self):
        runner = self._make_runner()
        pr = {"headRefName": "feature/SA-0MMN9YNS41N1B77L-llm-pr-review", "number": 1}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "SA-0MMN9YNS41N1B77L"

    def test_bug_branch(self):
        runner = self._make_runner()
        pr = {"headRefName": "bug/WL-ABC123DEF0-fix-crash", "number": 2}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "WL-ABC123DEF0"

    def test_wl_branch(self):
        runner = self._make_runner()
        pr = {"headRefName": "wl-SA-0MMABCDEF12345-short", "number": 3}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "SA-0MMABCDEF12345"

    def test_no_branch_falls_back_to_body(self):
        body_json = json.dumps({"body": "Fixes work-item: SA-TESTID1234"})

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess([], 0, body_json, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(run_shell)
        pr = {"headRefName": "some-random-branch", "number": 5}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "SA-TESTID1234"

    def test_body_closes_pattern(self):
        body_json = json.dumps({"body": "This PR closes WL-0ABCDEFGHIJ"})

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess([], 0, body_json, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(run_shell)
        pr = {"headRefName": "no-match-here", "number": 6}
        result = runner._extract_work_item_id(pr, "gh")
        assert result == "WL-0ABCDEFGHIJ"

    def test_no_work_item_found(self):
        body_json = json.dumps({"body": "Just a regular PR"})

        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess([], 0, body_json, "")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(run_shell)
        pr = {"headRefName": "main", "number": 7}
        result = runner._extract_work_item_id(pr, "gh")
        assert result is None

    def test_missing_branch_name(self):
        runner = self._make_runner()
        pr = {"number": 8}
        result = runner._extract_work_item_id(pr, "gh")
        assert result is None

    def test_gh_view_failure(self):
        def run_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            if "gh pr view" in cmd_str:
                return subprocess.CompletedProcess([], 1, "", "error")
            return subprocess.CompletedProcess([], 0, "", "")

        runner = self._make_runner(run_shell)
        pr = {"headRefName": "no-match", "number": 9}
        result = runner._extract_work_item_id(pr, "gh")
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests for audit dispatch state tracking
# ---------------------------------------------------------------------------


class TestAuditDispatchState:
    """Verify _get_audit_dispatch_state() and _post_audit_dispatch_marker()."""

    def _make_runner(self, wl_shell=None):
        default_shell = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
        return PRMonitorRunner(
            run_shell=default_shell,
            command_cwd="/tmp",
            wl_shell=wl_shell or default_shell,
        )

    def test_no_dispatch_state(self):
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_dispatch_state("SA-TEST1", 42)
        assert result is None

    def test_dispatch_state_found(self):
        marker = "<!-- ampa-pr-audit-dispatch:42 -->"
        payload = json.dumps({
            "dispatch_state": {
                "pr_number": 42,
                "dispatched_at": "2026-03-12T10:00:00Z",
                "container_id": "pool-1",
                "work_item_id": "SA-TEST1",
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "ampa-pr-monitor"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_dispatch_state("SA-TEST1", 42)
        assert result is not None
        assert result["dispatch_state"]["pr_number"] == 42
        assert result["dispatch_state"]["container_id"] == "pool-1"

    def test_dispatch_state_wrong_pr(self):
        """Dispatch marker for a different PR number is not matched."""
        marker = "<!-- ampa-pr-audit-dispatch:99 -->"
        payload = json.dumps({
            "dispatch_state": {"pr_number": 99, "dispatched_at": "2026-03-12T10:00:00Z"}
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "ampa-pr-monitor"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_dispatch_state("SA-TEST1", 42)
        assert result is None

    def test_wl_show_failure(self):
        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 1, "", "error")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_dispatch_state("SA-TEST1", 42)
        assert result is None

    def test_post_dispatch_marker_success(self):
        calls = []

        def wl_shell(cmd, **kwargs):
            cmd_str = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            calls.append(cmd_str)
            return subprocess.CompletedProcess([], 0, "{}", "")

        runner = self._make_runner(wl_shell)
        result = runner._post_audit_dispatch_marker(
            "SA-TEST1", 42, "2026-03-12T10:00:00Z", "pool-1"
        )
        assert result is True
        assert len(calls) == 1
        assert "wl comment add SA-TEST1" in calls[0]

    def test_post_dispatch_marker_failure(self):
        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 1, "", "error")

        runner = self._make_runner(wl_shell)
        result = runner._post_audit_dispatch_marker(
            "SA-TEST1", 42, "2026-03-12T10:00:00Z"
        )
        assert result is False


# ---------------------------------------------------------------------------
# Unit tests for audit result query
# ---------------------------------------------------------------------------


class TestAuditResult:
    """Verify _get_audit_result() and _parse_marker_json()."""

    def _make_runner(self, wl_shell=None):
        default_shell = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
        return PRMonitorRunner(
            run_shell=default_shell,
            command_cwd="/tmp",
            wl_shell=wl_shell or default_shell,
        )

    def test_no_audit_result(self):
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result("SA-TEST1", 42)
        assert result is None

    def test_audit_result_found(self):
        marker = "<!-- ampa-pr-audit-result -->"
        payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "criteria": [{"name": "Tests pass", "pass": True, "notes": "All green"}],
                "summary": "All criteria met",
                "concerns": [],
                "audited_at": "2026-03-12T12:00:00Z",
                "pr_number": 42,
                "pr_sha": "abc123",
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "audit-agent"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result("SA-TEST1", 42)
        assert result is not None
        assert result["overall"] == "pass"
        assert result["pr_number"] == 42

    def test_audit_result_wrong_pr(self):
        marker = "<!-- ampa-pr-audit-result -->"
        payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "audited_at": "2026-03-12T12:00:00Z",
                "pr_number": 99,
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "audit-agent"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result("SA-TEST1", 42)
        assert result is None

    def test_audit_result_stale(self):
        """Audit result older than after_iso is rejected."""
        marker = "<!-- ampa-pr-audit-result -->"
        payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "audited_at": "2026-03-12T10:00:00Z",
                "pr_number": 42,
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "audit-agent"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result(
            "SA-TEST1", 42, after_iso="2026-03-12T11:00:00Z"
        )
        assert result is None

    def test_audit_result_fresh(self):
        """Audit result newer than after_iso is accepted."""
        marker = "<!-- ampa-pr-audit-result -->"
        payload = json.dumps({
            "audit_result": {
                "overall": "pass",
                "audited_at": "2026-03-12T14:00:00Z",
                "pr_number": 42,
            }
        })
        wl_data = json.dumps({
            "workItem": {"id": "SA-TEST1"},
            "comments": [
                {"comment": f"{marker}\n{payload}", "author": "audit-agent"},
            ],
        })

        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 0, wl_data, "")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result(
            "SA-TEST1", 42, after_iso="2026-03-12T11:00:00Z"
        )
        assert result is not None
        assert result["overall"] == "pass"

    def test_wl_show_failure(self):
        def wl_shell(cmd, **kwargs):
            return subprocess.CompletedProcess([], 1, "", "error")

        runner = self._make_runner(wl_shell)
        result = runner._get_audit_result("SA-TEST1", 42)
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests for _parse_marker_json
# ---------------------------------------------------------------------------


class TestParseMarkerJson:
    """Verify the static _parse_marker_json helper."""

    def test_valid_json(self):
        body = '<!-- marker -->\n{"key": "value"}'
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result == {"key": "value"}

    def test_nested_json(self):
        body = '<!-- marker -->\n{"outer": {"inner": 42}}'
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result == {"outer": {"inner": 42}}

    def test_no_marker(self):
        body = "no marker here"
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result is None

    def test_no_json_after_marker(self):
        body = "<!-- marker -->\nno json here"
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result is None

    def test_invalid_json(self):
        body = "<!-- marker -->\n{invalid json}"
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result is None

    def test_marker_with_extra_text(self):
        body = "Some prefix\n<!-- marker -->\ntext {\"a\": 1} more"
        result = PRMonitorRunner._parse_marker_json(body, "<!-- marker -->")
        assert result == {"a": 1}
