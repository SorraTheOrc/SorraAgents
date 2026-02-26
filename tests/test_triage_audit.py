import json
import os
import glob
import datetime as dt
import subprocess
import re
from types import SimpleNamespace

from ampa import scheduler
from ampa.scheduler_types import CommandSpec, SchedulerConfig
from ampa.scheduler import (
    Scheduler,
    SchedulerStore,
)
import ampa.daemon as daemon
from ampa import notifications


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

    # dummy notify so scheduler doesn't try real network
    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        # wl list --stage in_review
        if cmd.strip() == "wl list --stage in_review --json":
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

    # set a fake bot token so summary extraction path runs (notify is a noop)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

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

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
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
    # ensure --needs-producer-review true is included in the update command
    update_cmds = [c for c in calls if c.startswith(f"wl update {work_id}")]
    assert any("--needs-producer-review true" in c for c in update_cmds), (
        f"Expected --needs-producer-review true in update command, got: {update_cmds}"
    )


def test_triage_audit_no_candidates_skips_discord(tmp_path, monkeypatch):
    """Verify triage-audit logs and avoids discord when no candidates."""
    calls = []

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
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

    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    sched.start_command(spec)

    assert calls == ["wl list --stage in_review --json"]


def test_triage_audit_excludes_in_progress_items(tmp_path, monkeypatch):
    """Verify in_progress items are NOT selected as candidates.

    The triage-audit now only queries ``wl list --stage in_review``, so items
    with ``in_progress`` status should never appear in the candidate list.
    This is a negative test replacing the old ``test_triage_audit_includes_blocked_items``.
    """
    calls = []

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
            # Return empty — no in_review items exist
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
        metadata={"truncate_chars": 65536, "audit_cooldown_hours": 0},
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    sched.start_command(spec)

    # No blocked or in_progress queries should be made — only in_review
    assert not any("wl in_progress" in c for c in calls)
    assert not any("wl list --status blocked" in c for c in calls)
    assert not any("wl blocked" in c for c in calls)
    # Only the in_review query should have been made, and since it returned
    # empty, no audit should have run
    assert calls == ["wl list --stage in_review --json"]


def test_per_status_cooldown_respected(tmp_path, monkeypatch):
    """Verify store-based cooldown: items past cooldown are audited, items within cooldown are skipped.

    The audit poller uses a single ``audit_cooldown_hours`` value (no
    per-status overrides).  Items whose ``last_audit_at`` is older than
    this threshold are eligible; items within the threshold are skipped.
    """
    calls = []
    wid_fresh = "WID-FRESH"
    wid_recent = "WID-RECENT"

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    now = dt.datetime.now(dt.timezone.utc)
    # last audit for wid_fresh: 7 hours ago (past the 6-hour cooldown)
    # last audit for wid_recent: 3 hours ago (within the 6-hour cooldown)
    last_audit_fresh = (now - dt.timedelta(hours=7)).isoformat()
    last_audit_recent = (now - dt.timedelta(hours=3)).isoformat()

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": wid_fresh,
                            "title": "Fresh review item",
                            "updated_at": (now - dt.timedelta(hours=4)).isoformat(),
                            "status": "in_review",
                        },
                        {
                            "id": wid_recent,
                            "title": "Recent review item",
                            "updated_at": (now - dt.timedelta(hours=2)).isoformat(),
                            "status": "in_review",
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
        if cmd.strip().startswith(f'opencode run "/audit {wid_fresh}"'):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="Summary:\nfresh review allowed",
                stderr="",
            )
        if cmd.strip().startswith(f'opencode run "/audit {wid_recent}"'):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Summary:\nrecent audited", stderr=""
            )
        if cmd.strip().startswith("wl comment add"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)

    # metadata sets cooldown to 6 hours (the poller uses a single cooldown)
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={
            "truncate_chars": 65536,
            "audit_cooldown_hours": 6,
        },
        command_type="triage-audit",
    )
    sched.store.add_command(spec)

    # persist last_audit timestamps: wid_fresh 7h ago, wid_recent 3h ago
    sched.store.update_state(
        spec.command_id,
        {
            "last_audit_at_by_item": {
                wid_fresh: last_audit_fresh,
                wid_recent: last_audit_recent,
            }
        },
    )

    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    sched.start_command(spec)

    # wid_fresh: last audit 7h ago, cooldown 6h → should be audited
    assert any(f"/audit {wid_fresh}" in c for c in calls)
    # wid_recent: last audit 3h ago, cooldown 6h → should be skipped
    assert not any(f"/audit {wid_recent}" in c for c in calls)


