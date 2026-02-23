"""Tests for ampa.discord_bot module.

These tests verify the AMPABot class without connecting to Discord.  They
exercise the Unix socket protocol, message parsing, and error handling by
mocking the discord.py Client and channel objects.

NOTE: Tests avoid ``pytest-asyncio`` (not installed) and instead run async
code via ``asyncio.run()`` or ``loop.run_until_complete()`` inside regular
synchronous test functions.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from ampa.discord_bot import AMPABot, DEFAULT_SOCKET_PATH, MAX_MESSAGE_SIZE, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeChannel:
    """Minimal fake for discord.TextChannel."""

    def __init__(self, name: str = "test-channel", channel_id: int = 12345):
        self.name = name
        self.id = channel_id
        self.sent: List[str] = []

    async def send(self, content: str) -> None:
        self.sent.append(content)


async def _send_socket_messages(
    socket_path: str,
    messages: List[Dict[str, Any]],
    timeout: float = 5.0,
) -> List[Dict[str, Any]]:
    """Connect to the Unix socket and send JSON messages, collecting responses."""
    responses: List[Dict[str, Any]] = []
    reader, writer = await asyncio.open_unix_connection(socket_path)
    for msg in messages:
        line = json.dumps(msg) + "\n"
        writer.write(line.encode())
        await writer.drain()
        resp_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if resp_line:
            responses.append(json.loads(resp_line))
    writer.close()
    await writer.wait_closed()
    return responses


async def _run_socket_test(bot, socket_path, messages):
    """Start socket server, send messages, stop server, return responses."""
    await bot._start_socket_server()
    try:
        return await _send_socket_messages(socket_path, messages)
    finally:
        if bot._server:
            bot._server.close()
            await bot._server.wait_closed()


# ---------------------------------------------------------------------------
# Tests: AMPABot socket protocol
# ---------------------------------------------------------------------------


class TestAMPABotSocketProtocol:
    """Test the Unix socket server and message handling."""

    @pytest.fixture
    def socket_path(self, tmp_path):
        return str(tmp_path / "test_bot.sock")

    @pytest.fixture
    def bot(self, socket_path):
        return AMPABot(
            token="fake-token",
            channel_id=12345,
            socket_path=socket_path,
        )

    @pytest.fixture
    def fake_channel(self):
        return FakeChannel()

    def test_send_content_message(self, bot, socket_path, fake_channel):
        """Bot sends content field as Discord message."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"content": "Hello from AMPA"}]
            )
            assert len(responses) == 1
            assert responses[0]["ok"] is True
            assert fake_channel.sent == ["Hello from AMPA"]

        asyncio.run(_test())

    def test_send_title_body_message(self, bot, socket_path, fake_channel):
        """Bot constructs content from title + body when content is absent."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"title": "Test Title", "body": "Test body text"}]
            )
            assert len(responses) == 1
            assert responses[0]["ok"] is True
            assert len(fake_channel.sent) == 1
            assert fake_channel.sent[0] == "# Test Title\n\nTest body text"

        asyncio.run(_test())

    def test_send_title_only(self, bot, socket_path, fake_channel):
        """Bot handles title without body."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"title": "Just a Title"}]
            )
            assert responses[0]["ok"] is True
            assert fake_channel.sent == ["# Just a Title"]

        asyncio.run(_test())

    def test_send_body_only(self, bot, socket_path, fake_channel):
        """Bot handles body without title."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"body": "Just a body"}]
            )
            assert responses[0]["ok"] is True
            assert fake_channel.sent == ["Just a body"]

        asyncio.run(_test())

    def test_empty_message_rejected(self, bot, socket_path, fake_channel):
        """Bot rejects messages with no content, title, or body."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"message_type": "heartbeat"}]
            )
            assert responses[0]["ok"] is False
            assert "empty message" in responses[0]["error"]
            assert len(fake_channel.sent) == 0

        asyncio.run(_test())

    def test_invalid_json_rejected(self, bot, socket_path, fake_channel):
        """Bot rejects non-JSON input."""

        async def _test():
            bot._channel = fake_channel
            await bot._start_socket_server()
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(b"not valid json\n")
                await writer.drain()
                resp_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                resp = json.loads(resp_line)
                assert resp["ok"] is False
                assert "invalid JSON" in resp["error"]
                writer.close()
                await writer.wait_closed()
            finally:
                if bot._server:
                    bot._server.close()
                    await bot._server.wait_closed()

        asyncio.run(_test())

    def test_multiple_messages_in_one_connection(self, bot, socket_path, fake_channel):
        """Bot handles multiple messages on a single connection."""

        async def _test():
            bot._channel = fake_channel
            messages = [
                {"content": "Message 1"},
                {"content": "Message 2"},
                {"content": "Message 3"},
            ]
            responses = await _run_socket_test(bot, socket_path, messages)
            assert len(responses) == 3
            assert all(r["ok"] for r in responses)
            assert fake_channel.sent == ["Message 1", "Message 2", "Message 3"]

        asyncio.run(_test())

    def test_discord_message_truncated_at_2000_chars(
        self, bot, socket_path, fake_channel
    ):
        """Messages exceeding 2000 chars are truncated before sending to Discord."""

        async def _test():
            bot._channel = fake_channel
            long_content = "x" * 2500
            responses = await _run_socket_test(
                bot, socket_path, [{"content": long_content}]
            )
            assert responses[0]["ok"] is True
            assert len(fake_channel.sent) == 1
            assert len(fake_channel.sent[0]) == 2000
            assert fake_channel.sent[0].endswith("...")

        asyncio.run(_test())

    def test_channel_not_resolved_returns_error(self, bot, socket_path):
        """If channel is not resolved, sending fails gracefully."""

        async def _test():
            bot._channel = None
            responses = await _run_socket_test(bot, socket_path, [{"content": "Hello"}])
            assert responses[0]["ok"] is False
            assert "failed to send" in responses[0].get("error", "")

        asyncio.run(_test())

    def test_discord_send_failure(self, bot, socket_path):
        """If Discord channel.send() raises, bot reports failure."""

        async def _test():
            channel = FakeChannel()

            async def fail_send(content):
                raise RuntimeError("Discord API error")

            channel.send = fail_send
            bot._channel = channel

            responses = await _run_socket_test(bot, socket_path, [{"content": "Hello"}])
            assert responses[0]["ok"] is False

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: _send_to_discord
# ---------------------------------------------------------------------------


