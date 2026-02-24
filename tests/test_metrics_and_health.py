import json
import os
import time
import urllib.error
import urllib.request

import pytest

from ampa.server import start_metrics_server, ampa_heartbeat_sent_total
from ampa.server import (
    ampa_heartbeat_failure_total,
    ampa_last_heartbeat_timestamp_seconds,
)
from ampa import conversation_manager
from ampa import session_block


@pytest.fixture()
def metrics_server(monkeypatch):
    """Start a metrics server and shut it down after the test.

    Yields (base_url, server) so tests can make requests and the server
    is properly cleaned up, preventing leaked threads.
    """
    servers = []

    def _start(**kwargs):
        server, port = start_metrics_server(port=0, **kwargs)
        servers.append(server)
        return f"http://127.0.0.1:{port}", server

    yield _start

    for srv in servers:
        if srv._server:
            srv._server[0].shutdown()


def test_health_and_metrics_ok(tmp_path, monkeypatch, metrics_server):
    # Ensure bot token env is present -> /health returns 200
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-bot-token")
    url, server = metrics_server()

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


def test_health_misconfigured(tmp_path, monkeypatch, metrics_server):
    # Remove bot token -> /health returns 503
    monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
    url, server = metrics_server()

    try:
        urllib.request.urlopen(f"{url}/health")
        raised = False
    except urllib.error.HTTPError as exc:
        raised = True
        assert exc.code == 503
    assert raised


def test_responder_endpoint_resumes_session(tmp_path, monkeypatch, metrics_server):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    session_id = "s-respond"
    conversation_manager.start_conversation(session_id, "Approve?")

    url, server = metrics_server()
    payload = json.dumps({"session_id": session_id, "response": "yes"}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/respond",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200
    body = json.loads(resp.read().decode())
    assert body["status"] == "resumed"
    assert body["session"] == session_id


def test_session_state_endpoint_returns_state(tmp_path, monkeypatch, metrics_server):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    session_id = "s-session"
    conversation_manager.start_conversation(session_id, "Confirm?")

    url, server = metrics_server()
    resp = urllib.request.urlopen(f"{url}/session/{session_id}")
    assert resp.status == 200
    body = json.loads(resp.read().decode())
    assert body["session"] == session_id
    assert body["state"] == "waiting_for_input"


def test_admin_fallback_controls_responder(tmp_path, monkeypatch, metrics_server):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AMPA_ADMIN_TOKEN", "secret-token")
    monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "test-bot-token")

    monkeypatch.setattr(
        session_block.notifications_module,
        "notify",
        lambda *args, **kwargs: True,
    )

    session_id = "s-fallback"
    conversation_manager.start_conversation(session_id, "Approve?")

    base, server = metrics_server()

    cfg_payload = json.dumps(
        {
            "default": "hold",
            "public_default": "hold",
            "projects": {"proj-1": "auto-accept"},
        }
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


def test_responder_public_default_applies_when_project_missing(
    tmp_path, monkeypatch, metrics_server
):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AMPA_ADMIN_TOKEN", "secret-token")

    base, server = metrics_server()

    cfg_payload = json.dumps(
        {"default": "auto-accept", "public_default": "hold", "projects": {}}
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

    session_id = "s-public"
    conversation_manager.start_conversation(session_id, "Approve?")

    resp_req = urllib.request.Request(
        f"{base}/respond",
        data=json.dumps({"session_id": session_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(resp_req)
        raised = False
    except urllib.error.HTTPError as exc:
        raised = True
        assert exc.code == 400
        body = json.loads(exc.read().decode())
        assert "payload missing response" in body["error"]
    assert raised


def test_admin_fallback_requires_token(tmp_path, monkeypatch, metrics_server):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("AMPA_ADMIN_TOKEN", "secret-token")

    base, server = metrics_server()

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