def test_triage_audit_audit_only_no_update(tmp_path, monkeypatch):
    """Verify audit-only mode avoids wl update."""
    calls = []
    work_id = "AUDIT-ONLY-1"

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
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

    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    sched.start_command(spec)

    # ensure comment add was attempted and no update was made
    assert any(c.startswith(f"wl comment add {work_id}") for c in calls)
    assert not any(c.startswith(f"wl update {work_id}") for c in calls)


def test_triage_audit_audit_only_no_templates(tmp_path, monkeypatch):
    """Verify audit-only mode does not add template headings in comment payload."""
    calls = []
    work_id = "AUDIT-ONLY-2"
    comment_payload = {"text": ""}

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
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
    """Verify Discord summary includes a body line, not just heading.

    Also verifies that the Work Item ID and GitHub issue URL are included
    as extra fields in the Discord notification content.
    """
    calls = []
    work_id = "DISCORD-SUMMARY-1"
    captured = {}

    # Create .worklog/config.yaml so _get_github_repo() finds a repo slug
    wl_dir = tmp_path / ".worklog"
    wl_dir.mkdir(parents=True, exist_ok=True)
    (wl_dir / "config.yaml").write_text("githubRepo: TestOwner/TestRepo\n")

    def fake_notify(title, body="", message_type="other", *, payload=None):
        captured["title"] = title
        captured["body"] = body
        captured["message_type"] = message_type
        if payload is not None:
            captured["payload"] = payload
        return True

    monkeypatch.setattr(notifications, "notify", fake_notify)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
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
            # Return nested workItem with githubIssueNumber
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"workItem": {"githubIssueNumber": 42}}),
                stderr="",
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

    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    sched.start_command(spec)

    content = captured.get("payload", {}).get("content", "")
    assert "# Triage Audit — Discord summary item" in content
    assert "Summary: A short summary for Discord." in content
    # New: Work Item ID and GitHub issue URL in extra fields
    assert f"Work Item: {work_id}" in content
    assert "GitHub: https://github.com/TestOwner/TestRepo/issues/42" in content


def test_triage_audit_no_candidates_logs(tmp_path, monkeypatch, caplog):
    calls = []

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
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

    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    with caplog.at_level("INFO"):
        sched.start_command(spec)

    assert calls == ["wl list --stage in_review --json"]
    # The audit poller logs "no items in_review" and the scheduler logs
    # "no eligible candidates" — check for either message indicating no work.
    assert any(
        "no items" in message.lower() or "no eligible candidates" in message.lower()
        for message in caplog.messages
    )


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


# ---------------------------------------------------------------------------
# Audit poller routing integration tests
# ---------------------------------------------------------------------------


def test_triage_audit_runner_requires_work_item():
    """TriageAuditRunner.run() raises TypeError when called without work_item."""
    from ampa.triage_audit import TriageAuditRunner

    runner = TriageAuditRunner(
        run_shell=lambda *a, **kw: subprocess.CompletedProcess("", 0),
        command_cwd="/tmp",
        store=DummyStore(),
    )
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        command_type="triage-audit",
    )
    import pytest

    with pytest.raises(TypeError, match="requires a pre-selected work_item"):
        runner.run(spec, None, None)


def test_scheduler_handles_query_failure_gracefully(tmp_path, monkeypatch, caplog):
    """When wl list fails, the scheduler logs the failure and returns without crashing."""
    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        if cmd.strip() == "wl list --stage in_review --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="connection refused"
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 6},
        command_type="triage-audit",
    )
    sched.store.add_command(spec)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    with caplog.at_level("INFO"):
        result = sched.start_command(spec)

    # Should not crash; scheduler returns the run result
    assert result is not None
    # The poller logs a warning about the failed query
    assert any(
        "no items" in msg.lower()
        or "no eligible candidates" in msg.lower()
        or "query failed" in msg.lower()
        for msg in caplog.messages
    )


