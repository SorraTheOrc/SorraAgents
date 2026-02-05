import datetime
import socket

import pytest

from ampa.daemon import build_payload, get_env_config


def test_build_payload_includes_hostname_and_timestamp():
    hostname = "test-host"
    ts = datetime.datetime(
        2020, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc
    ).isoformat()
    payload = build_payload(hostname, ts, work_item_id="SA-123")
    assert "content" in payload
    content = payload["content"]
    assert "Host: test-host" in content
    assert "Timestamp: 2020-01-02T03:04:05+00:00" in content
    assert "work_item_id: SA-123" in content


def test_get_env_config_missing_webhook(monkeypatch):
    monkeypatch.delenv("AMPA_DISCORD_WEBHOOK", raising=False)
    monkeypatch.setenv("AMPA_HEARTBEAT_MINUTES", "1")
    # Ensure package .env is not loaded during this test so the missing-webhook
    # behavior is exercised even when ampa/.env exists in the repository.
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")
    with pytest.raises(SystemExit):
        get_env_config()


def test_get_env_config_invalid_minutes(monkeypatch):
    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.com")
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")
    monkeypatch.setenv("AMPA_HEARTBEAT_MINUTES", "-5")
    cfg = get_env_config()
    assert cfg["minutes"] == 1
