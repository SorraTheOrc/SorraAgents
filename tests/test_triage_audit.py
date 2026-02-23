import json
import os
import glob
import datetime as dt
import subprocess
import re
from types import SimpleNamespace

from ampa import scheduler
from ampa.scheduler import (
    Scheduler,
    CommandSpec,
    SchedulerConfig,
    SchedulerStore,
)
import ampa.daemon as daemon
from ampa import webhook
from ampa.engine.core import EngineConfig
from ampa.engine.dispatch import DispatchResult


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        # in-memory store
        self.path = ":memory:"
        self.data = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "config": {},
        }

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


def test_triage_audit_includes_blocked_items(tmp_path, monkeypatch):
    """Verify blocked items are included when in_progress is empty."""
    calls = []
    work_id = "BLOCKED-1"

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if cmd.strip() == "wl list --status blocked --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Blocked item",
                            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "status": "blocked",
                        }
                    ]
                }
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        # allow fallback 'wl blocked --json'
        if cmd.strip() == "wl blocked --json":
            out = json.dumps(
                [
                    {
                        "id": work_id,
                        "title": "Blocked item",
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "status": "blocked",
                    }
                ]
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith(f"wl comment list {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"comments": []}), stderr=""
            )
        if cmd.strip().startswith(f'opencode run "/audit {work_id}"'):
            out = "Summary:\nBlocked item audit\n"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
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

    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    sched.start_command(spec)

    # ensure blocked listing was attempted and audit ran for blocked item
    assert any(
        "wl list --status blocked --json" in c or c.strip() == "wl blocked --json"
        for c in calls
    )
    assert any(f"/audit {work_id}" in c for c in calls)


def test_per_status_cooldown_respected(tmp_path, monkeypatch):
    """Verify per-status cooldowns: in_review cooldown smaller than in_progress."""
    calls = []
    wid_in_review = "WID-REV"
    wid_in_progress = "WID-PROG"

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    now = dt.datetime.now(dt.timezone.utc)
    # last audit times: both 2 hours ago
    last_audit_iso = (now - dt.timedelta(hours=2)).isoformat()

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": wid_in_review,
                            "title": "Review item",
                            "updated_at": (now - dt.timedelta(hours=3)).isoformat(),
                            "status": "in_review",
                        },
                        {
                            "id": wid_in_progress,
                            "title": "Prog item",
                            "updated_at": (now - dt.timedelta(hours=4)).isoformat(),
                            "status": "in_progress",
                        },
                    ]
                }
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith("wl comment list"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"comments": []}), stderr=""
            )
        if cmd.strip().startswith(f'opencode run "/audit {wid_in_review}"'):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Summary:\nreview allowed", stderr=""
            )
        if cmd.strip().startswith(f'opencode run "/audit {wid_in_progress}"'):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Summary:\nprog audited", stderr=""
            )
        if cmd.strip().startswith("wl comment add"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)

    # metadata sets in_review cooldown to 1 hour, in_progress to 6 hours
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={
            "truncate_chars": 65536,
            "audit_cooldown_hours": 6,
            "audit_cooldown_hours_in_progress": 6,
            "audit_cooldown_hours_in_review": 1,
        },
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    # persist last_audit timestamps for both items as 2 hours ago
    sched.store.update_state(
        spec.command_id,
        {
            "last_audit_at_by_item": {
                wid_in_review: last_audit_iso,
                wid_in_progress: last_audit_iso,
            }
        },
    )

    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    sched.start_command(spec)

    # Since in_review cooldown is 1h and last_audit was 2h ago, the in_review item should be audited.
    # The in_progress item should be skipped because its cooldown is 6h and last_audit is 2h ago.
    assert any(f"/audit {wid_in_review}" in c for c in calls)
    assert not any(f"/audit {wid_in_progress}" in c for c in calls)


