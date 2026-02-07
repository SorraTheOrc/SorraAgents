import json
from types import SimpleNamespace

import pytest

import ampa.scheduler as sched_mod
from ampa.scheduler import Scheduler, SchedulerStore, SchedulerConfig


def test_post_startup_message_uses_wl_status(tmp_path, monkeypatch):
    # prepare a minimal store file
    store_path = tmp_path / "store.json"
    store_path.write_text(json.dumps({"commands": {}, "state": {}}))
    store = SchedulerStore(str(store_path))

    # build a simple config
    config = SchedulerConfig(
        poll_interval_seconds=5,
        global_min_interval_seconds=60,
        priority_weight=0.1,
        store_path=str(store_path),
        llm_healthcheck_url="http://localhost/health",
        max_run_history=50,
    )

    # fake run_shell that returns wl status output on stdout
    def fake_run_shell(cmd, shell, check, capture_output, text, cwd):
        return SimpleNamespace(
            returncode=0, stdout="WL status: all good\n1 in_progress\n", stderr=""
        )

    # capture payload sent to webhook
    captured = {}

    def fake_send_webhook(url, payload, timeout=10, message_type="other"):
        captured["url"] = url
        captured["payload"] = payload
        captured["message_type"] = message_type

    # ensure daemon.get_env_config returns a webhook so _post_startup_message proceeds
    monkeypatch.setattr(
        sched_mod.daemon,
        "get_env_config",
        lambda: {"webhook": "http://example.com/webhook"},
    )
    # replace send_webhook on the imported webhook_module used by scheduler
    monkeypatch.setattr(sched_mod.webhook_module, "send_webhook", fake_send_webhook)

    sched = Scheduler(
        store, config, run_shell=fake_run_shell, command_cwd=str(tmp_path)
    )

    # call the protected method directly and assert the payload contains the wl status text
    sched._post_startup_message()

    assert "payload" in captured, "send_webhook was not called"
    content = captured["payload"]["content"]
    assert content.startswith("# Scheduler Started")
    assert "WL status: all good" in content