class TestSendToDiscord:
    def test_send_returns_true_on_success(self):
        async def _test():
            ch = FakeChannel()
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = ch
            result = await bot._send_to_discord("hello")
            assert result is True
            assert ch.sent == ["hello"]

        asyncio.run(_test())

    def test_send_returns_false_when_no_channel(self):
        async def _test():
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = None
            result = await bot._send_to_discord("hello")
            assert result is False

        asyncio.run(_test())

    def test_send_returns_false_on_exception(self):
        async def _test():
            ch = FakeChannel()

            async def raise_err(content):
                raise Exception("boom")

            ch.send = raise_err
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = ch
            result = await bot._send_to_discord("hello")
            assert result is False

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: socket cleanup
# ---------------------------------------------------------------------------


class TestSocketCleanup:
    def test_cleanup_removes_socket_file(self, tmp_path):
        sock_path = str(tmp_path / "test.sock")
        with open(sock_path, "w") as f:
            f.write("")
        bot = AMPABot(token="t", channel_id=1, socket_path=sock_path)
        bot._cleanup_socket()
        assert not os.path.exists(sock_path)

    def test_cleanup_no_error_if_missing(self, tmp_path):
        sock_path = str(tmp_path / "nonexistent.sock")
        bot = AMPABot(token="t", channel_id=1, socket_path=sock_path)
        bot._cleanup_socket()  # Should not raise


# ---------------------------------------------------------------------------
# Tests: shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_closes_server_and_client(self):
        async def _test():
            bot = AMPABot(token="t", channel_id=1)

            # Fake server
            class FakeServer:
                closed = False

                def close(self):
                    self.closed = True

                async def wait_closed(self):
                    pass

            # Fake client
            class FakeClient:
                closed = False

                async def close(self):
                    self.closed = True

            server = FakeServer()
            client = FakeClient()
            bot._server = server
            bot._client = client

            await bot._shutdown()
            assert server.closed
            assert client.closed
            assert bot._server is None

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: CLI entry point (main)
# ---------------------------------------------------------------------------


class TestMain:
    def test_missing_token_exits(self, monkeypatch):
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("AMPA_DISCORD_CHANNEL_ID", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_missing_channel_id_exits(self, monkeypatch):
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        monkeypatch.delenv("AMPA_DISCORD_CHANNEL_ID", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_invalid_channel_id_exits(self, monkeypatch):
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("AMPA_DISCORD_CHANNEL_ID", "not-a-number")
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_valid_config_creates_bot(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("AMPA_DISCORD_CHANNEL_ID", "12345")
        sock = str(tmp_path / "test.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)

        run_called: List[Dict[str, Any]] = []

        def fake_run(self):
            run_called.append(
                {
                    "token": self.token,
                    "channel_id": self.channel_id,
                    "socket_path": self.socket_path,
                }
            )

        monkeypatch.setattr(AMPABot, "run", fake_run)
        main()
        assert len(run_called) == 1
        assert run_called[0]["token"] == "fake-token"
        assert run_called[0]["channel_id"] == 12345
        assert run_called[0]["socket_path"] == sock

    def test_default_socket_path_used(self, monkeypatch):
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("AMPA_DISCORD_CHANNEL_ID", "12345")
        monkeypatch.delenv("AMPA_BOT_SOCKET_PATH", raising=False)

        run_called: List[Dict[str, Any]] = []

        def fake_run(self):
            run_called.append({"socket_path": self.socket_path})

        monkeypatch.setattr(AMPABot, "run", fake_run)
        main()
        assert run_called[0]["socket_path"] == DEFAULT_SOCKET_PATH


# ---------------------------------------------------------------------------
# Tests: AMPABot initialization
# ---------------------------------------------------------------------------


class TestAMPABotInit:
    def test_default_socket_path(self):
        bot = AMPABot(token="t", channel_id=123)
        assert bot.socket_path == DEFAULT_SOCKET_PATH
        assert bot.token == "t"
        assert bot.channel_id == 123

    def test_custom_socket_path(self):
        bot = AMPABot(token="t", channel_id=123, socket_path="/custom/path.sock")
        assert bot.socket_path == "/custom/path.sock"

    def test_initial_state_is_none(self):
        bot = AMPABot(token="t", channel_id=123)
        assert bot._channel is None
        assert bot._client is None
        assert bot._server is None

    def test_discord_import_error(self, monkeypatch):
        """If discord.py is not installed, bot.run() exits with code 1."""
        bot = AMPABot(token="t", channel_id=123)

        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "discord":
                raise ImportError("No module named 'discord'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        with pytest.raises(SystemExit) as exc_info:
            bot.run()
        assert exc_info.value.code == 1
