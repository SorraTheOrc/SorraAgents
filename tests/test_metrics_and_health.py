import json
import os
import time
import urllib.error
import urllib.request

from ampa.metrics import start_metrics_server, ampa_heartbeat_sent_total
from ampa.metrics import (
    ampa_heartbeat_failure_total,
    ampa_last_heartbeat_timestamp_seconds,
)
from ampa import conversation_manager
import session_block


def test_health_and_metrics_ok(tmp_path, monkeypatch):
    # Ensure webhook env is present -> /health returns 200
    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "https://example.com/webhook")
    server, port = start_metrics_server(port=0)
    url = f"http://127.0.0.1:{port}"

    # Health should be OK
    resp = urllib.request.urlopen(f"{url}/health")
    assert resp.status == 200
    body = resp.read().decode()
    assert "OK" in body

    # Metrics endpoint should include our metric names
    resp = urllib.request.urlopen(f"{url}/metrics")
    data = resp.read().decode()
    assert "ampa_heartbeat_sent_total" in data
    assert "ampa_heartbeat_failure_total" in data
    assert "ampa_last_heartbeat_timestamp_seconds" in data


def test_health_misconfigured(tmp_path, monkeypatch):
    # Remove webhook -> /health returns 503
    monkeypatch.delenv("AMPA_DISCORD_WEBHOOK", raising=False)
    server, port = start_metrics_server(port=0)
    url = f"http://127.0.0.1:{port}"

    try:
        urllib.request.urlopen(f"{url}/health")
        raised = False
    except urllib.error.HTTPError as exc:
        raised = True
        assert exc.code == 503
    assert raised


def test_responder_endpoint_resumes_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    session_id = "s-respond"
    conversation_manager.start_conversation(session_id, "Approve?")

    server, port = start_metrics_server(port=0)
    url = f"http://127.0.0.1:{port}/respond"
    payload = json.dumps({"session_id": session_id, "response": "yes"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200
    body = json.loads(resp.read().decode())
    assert body["status"] == "resumed"
    assert body["session"] == session_id


def test_session_state_endpoint_returns_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    session_id = "s-session"
    conversation_manager.start_conversation(session_id, "Confirm?")

    server, port = start_metrics_server(port=0)
    url = f"http://127.0.0.1:{port}/session/{session_id}"
    resp = urllib.request.urlopen(url)
    assert resp.status == 200
    body = json.loads(resp.read().decode())
    assert body["session"] == session_id
    assert body["state"] == "waiting_for_input"


def test_admin_fallback_controls_responder(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AMPA_ADMIN_TOKEN", "secret-token")
    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "https://example.com/webhook")

    monkeypatch.setattr(
        session_block.webhook_module,
        "send_webhook",
        lambda *args, **kwargs: 204,
    )

    session_id = "s-fallback"
    conversation_manager.start_conversation(session_id, "Approve?")

    server, port = start_metrics_server(port=0)
    base = f"http://127.0.0.1:{port}"

    cfg_payload = json.dumps(
        {"default": "hold", "projects": {"proj-1": "auto-accept"}}
    ).encode("utf-8")
    cfg_req = urllib.request.Request(
        f"{base}/admin/fallback",
        data=cfg_payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token",
        },
        method="POST",
    )
    cfg_resp = urllib.request.urlopen(cfg_req)
    assert cfg_resp.status == 200

    resp_req = urllib.request.Request(
        f"{base}/respond",
        data=json.dumps({"session_id": session_id, "project_id": "proj-1"}).encode(
            "utf-8"
        ),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(resp_req)
    assert resp.status == 200
    body = json.loads(resp.read().decode())
    assert body["status"] == "resumed"
    assert body["session"] == session_id
    assert body["response"] == "accept"


def test_admin_fallback_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AMPA_ADMIN_TOKEN", "secret-token")

    server, port = start_metrics_server(port=0)
    base = f"http://127.0.0.1:{port}"

    req = urllib.request.Request(
        f"{base}/admin/fallback",
        data=json.dumps({"default": "hold"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        raised = False
    except urllib.error.HTTPError as exc:
        raised = True
        assert exc.code == 401
    assert raised
