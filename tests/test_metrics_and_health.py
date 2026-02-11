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