def test_scheduler_poller_handler_end_to_end(tmp_path, monkeypatch):
    """End-to-end: scheduler routes through poller, which selects a candidate and
    hands it off to TriageAuditRunner for audit execution."""
    calls = []
    work_id = "END2END-ITEM"

    monkeypatch.setattr(notifications, "notify", lambda *a, **k: True)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "E2E test item",
                            "updated_at": (
                                dt.datetime.now(dt.timezone.utc)
                                - dt.timedelta(hours=10)
                            ).isoformat(),
                        }
                    ]
                }
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip().startswith(f'opencode run "/audit {work_id}"'):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="Summary:\nAudit passed, all criteria met.",
                stderr="",
            )
        if cmd.strip().startswith("wl comment add"):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)
    spec = CommandSpec(
        command_id="wl-triage-audit",
        command="true",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={"audit_cooldown_hours": 6, "truncate_chars": 65536},
        command_type="triage-audit",
    )
    sched.store.add_command(spec)
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    sched.start_command(spec)

    # Verify the poller queried candidates
    assert any("wl list --stage in_review --json" in c for c in calls)
    # Verify the handler executed the audit command
    assert any(f"/audit {work_id}" in c for c in calls)
    # Verify the poller persisted the cooldown timestamp in the store
    state = sched.store.get_state(spec.command_id)
    assert work_id in state.get("last_audit_at_by_item", {})


# ---------------------------------------------------------------------------
# _get_github_repo / _build_github_issue_url tests
# ---------------------------------------------------------------------------
from ampa.triage_audit import _get_github_repo, _build_github_issue_url


def test_get_github_repo_happy_path(tmp_path):
    """Reads githubRepo from .worklog/config.yaml."""
    wl_dir = tmp_path / ".worklog"
    wl_dir.mkdir()
    (wl_dir / "config.yaml").write_text("githubRepo: MyOrg/MyRepo\n")
    assert _get_github_repo(str(tmp_path)) == "MyOrg/MyRepo"


def test_get_github_repo_missing_file(tmp_path):
    """Returns None when config.yaml does not exist."""
    assert _get_github_repo(str(tmp_path)) is None


def test_get_github_repo_missing_key(tmp_path):
    """Returns None when githubRepo key is absent."""
    wl_dir = tmp_path / ".worklog"
    wl_dir.mkdir()
    (wl_dir / "config.yaml").write_text("someOtherKey: value\n")
    assert _get_github_repo(str(tmp_path)) is None


def test_get_github_repo_not_set(tmp_path):
    """Returns None when githubRepo is '(not set)'."""
    wl_dir = tmp_path / ".worklog"
    wl_dir.mkdir()
    (wl_dir / "config.yaml").write_text("githubRepo: (not set)\n")
    assert _get_github_repo(str(tmp_path)) is None


def test_get_github_repo_none_cwd():
    """Returns None gracefully when command_cwd is None and ./worklog doesn't exist."""
    # This should not raise — it should return None
    result = _get_github_repo(None)
    # Result depends on whether ./.worklog/config.yaml exists in the cwd
    assert result is None or isinstance(result, str)


def test_build_github_issue_url_happy_path():
    """Builds correct URL from repo slug and issue number."""
    assert (
        _build_github_issue_url("MyOrg/MyRepo", 42)
        == "https://github.com/MyOrg/MyRepo/issues/42"
    )


def test_build_github_issue_url_string_number():
    """Accepts issue number as a string."""
    assert (
        _build_github_issue_url("MyOrg/MyRepo", "7")
        == "https://github.com/MyOrg/MyRepo/issues/7"
    )


def test_build_github_issue_url_none_repo():
    """Returns None when repo_slug is None."""
    assert _build_github_issue_url(None, 42) is None


def test_build_github_issue_url_none_number():
    """Returns None when issue_number is None."""
    assert _build_github_issue_url("MyOrg/MyRepo", None) is None


def test_build_github_issue_url_invalid_number():
    """Returns None when issue_number is not a valid integer."""
    assert _build_github_issue_url("MyOrg/MyRepo", "not-a-number") is None


def test_build_github_issue_url_zero():
    """Returns None when issue_number is 0 (falsy)."""
    assert _build_github_issue_url("MyOrg/MyRepo", 0) is None


# ---------------------------------------------------------------------------
# _extract_audit_report tests
# ---------------------------------------------------------------------------
from ampa.triage_audit import _extract_audit_report
from ampa.triage_audit import _extract_summary_from_report


