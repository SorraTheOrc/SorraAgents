import datetime
import json
import os

import pytest

from ampa.daemon import get_env_config, run_once


class DummyResp:
    def __init__(self, code=204, text=""):
        self.status_code = code
        self.text = text

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise Exception(f"HTTP {self.status_code}")


def write_state(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def read_state(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_heartbeat_skipped_when_other_message_since_last_heartbeat(
    monkeypatch, tmp_path
):
    state_file = tmp_path / "ampa_state.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    # last heartbeat was 60s ago, other message 30s ago -> skip heartbeat
    write_state(
        state_file,
        {
            "last_heartbeat_ts": (now - datetime.timedelta(seconds=60)).isoformat(),
            "last_message_ts": (now - datetime.timedelta(seconds=30)).isoformat(),
            "last_message_type": "other",
        },
    )

    monkeypatch.setenv("AMPA_STATE_FILE", str(state_file))
    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid")
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")

    called = {"count": 0}

    def fake_post(self, url, json=None, timeout=None):
        called["count"] += 1
        return DummyResp()

    monkeypatch.setattr("ampa.daemon.requests.Session.post", fake_post)

    cfg = get_env_config()
    status = run_once(cfg)
    assert status == 0
    assert called["count"] == 0


def test_heartbeat_sent_and_updates_state(monkeypatch, tmp_path):
    state_file = tmp_path / "ampa_state.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    # last heartbeat was 60s ago, last message was 120s ago -> heartbeat should send
    write_state(
        state_file,
        {
            "last_heartbeat_ts": (now - datetime.timedelta(seconds=60)).isoformat(),
            "last_message_ts": (now - datetime.timedelta(seconds=120)).isoformat(),
            "last_message_type": "other",
        },
    )

    monkeypatch.setenv("AMPA_STATE_FILE", str(state_file))
    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid")
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")

    def fake_post(self, url, json=None, timeout=None):
        return DummyResp(204)

    monkeypatch.setattr("ampa.daemon.requests.Session.post", fake_post)

    cfg = get_env_config()
    status = run_once(cfg)
    assert status == 204

    st = read_state(state_file)
    assert st.get("last_message_type") == "heartbeat"
    assert "last_heartbeat_ts" in st


def test_initial_heartbeat_when_no_state(monkeypatch, tmp_path):
    state_file = tmp_path / "ampa_state.json"
    if state_file.exists():
        state_file.unlink()

    monkeypatch.setenv("AMPA_STATE_FILE", str(state_file))
    monkeypatch.setenv("AMPA_DISCORD_WEBHOOK", "http://example.invalid")
    monkeypatch.setenv("AMPA_LOAD_DOTENV", "0")

    def fake_post(self, url, json=None, timeout=None):
        return DummyResp(204)

    monkeypatch.setattr("ampa.daemon.requests.Session.post", fake_post)

    cfg = get_env_config()
    status = run_once(cfg)
    assert status == 204

    st = read_state(state_file)
    assert st.get("last_message_type") == "heartbeat"
    assert "last_heartbeat_ts" in st
