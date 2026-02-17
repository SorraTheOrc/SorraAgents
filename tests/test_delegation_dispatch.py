import json
import subprocess
import datetime as dt

from ampa import scheduler
from ampa.scheduler import CommandSpec, SchedulerConfig, SchedulerStore, Scheduler
from ampa import webhook as webhook_module


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        self.path = ":memory:"
        self.data = {
            "commands": {},
            "state": {},
            "last_global_start_ts": None,
            "config": {},
            "dispatches": [],
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


def test_dispatch_logged_before_spawn(tmp_path, monkeypatch):
    """Verify a dispatch record is persisted before opencode is spawned.

    This test patches the store.append_dispatch to record that it ran and
    ensures the opencode child process is invoked only after the dispatch
    persistence has occurred. It also checks that a pre-dispatch webhook was
    issued containing the dispatch id.
    """
    calls = []
    state = {"append_called": False}
    captured = {"calls": []}

    # fake wl in_progress -> no in-progress items
    # wl next -> return a candidate
    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        s = cmd.strip()
        if s == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if s == "wl next --json":
            payload = {
                "workItem": {"id": "SA-TEST-123", "title": "Idea item", "stage": "idea"}
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if s.startswith("opencode run"):
            # opencode should only be called after append_dispatch
            assert state["append_called"] is True, (
                "opencode spawned before dispatch was recorded"
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)

    # patch append_dispatch to mark it called and return a stable id
    def fake_append_dispatch(record, retain_last=100):
        state["append_called"] = True
        # mimic normal behaviour of assigning id and ts
        record = dict(record)
        record.setdefault("id", "DISPATCH-1")
        record.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
        sched.store.data.setdefault("dispatches", []).append(record)
        return record["id"]

    monkeypatch.setattr(sched.store, "append_dispatch", fake_append_dispatch)

    # capture webhook calls and assert message_type dispatch and payload contains id
    def fake_send_webhook(url, payload, timeout=10, message_type="other"):
        captured["calls"].append(
            {"url": url, "payload": payload, "message_type": message_type}
        )
        return 200

    monkeypatch.setattr(webhook_module, "send_webhook", fake_send_webhook)

    spec = CommandSpec(
        command_id="delegation",
        command="",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        title="Delegation",
        command_type="delegation",
    )
    sched.store.add_command(spec)

    # ensure webhook env is present so pre-dispatch path runs
    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid/webhook")

    # run start_command which triggers the delegation flow
    sched.start_command(spec)

    # verify opencode was invoked
    assert any(c.startswith("opencode run") for c in calls)
    # verify append_dispatch was called before opencode (assertion in fake_run_shell)
    assert state["append_called"] is True
    # verify a dispatch webhook was sent and included the dispatch id
    assert any(c.get("message_type") == "dispatch" for c in captured["calls"]), (
        "no dispatch webhook sent"
    )
    # find the dispatch call and verify content is the human-friendly message
    dispatch_calls = [
        c for c in captured["calls"] if c.get("message_type") == "dispatch"
    ]
    assert dispatch_calls, "no dispatch webhook recorded"
    content = (dispatch_calls[-1].get("payload") or {}).get("content", "")
    assert "Delegating" in content
    assert "SA-TEST-123" in content


def test_dispatch_uses_implement_skill_command(tmp_path, monkeypatch):
    calls = []

    def fake_run_shell(cmd, **kwargs):
        calls.append(cmd)
        s = cmd.strip()
        if s == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if s.startswith("wl next") and "--json" in s:
            payload = {
                "workItem": {
                    "id": "SA-IMPL-1",
                    "title": "Plan item",
                    "stage": "plan_complete",
                }
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if s.startswith("opencode run"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = make_scheduler(fake_run_shell, tmp_path)

    spec = CommandSpec(
        command_id="delegation",
        command="",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        title="Delegation",
        command_type="delegation",
    )
    sched.store.add_command(spec)

    sched.start_command(spec)

    assert any(
        'opencode run "work on SA-IMPL-1 using the implement skill"' in c for c in calls
    )