def test_extract_audit_report_happy_path():
    """Extracts content between start and end markers."""
    raw = (
        "Some preamble noise from the agent\n"
        "--- AUDIT REPORT START ---\n"
        "## Summary\n"
        "\n"
        "Everything looks great.\n"
        "\n"
        "## Recommendation\n"
        "\n"
        "This item can be closed.\n"
        "--- AUDIT REPORT END ---\n"
        "trailing noise\n"
    )
    result = _extract_audit_report(raw)
    assert result.startswith("## Summary")
    assert "Everything looks great." in result
    assert "This item can be closed." in result
    assert "--- AUDIT REPORT START ---" not in result
    assert "--- AUDIT REPORT END ---" not in result
    assert "preamble" not in result
    assert "trailing noise" not in result


def test_extract_audit_report_missing_start_marker(caplog):
    """Falls back to full output when start marker is missing."""
    raw = "No markers here, just plain audit output."
    with caplog.at_level("WARNING"):
        result = _extract_audit_report(raw)
    assert result == raw
    assert any("missing start marker" in m.lower() for m in caplog.messages)


def test_extract_audit_report_missing_end_marker(caplog):
    """Uses content after start marker when end marker is missing."""
    raw = (
        "preamble\n--- AUDIT REPORT START ---\n## Summary\n\nThe end marker was lost.\n"
    )
    with caplog.at_level("WARNING"):
        result = _extract_audit_report(raw)
    assert "The end marker was lost." in result
    assert "## Summary" in result
    assert "preamble" not in result
    assert any("missing end marker" in m.lower() for m in caplog.messages)


def test_extract_audit_report_empty_content(caplog):
    """Falls back to full output when content between markers is empty."""
    raw = "--- AUDIT REPORT START ---\n--- AUDIT REPORT END ---\n"
    with caplog.at_level("WARNING"):
        result = _extract_audit_report(raw)
    assert result == raw
    assert any("empty" in m.lower() for m in caplog.messages)


def test_extract_audit_report_whitespace_only_content(caplog):
    """Falls back to full output when content between markers is whitespace-only."""
    raw = "--- AUDIT REPORT START ---\n   \n\n--- AUDIT REPORT END ---\n"
    with caplog.at_level("WARNING"):
        result = _extract_audit_report(raw)
    assert result == raw
    assert any("empty" in m.lower() for m in caplog.messages)


def test_extract_audit_report_multiple_marker_pairs():
    """Only the first pair of markers is used."""
    raw = (
        "--- AUDIT REPORT START ---\n"
        "First report.\n"
        "--- AUDIT REPORT END ---\n"
        "--- AUDIT REPORT START ---\n"
        "Second report.\n"
        "--- AUDIT REPORT END ---\n"
    )
    result = _extract_audit_report(raw)
    assert result == "First report."
    assert "Second report." not in result


