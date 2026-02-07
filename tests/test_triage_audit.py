import json
import os
import glob
import datetime as dt
import subprocess

from ampa.scheduler import (
    Scheduler,
    CommandSpec,
    SchedulerConfig,
    SchedulerStore,
)
import ampa.daemon as daemon
from ampa import webhook


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        # in-memory store
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self) -> None:
        return None


def make_scheduler(run_shell_callable, tmp_path):
    store = DummyStore()
    config = SchedulerConfig(
        poll_interval_seconds=1,
        global_min_interval_seconds=1,
        priority_weight=0.1,
        store_path=str(tmp_path / "store.json"),
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )
    sched = Scheduler(
        store, config, run_shell=run_shell_callable, command_cwd=str(tmp_path)
    )
    return sched


def test_triage_audit_runs_and_cleans_temp(tmp_path, monkeypatch):
    """Verify triage-audit flow executes audit command and removes temp comment file."""
    calls = []
    work_id = "TEST-WID-123"

    # dummy send_webhook so scheduler doesn't try real network
    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        # wl in_progress
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Test item",
                            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        }
                    ]
                }
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        # wl comment list <work>
        if cmd.strip().startswith(f"wl comment list {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"comments": []}), stderr=""
            )
        # opencode audit
        if cmd.strip().startswith(f'opencode run "/audit {work_id}"'):
            # return some stdout that includes a Summary: section
            out = "Summary:\nThis is a short summary line.\n\nDetails:\nMore info"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        # wl comment add
        if cmd.strip().startswith(f"wl comment add {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)

    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"truncate_chars": 65536, "audit_cooldown_hours": 0},
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    # ensure no pre-existing temp files for this work id
    pre = glob.glob(f"/tmp/wl-audit-comment-{work_id}-*.md")
    assert not pre

    # set a fake webhook so summary extraction path runs (send_webhook is a noop)
    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    # run the command (this will invoke our fake_run_shell)
    sched.start_command(spec)

    # ensure audit command was called
    assert any(f"/audit {work_id}" in c for c in calls)
    # ensure a wl comment add was attempted
    assert any(c.startswith(f"wl comment add {work_id}") for c in calls)

    # ensure temp files for this work id were removed
    post = glob.glob(f"/tmp/wl-audit-comment-{work_id}-*.md")
    assert not post


def test_triage_audit_auto_complete_with_gh(tmp_path, monkeypatch):
    """Verify triage-audit auto-completes when gh confirms PR merged."""
    calls = []
    work_id = "TEST-WID-PR-1"

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "PR item",
                            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        }
                    ]
                }
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith(f"wl comment list {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"comments": []}), stderr=""
            )
        if cmd.strip().startswith(f'opencode run "/audit {work_id}"'):
            out = "Summary:\nPR merged: https://github.com/example/repo/pull/42\n\nDetails: ready to close"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith("gh pr view"):
            # simulate gh returning merged:true
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"merged": True}), stderr=""
            )
        if cmd.strip().startswith(f"wl comment add {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
            )
        if cmd.strip().startswith(f"wl show {work_id}"):
            # return no children
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({}), stderr=""
            )
        if cmd.strip().startswith(f"wl update {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)

    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={
            "truncate_chars": 65536,
            "audit_cooldown_hours": 0,
            "verify_pr_with_gh": True,
        },
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    # enable GH verification via env
    monkeypatch.setenv("AMPA_VERIFY_PR_WITH_GH", "1")

    sched.start_command(spec)

    # ensure gh pr view was invoked
    assert any(c.startswith("gh pr view") for c in calls)
    # ensure wl update was invoked to set completed
    assert any(c.startswith(f"wl update {work_id}") for c in calls)


def test_triage_audit_no_candidates_skips_discord(tmp_path, monkeypatch):
    """Verify triage-audit logs and avoids discord when no candidates."""
    calls = []

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)

    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 0},
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    sched.start_command(spec)

    assert calls == ["wl in_progress --json"]
