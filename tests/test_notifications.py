"""Tests for ampa.notifications module.

These tests verify the notification API's socket client, dead-letter
fallback, state-file tracking, and payload builders without requiring
a running Discord bot.  Where socket communication is needed, we spin
up a lightweight asyncio Unix socket server and run the synchronous
socket client in a background thread to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any, Dict, List

import pytest

from ampa.notifications import (
    DEFAULT_SOCKET_PATH,
    _default_deadletter_path,
    _read_state,
    _send_via_socket,
    _state_file_path,
    _truncate_output,
    _write_state,
    build_command_payload,
    build_payload,
    dead_letter,
    notify,
)


# ---------------------------------------------------------------------------
# Helpers: tiny echo/ack Unix socket server
# ---------------------------------------------------------------------------


class _FakeSocketServer:
    """A minimal Unix socket server that acknowledges messages."""

    def __init__(self, socket_path: str, *, ok: bool = True, error: str = ""):
        self.socket_path = socket_path
        self._ok = ok
        self._error = error
        self.received: List[Dict[str, Any]] = []
        self._server = None

    async def _handle(self, reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                data = json.loads(line)
                self.received.append(data)
            except Exception:
                pass
            resp = {"ok": self._ok}
            if self._error:
                resp["error"] = self._error
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle, path=self.socket_path
        )

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


def _run_sync_in_async(sync_fn, *args, **kwargs):
    """Run a synchronous function inside an async context by offloading to a
    thread.  Returns a coroutine that yields the result of ``sync_fn``."""

    async def _inner():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: sync_fn(*args, **kwargs))

    return _inner()


# ---------------------------------------------------------------------------
# Tests: payload builders (identical to webhook.py behavior)
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_basic_heartbeat(self):
        p = build_payload("host", "2026-01-01T00:00:00Z", None)
        assert p["content"].startswith("# AMPA Heartbeat")

    def test_custom_title(self):
        p = build_payload("host", "ts", title="Custom Title")
        assert "# Custom Title" in p["content"]

    def test_extra_fields(self):
        p = build_payload(
            "host",
            "ts",
            extra_fields=[
                {"name": "Summary", "value": "All good"},
                {"name": "Status", "value": "OK"},
            ],
        )
        assert "Summary: All good" in p["content"]
        assert "Status: OK" in p["content"]

    def test_no_extra_fields(self):
        p = build_payload("host", "ts", title="Test")
        assert p["content"] == "# Test"


class TestBuildCommandPayload:
    def test_basic(self):
        p = build_command_payload("host", "ts", "cmd1", "output text", 0, title="Done")
        assert p["content"].startswith("# Done")
        assert "output text" in p["content"]

    def test_no_output(self):
        p = build_command_payload("host", "ts", "cmd1", None, 0, title="Empty")
        assert p["content"] == "# Empty"

    def test_truncation(self):
        p = build_command_payload("host", "ts", "cmd1", "x" * 2000, 0, title="Big")
        assert "truncated" in p["content"]
        assert len(p["content"]) < 1200

    def test_empty_title_fallback(self):
        p = build_command_payload("host", "ts", "cmd1", "out", 0, title="")
        assert p["content"].startswith("# AMPA Notification")


# ---------------------------------------------------------------------------
# Tests: state helpers
# ---------------------------------------------------------------------------


class TestStateHelpers:
    def test_read_write_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        _write_state(path, {"a": "1", "b": "2"})
        state = _read_state(path)
        assert state == {"a": "1", "b": "2"}

    def test_read_missing_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        assert _read_state(path) == {}

    def test_read_corrupted_file(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("not json")
        assert _read_state(path) == {}

    def test_state_file_path_default(self, monkeypatch):
        monkeypatch.delenv("AMPA_STATE_FILE", raising=False)
        path = _state_file_path()
        assert "ampa_state.json" in path

    def test_state_file_path_custom(self, monkeypatch):
        monkeypatch.setenv("AMPA_STATE_FILE", "/custom/state.json")
        assert _state_file_path() == "/custom/state.json"


# ---------------------------------------------------------------------------
# Tests: dead_letter
# ---------------------------------------------------------------------------


class TestDeadLetter:
    def test_writes_to_file(self, tmp_path, monkeypatch):
        dl_file = str(tmp_path / "dead.log")
        monkeypatch.setenv("AMPA_DEADLETTER_FILE", dl_file)
        dead_letter({"content": "test"}, reason="socket down")
        with open(dl_file) as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["reason"] == "socket down"
        assert record["payload"]["content"] == "test"
        assert "ts" in record

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        dl_file = str(tmp_path / "subdir" / "dead.log")
        monkeypatch.setenv("AMPA_DEADLETTER_FILE", dl_file)
        dead_letter({"content": "test"})
        assert os.path.exists(dl_file)

    def test_default_path_is_project_local(self, monkeypatch):
        """Default dead-letter path should be under .worklog/ampa/."""
        monkeypatch.delenv("AMPA_DEADLETTER_FILE", raising=False)
        path = _default_deadletter_path()
        assert path.endswith(os.path.join(".worklog", "ampa", "deadletter.log"))

    def test_writes_to_default_path_without_env_var(self, tmp_path, monkeypatch):
        """Dead-letter writes succeed on a fresh install with no env var."""
        monkeypatch.delenv("AMPA_DEADLETTER_FILE", raising=False)
        # Point cwd at tmp_path so the default path is writable.
        monkeypatch.chdir(tmp_path)
        dead_letter({"content": "default-path-test"}, reason="test")
        expected = tmp_path / ".worklog" / "ampa" / "deadletter.log"
        assert expected.exists()
        record = json.loads(expected.read_text().strip())
        assert record["reason"] == "test"
        assert record["payload"]["content"] == "default-path-test"

    def test_env_var_override_still_works(self, tmp_path, monkeypatch):
        """AMPA_DEADLETTER_FILE env var overrides the default path."""
        custom_file = str(tmp_path / "custom_dead.log")
        monkeypatch.setenv("AMPA_DEADLETTER_FILE", custom_file)
        dead_letter({"content": "override"}, reason="custom")
        assert os.path.exists(custom_file)
        record = json.loads(open(custom_file).readline())
        assert record["payload"]["content"] == "override"


# ---------------------------------------------------------------------------
# Tests: _truncate_output
# ---------------------------------------------------------------------------


class TestTruncateOutput:
    def test_short_not_truncated(self):
        assert _truncate_output("hello", limit=10) == "hello"

    def test_long_truncated(self):
        result = _truncate_output("x" * 100, limit=10)
        assert len(result) < 100
        assert "truncated" in result


# ---------------------------------------------------------------------------
# Tests: _send_via_socket
# ---------------------------------------------------------------------------


class TestSendViaSocket:
    def test_successful_send(self, tmp_path):
        sock = str(tmp_path / "test.sock")

        async def _test():
            srv = _FakeSocketServer(sock)
            await srv.start()
            try:
                result = await _run_sync_in_async(
                    _send_via_socket, sock, {"content": "hello"}
                )
                assert result is True
                assert len(srv.received) == 1
                assert srv.received[0]["content"] == "hello"
            finally:
                await srv.stop()

        asyncio.run(_test())

    def test_socket_not_found(self, tmp_path):
        sock = str(tmp_path / "nonexistent.sock")
        result = _send_via_socket(sock, {"content": "hello"})
        assert result is False

    def test_bot_returns_error(self, tmp_path):
        sock = str(tmp_path / "test.sock")

        async def _test():
            srv = _FakeSocketServer(sock, ok=False, error="channel not found")
            await srv.start()
            try:
                result = await _run_sync_in_async(
                    _send_via_socket, sock, {"content": "hello"}
                )
                assert result is False
            finally:
                await srv.stop()

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: notify
# ---------------------------------------------------------------------------


class TestNotify:
    def test_notify_with_title_and_body(self, tmp_path, monkeypatch):
        sock = str(tmp_path / "test.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)
        state_file = str(tmp_path / "state.json")
        monkeypatch.setenv("AMPA_STATE_FILE", state_file)
        dl_file = str(tmp_path / "dead.log")
        monkeypatch.setenv("AMPA_DEADLETTER_FILE", dl_file)

        async def _test():
            srv = _FakeSocketServer(sock)
            await srv.start()
            try:
                result = await _run_sync_in_async(
                    notify, "Test Title", "Test body", "command"
                )
                assert result is True
                assert len(srv.received) == 1
                msg = srv.received[0]
                assert msg["content"] == "# Test Title\n\nTest body"
                assert msg["message_type"] == "command"
            finally:
                await srv.stop()

        asyncio.run(_test())

        # State file should be updated
        state = _read_state(state_file)
        assert state["last_message_type"] == "command"
        assert "last_message_ts" in state

    def test_notify_title_only(self, tmp_path, monkeypatch):
        sock = str(tmp_path / "test.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)
        state_file = str(tmp_path / "state.json")
        monkeypatch.setenv("AMPA_STATE_FILE", state_file)

        async def _test():
            srv = _FakeSocketServer(sock)
            await srv.start()
            try:
                result = await _run_sync_in_async(notify, "Just Title", "", "startup")
                assert result is True
                assert srv.received[0]["content"] == "# Just Title"
            finally:
                await srv.stop()

        asyncio.run(_test())

    def test_notify_with_payload(self, tmp_path, monkeypatch):
        sock = str(tmp_path / "test.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)
        state_file = str(tmp_path / "state.json")
        monkeypatch.setenv("AMPA_STATE_FILE", state_file)

        async def _test():
            srv = _FakeSocketServer(sock)
            await srv.start()
            try:
                payload = {"content": "pre-built message"}
                # notify() with payload kwarg — need to use a lambda to pass kwargs
                result = await _run_sync_in_async(
                    lambda: notify("ignored", "ignored", payload=payload)
                )
                assert result is True
                assert srv.received[0]["content"] == "pre-built message"
            finally:
                await srv.stop()

        asyncio.run(_test())

    def test_notify_empty_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", str(tmp_path / "test.sock"))
        result = notify("", "")
        assert result is False

    def test_notify_dead_letters_on_socket_failure(self, tmp_path, monkeypatch):
        sock = str(tmp_path / "nonexistent.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)
        state_file = str(tmp_path / "state.json")
        monkeypatch.setenv("AMPA_STATE_FILE", state_file)
        dl_file = str(tmp_path / "dead.log")
        monkeypatch.setenv("AMPA_DEADLETTER_FILE", dl_file)

        result = notify("Error Title", "Error body", message_type="error")
        assert result is False

        # Dead-letter file should exist
        assert os.path.exists(dl_file)
        with open(dl_file) as f:
            record = json.loads(f.readline())
        assert record["reason"] == "Unix socket unreachable"
        assert record["payload"]["content"] == "# Error Title\n\nError body"

        # State should still be updated (matches legacy behavior)
        state = _read_state(state_file)
        assert state["last_message_type"] == "error"

    def test_notify_heartbeat_message_type(self, tmp_path, monkeypatch):
        sock = str(tmp_path / "test.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)
        state_file = str(tmp_path / "state.json")
        monkeypatch.setenv("AMPA_STATE_FILE", state_file)

        async def _test():
            srv = _FakeSocketServer(sock)
            await srv.start()
            try:
                result = await _run_sync_in_async(
                    notify, "AMPA Heartbeat", "", "heartbeat"
                )
                assert result is True
            finally:
                await srv.stop()

        asyncio.run(_test())

        state = _read_state(state_file)
        assert state["last_message_type"] == "heartbeat"

    def test_notify_body_only(self, tmp_path, monkeypatch):
        sock = str(tmp_path / "test.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)
        state_file = str(tmp_path / "state.json")
        monkeypatch.setenv("AMPA_STATE_FILE", state_file)

        async def _test():
            srv = _FakeSocketServer(sock)
            await srv.start()
            try:
                result = await _run_sync_in_async(notify, "", "Just body text", "other")
                assert result is True
                assert srv.received[0]["content"] == "Just body text"
            finally:
                await srv.stop()

        asyncio.run(_test())