def test_extract_audit_report_empty_input():
    """Returns empty string for empty input."""
    assert _extract_audit_report("") == ""
    assert _extract_audit_report(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _extract_summary_from_report tests
# ---------------------------------------------------------------------------


def test_extract_summary_from_report_happy_path():
    """Extracts the Summary section from a structured report."""
    report = (
        "## Summary\n"
        "\n"
        "Everything looks great. All criteria are met.\n"
        "\n"
        "## Acceptance Criteria Status\n"
        "\n"
        "| # | Criterion | Verdict |\n"
    )
    result = _extract_summary_from_report(report)
    assert result == "Everything looks great. All criteria are met."


def test_extract_summary_from_report_no_heading():
    """Returns empty string when no ## Summary heading exists."""
    report = "## Acceptance Criteria Status\n\nSome table here.\n"
    assert _extract_summary_from_report(report) == ""


def test_extract_summary_from_report_summary_at_end():
    """Extracts summary when it is the last section."""
    report = "## Summary\n\nFinal summary with no sections after it.\n"
    result = _extract_summary_from_report(report)
    assert result == "Final summary with no sections after it."


def test_extract_summary_from_report_empty_input():
    """Returns empty string for empty input."""
    assert _extract_summary_from_report("") == ""


def test_extract_summary_from_report_multiline():
    """Extracts multi-line summary content."""
    report = (
        "## Summary\n"
        "\n"
        "Line one of the summary.\n"
        "Line two continues.\n"
        "\n"
        "Another paragraph in summary.\n"
        "\n"
        "## Recommendation\n"
        "\n"
        "Close it.\n"
    )
    result = _extract_summary_from_report(report)
    assert "Line one of the summary." in result
    assert "Line two continues." in result
    assert "Another paragraph in summary." in result
    assert "Close it." not in result


# ---------------------------------------------------------------------------
# End-to-end integration test (mock-based)
# ---------------------------------------------------------------------------


def test_structured_audit_end_to_end(tmp_path, monkeypatch):
    """Integration test: canned structured audit output flows through
    marker extraction → comment posting (structured report only) →
    Discord summary extraction from ## Summary section.

    Verifies:
    1. The posted WL comment contains the structured report (not raw output).
    2. The posted WL comment does NOT contain the old "Audit output:" label.
    3. The Discord webhook payload includes the summary extracted from ## Summary.
    4. Preamble/trailing noise from the raw output is excluded from the comment.
    """
    calls = []
    webhook_payloads = []
    posted_comments = []
    work_id = "TEST-E2E-STRUCT-001"

    canned_audit_output = (
        "Some preamble noise from the agent stdout\n"
        "--- AUDIT REPORT START ---\n"
        "## Summary\n"
        "\n"
        "All 3 acceptance criteria are met. The implementation is correct and tests pass.\n"
        "\n"
        "## Acceptance Criteria Status\n"
        "\n"
        "| # | Criterion | Verdict | Evidence |\n"
        "|---|-----------|---------|----------|\n"
        "| 1 | Widget renders | met | src/widget.tsx:15 |\n"
        "| 2 | API returns 200 | met | src/api.ts:42 |\n"
        "| 3 | Tests pass | met | tests/widget.test.ts:8 |\n"
        "\n"
        "## Children Status\n"
        "\n"
        "No children.\n"
        "\n"
        "## Recommendation\n"
        "\n"
        "This item can be closed: all acceptance criteria are met.\n"
        "--- AUDIT REPORT END ---\n"
        "trailing agent noise\n"
    )

    def capture_notify(title, body="", message_type="other", *, payload=None):
        if payload is not None:
            webhook_payloads.append(payload)
        return True

    monkeypatch.setattr(notifications, "notify", capture_notify)

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        if cmd.strip() == "wl list --stage in_review --json":
            out = json.dumps(
                {
                    "workItems": [
                        {
                            "id": work_id,
                            "title": "Structured audit test item",
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
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=canned_audit_output, stderr=""
            )
        if cmd.strip().startswith(f"wl comment add {work_id}"):
            posted_comments.append(cmd)
            # Extract the comment file path from the command to read its content
            import re as _re

            m = _re.search(r"cat '([^']+)'", cmd)
            if m:
                try:
                    with open(m.group(1), "r") as f:
                        posted_comments.append(f.read())
                except Exception:
                    pass
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"success": True}), stderr=""
            )
        if cmd.strip().startswith(f"wl show {work_id}"):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"id": work_id, "status": "in_progress"}),
                stderr="",
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

    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-token")

    sched.start_command(spec)

    # --- Verify comment posting ---
    # A wl comment add should have been attempted
    comment_cmds = [c for c in calls if c.startswith(f"wl comment add {work_id}")]
    assert comment_cmds, "Expected at least one wl comment add call"

    # The posted comment content should contain the structured report
    comment_content = "\n".join(posted_comments)
    assert "# AMPA Audit Result" in comment_content
    assert "## Summary" in comment_content
    assert "All 3 acceptance criteria are met" in comment_content
    assert "## Acceptance Criteria Status" in comment_content
    assert "Widget renders" in comment_content
    assert "## Recommendation" in comment_content

    # The posted comment should NOT contain old-style labels or raw noise
    assert "Audit output:" not in comment_content
    assert "preamble noise" not in comment_content
    assert "trailing agent noise" not in comment_content
    # Delimiters themselves should not be in the comment
    assert "--- AUDIT REPORT START ---" not in comment_content
    assert "--- AUDIT REPORT END ---" not in comment_content

    # --- Verify Discord webhook ---
    assert webhook_payloads, "Expected at least one webhook payload"
    # The webhook should include the Summary section text
    payload_str = json.dumps(webhook_payloads[0])
    assert "All 3 acceptance criteria are met" in payload_str