def test_triage_audit_audit_only_no_update(tmp_path, monkeypatch):
    """Verify audit-only mode avoids wl update."""
    calls = []
    work_id = "AUDIT-ONLY-1"

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Audit only item",
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
            out = "Summary:\nAudit only output\n"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith(f"wl comment add {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
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
        metadata={"truncate_chars": 65536, "audit_cooldown_hours": 0},
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    sched.start_command(spec)

    # ensure comment add was attempted and no update was made
    assert any(c.startswith(f"wl comment add {work_id}") for c in calls)
    assert not any(c.startswith(f"wl update {work_id}") for c in calls)


def test_triage_audit_audit_only_no_templates(tmp_path, monkeypatch):
    """Verify audit-only mode does not add template headings in comment payload."""
    calls = []
    work_id = "AUDIT-ONLY-2"
    comment_payload = {"text": ""}

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Audit only title",
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
            out = "Summary:\nAudit only output\n"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith(f"wl comment add {work_id}"):
            m = re.search(r"\$\(cat '([^']+)'\)", cmd)
            assert m
            with open(m.group(1), "r", encoding="utf-8") as fh:
                comment_payload["text"] = fh.read()
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

    sched.start_command(spec)

    assert "Proposed child work items" not in comment_payload["text"]
    assert "## Intake" not in comment_payload["text"]
    assert "## Plan" not in comment_payload["text"]


def test_triage_audit_discord_summary_includes_body(tmp_path, monkeypatch):
    """Verify Discord summary includes a body line, not just heading."""
    calls = []
    work_id = "DISCORD-SUMMARY-1"
    captured = {}

    def fake_send_webhook(url, payload, timeout=10, message_type="other"):
        captured["payload"] = payload
        return 200

    monkeypatch.setattr(webhook, "send_webhook", fake_send_webhook)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Discord summary item",
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
            out = "Summary:\nA short summary for Discord.\n"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith(f"wl comment add {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
            )
        if cmd.strip().startswith(f"wl show {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({}), stderr=""
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

    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    sched.start_command(spec)

    content = captured.get("payload", {}).get("content", "")
    assert "# Triage Audit — Discord summary item" in content
    assert "Summary: A short summary for Discord." in content
    assert "Delegation:" in content


def test_triage_audit_delegation_disabled(tmp_path, monkeypatch):
    """Verify delegation does not run when audit_only is true."""
    calls = []
    work_id = "DELEGATE-1"

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Audit item",
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
            out = "Summary:\nAudit output\n"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
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
        metadata={
            "truncate_chars": 65536,
            "audit_cooldown_hours": 0,
            "audit_only": True,
        },
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    sched.start_command(spec)

    assert not any("wl next --json" in c for c in calls)
    assert not any("work on" in c for c in calls)


def test_triage_audit_delegation_skips_when_in_progress(tmp_path, monkeypatch):
    """Verify delegation no-ops when in-progress items exist."""
    calls = []
    work_id = "DELEGATE-2"

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Audit item",
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
            out = "Summary:\nAudit output\n"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
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
        metadata={
            "truncate_chars": 65536,
            "audit_cooldown_hours": 0,
            "audit_only": False,
        },
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    sched.start_command(spec)

    # The engine's CandidateSelector.select() always fetches candidates (even
    # with in-progress items) for the audit trail, so "wl next --json" may
    # appear in calls.  The important assertion is that no dispatch occurred.
    assert not any("work on" in c for c in calls)
    assert not any("/intake" in c for c in calls)
    assert not any("/implement" in c for c in calls)


def test_triage_audit_delegation_dispatches_intake_when_idle(tmp_path, monkeypatch):
    """Verify delegation dispatches intake when idle and enabled.

    The engine's CandidateSelector fetches candidates via ``wl next --json``
    and the engine dispatches via its ``OpenCodeRunDispatcher``.  We mock the
    dispatcher to avoid real subprocess spawning and verify that the engine
    selected the candidate and dispatched an intake command.
    """
    calls = []
    work_id = "DELEGATE-3"
    candidate_id = "SA-TEST-1"
    in_progress_calls = {"count": 0}
    long_description = (
        "This is a test work item for delegation via triage-audit. "
        "It has sufficient context to satisfy the requires_work_item_context "
        "invariant which needs more than 100 characters in the description.\n\n"
        "Acceptance Criteria:\n"
        "- [ ] Delegation dispatches intake\n"
        "- [ ] Webhook is sent"
    )

    monkeypatch.setattr(webhook, "send_webhook", lambda *a, **k: None)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl in_progress --json":
            in_progress_calls["count"] += 1
            if in_progress_calls["count"] == 1:
                out = json.dumps(
                    {
                        "workItems": [
                            {
                                "id": work_id,
                                "title": "Audit item",
                                "updated_at": dt.datetime.now(
                                    dt.timezone.utc
                                ).isoformat(),
                            }
                        ]
                    }
                )
            else:
                out = json.dumps({"workItems": []})
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip() == "wl next --json":
            payload = {
                "workItem": {
                    "id": candidate_id,
                    "title": "Test idea",
                    "status": "open",
                    "stage": "idea",
                }
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if "wl show" in cmd and candidate_id in cmd:
            item = {
                "id": candidate_id,
                "title": "Test idea",
                "status": "open",
                "stage": "idea",
                "description": long_description,
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(item), stderr=""
            )
        if cmd.strip().startswith("opencode run"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr=""
            )
        if cmd.strip().startswith(f"wl comment list {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"comments": []}), stderr=""
            )
        if cmd.strip().startswith(f'opencode run "/audit {work_id}"'):
            out = "Summary:\nAudit output\n"
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith(f"wl comment add {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("AMPA_FALLBACK_MODE", raising=False)

    sched = make_scheduler(fake_run_shell, tmp_path)

    # Override the engine's fallback_mode to None so it doesn't force
    # action=accept (which has no command template)
    sched.engine._config = EngineConfig(  # type: ignore[union-attr]
        descriptor_path=sched.engine._config.descriptor_path,  # type: ignore[union-attr]
        fallback_mode=None,
    )

    # Mock the engine's dispatcher to avoid real subprocess spawning
    dispatch_state = {"called": False, "command": None}

    def fake_dispatch(command, work_item_id):
        dispatch_state["called"] = True
        dispatch_state["command"] = command
        return DispatchResult(
            success=True,
            command=command,
            work_item_id=work_item_id,
            pid=99999,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

    monkeypatch.setattr(sched.engine._dispatcher, "dispatch", fake_dispatch)  # type: ignore[union-attr]

    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={
            "truncate_chars": 65536,
            "audit_cooldown_hours": 0,
            "audit_only": False,
        },
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    sched.start_command(spec)

    assert any("wl next --json" in c for c in calls)
    # The engine dispatches via its dispatcher, not via run_shell
    assert dispatch_state["called"], "engine dispatcher was not called"
    assert candidate_id in dispatch_state["command"]


def test_triage_audit_no_candidates_logs(tmp_path, monkeypatch, caplog):
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

    with caplog.at_level("INFO"):
        sched.start_command(spec)

    assert calls == ["wl in_progress --json"]
    assert any("no candidates" in message.lower() for message in caplog.messages)


def test_scheduler_run_once_unknown_command(tmp_path, monkeypatch, capsys):
    from ampa import scheduler_cli

    sched = make_scheduler(lambda *a, **k: subprocess.CompletedProcess("", 0), tmp_path)
    monkeypatch.setattr(scheduler_cli, "load_scheduler", lambda command_cwd=None: sched)
    monkeypatch.setattr(scheduler_cli.daemon, "load_env", lambda: None)
    args = SimpleNamespace(command_id="missing")

    exit_code = scheduler_cli._cli_run_once(args)
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "Unknown command id" in out
