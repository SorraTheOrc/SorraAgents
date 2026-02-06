import os
import time
import urllib.request

from ampa.metrics import start_metrics_server, ampa_heartbeat_sent_total
from ampa.metrics import (
    ampa_heartbeat_failure_total,
    ampa_last_heartbeat_timestamp_seconds,
)


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
