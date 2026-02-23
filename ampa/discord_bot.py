"""Discord bot process for AMPA notifications.

This module implements a Discord bot that listens on a Unix domain socket for
incoming notification requests and sends them as messages to a configured
Discord channel.  It replaces the webhook-based notification system with a
persistent bot connection.

Usage::

    python -m ampa.discord_bot

Environment variables:

- ``AMPA_DISCORD_BOT_TOKEN``  – Discord bot token (required)
- ``AMPA_DISCORD_CHANNEL_ID`` – Target channel ID as an integer (required)
- ``AMPA_BOT_SOCKET_PATH``    – Unix socket path (default: ``/tmp/ampa_bot.sock``)

The bot accepts newline-delimited JSON messages on the Unix socket.  Each
message must be a JSON object; it is sent to the configured Discord channel as
a plain-text message using the ``content`` field (matching the existing webhook
payload format ``{"content": "..."}``) .

Protocol
--------
Each client connection may send one or more JSON messages separated by
newlines.  The bot reads each line, deserializes it, and sends it to Discord.
A JSON response is written back per message::

    {"ok": true}           # success
    {"ok": false, "error": "..."} # failure

The connection is closed by the client when done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Optional

LOG = logging.getLogger("ampa.discord_bot")

# Default socket path
DEFAULT_SOCKET_PATH = "/tmp/ampa_bot.sock"

# Maximum message size we'll accept on the socket (64 KiB).
MAX_MESSAGE_SIZE = 65_536


class AMPABot:
    """Thin wrapper around a ``discord.Client`` that also runs a Unix socket
    server for receiving notification requests from synchronous callers."""

    def __init__(
        self,
        token: str,
        channel_id: int,
        socket_path: str = DEFAULT_SOCKET_PATH,
    ) -> None:
        self.token = token
        self.channel_id = channel_id
        self.socket_path = socket_path

        self._channel: Optional[object] = None  # discord.TextChannel once resolved
        self._client: Optional[object] = None  # discord.Client
        self._server: Optional[asyncio.AbstractServer] = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the bot and socket server.  Blocks until shutdown."""
        try:
            import discord  # type: ignore
        except ImportError:
            LOG.error(
                "discord.py is not installed.  Install it with: pip install discord.py"
            )
            sys.exit(1)

        intents = discord.Intents.default()
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready() -> None:
            LOG.info(
                "Connected to Discord as %s (id=%s)",
                client.user,
                client.user.id if client.user else "?",
            )
            channel = client.get_channel(self.channel_id)
            if channel is None:
                LOG.error(
                    "Channel ID %s not found in any server the bot has access to.  "
                    "Ensure the bot is invited to the correct server and the "
                    "channel ID is valid.",
                    self.channel_id,
                )
                await client.close()
                return

            self._channel = channel
            LOG.info("Target channel: #%s (id=%s)", channel.name, channel.id)

            # Start the Unix socket server now that we have a valid channel.
            await self._start_socket_server()

        @client.event
        async def on_disconnect() -> None:
            LOG.warning(
                "Disconnected from Discord – discord.py will attempt to reconnect"
            )

        @client.event
        async def on_resumed() -> None:
            LOG.info("Resumed Discord session")

        # Register signal handlers so the bot exits cleanly.
        loop = asyncio.new_event_loop()

        def _handle_signal() -> None:
            LOG.info("Received termination signal – shutting down")
            loop.create_task(self._shutdown())

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                # Windows does not support add_signal_handler; fall through
                pass

        try:
            loop.run_until_complete(client.start(self.token))
        except KeyboardInterrupt:
            LOG.info("KeyboardInterrupt – shutting down")
            loop.run_until_complete(self._shutdown())
        finally:
            self._cleanup_socket()
            loop.close()

    # ------------------------------------------------------------------
    # Socket server
    # ------------------------------------------------------------------

    async def _start_socket_server(self) -> None:
        """Create an asyncio Unix socket server."""
        self._cleanup_socket()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        LOG.info("Listening on Unix socket: %s", self.socket_path)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection on the Unix socket.

        Each line is expected to be a JSON object.  We send the ``content``
        field as a Discord message and respond with ``{"ok": true}`` or
        ``{"ok": false, "error": "..."}``.
        """
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # EOF – client closed connection

                if len(line) > MAX_MESSAGE_SIZE:
                    response = {"ok": False, "error": "message too large"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    response = {"ok": False, "error": f"invalid JSON: {exc}"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                # Extract the message content.  Accept either the webhook-style
                # ``{"content": "..."}`` format or a ``{"body": "...", "title":
                # "..."}`` format from the notification API.
                content = data.get("content")
                if content is None:
                    title = data.get("title", "")
                    body = data.get("body", "")
                    if title and body:
                        content = f"# {title}\n\n{body}"
                    elif title:
                        content = f"# {title}"
                    elif body:
                        content = body

                if not content:
                    response = {
                        "ok": False,
                        "error": "empty message: no 'content' or 'body' field",
                    }
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                # Discord messages are limited to 2000 characters.
                if len(content) > 2000:
                    content = content[:1997] + "..."

                ok = await self._send_to_discord(content)
                response = {"ok": ok}
                if not ok:
                    response["error"] = "failed to send to Discord"
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("Error handling socket client")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_to_discord(self, content: str) -> bool:
        """Send a text message to the configured Discord channel."""
        if self._channel is None:
            LOG.error("Cannot send message: channel not resolved")
            return False
        try:
            await self._channel.send(content)
            LOG.debug(
                "Sent message to #%s (%d chars)",
                getattr(self._channel, "name", "?"),
                len(content),
            )
            return True
        except Exception:
            LOG.exception("Failed to send message to Discord")
            return False

    # ------------------------------------------------------------------
    # Shutdown helpers
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Gracefully stop the socket server and disconnect from Discord."""
        LOG.info("Shutting down...")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._client is not None:
            await self._client.close()

    def _cleanup_socket(self) -> None:
        """Remove the Unix socket file if it exists."""
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            LOG.warning("Could not remove socket file: %s", self.socket_path)


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def main() -> None:
    """CLI entry point for ``python -m ampa.discord_bot``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = os.getenv("AMPA_DISCORD_BOT_TOKEN")
    if not token:
        LOG.error(
            "AMPA_DISCORD_BOT_TOKEN is not set.  "
            "Create a bot in the Discord Developer Portal and set this env var "
            "to the bot token."
        )
        sys.exit(1)

    channel_id_raw = os.getenv("AMPA_DISCORD_CHANNEL_ID")
    if not channel_id_raw:
        LOG.error(
            "AMPA_DISCORD_CHANNEL_ID is not set.  "
            "Set this to the integer ID of the Discord channel where "
            "notifications should be sent."
        )
        sys.exit(1)

    try:
        channel_id = int(channel_id_raw)
    except ValueError:
        LOG.error(
            "AMPA_DISCORD_CHANNEL_ID=%r is not a valid integer.  "
            "Channel IDs are numeric (right-click channel > Copy ID in Discord).",
            channel_id_raw,
        )
        sys.exit(1)

    socket_path = os.getenv("AMPA_BOT_SOCKET_PATH", DEFAULT_SOCKET_PATH)

    LOG.info(
        "Starting AMPA Discord bot – channel_id=%s socket=%s",
        channel_id,
        socket_path,
    )
    bot = AMPABot(token=token, channel_id=channel_id, socket_path=socket_path)
    bot.run()


if __name__ == "__main__":
    main()
