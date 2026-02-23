"""Public notification API for AMPA.

This module provides a single entry point — :func:`notify` — that all AMPA
modules should call instead of ``webhook_module.send_webhook()``.  Internally
it routes messages to the Discord bot via a Unix domain socket.

If the socket is unreachable the message is dead-lettered to a local file so
nothing is silently lost.

State tracking
--------------
Each successful send records ``last_message_ts`` and ``last_message_type`` in a
state file (same path as the legacy webhook state).  The daemon's heartbeat
suppression logic reads this state to avoid sending redundant heartbeats when
a non-heartbeat message was already sent recently.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import tempfile
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("ampa.notifications")

# Default Unix socket path — must match the bot's default.
DEFAULT_SOCKET_PATH = "/tmp/ampa_bot.sock"

# Socket connect + send timeout in seconds.
SOCKET_TIMEOUT = 10


# ---------------------------------------------------------------------------
# State helpers (ported from webhook.py so the state-file contract is
# preserved across the migration).
# ---------------------------------------------------------------------------


def _read_state(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_state(path: str, data: Dict[str, str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        LOG.exception("Failed to write state file %s", path)


def _state_file_path() -> str:
    return os.getenv("AMPA_STATE_FILE") or os.path.join(
        tempfile.gettempdir(), "ampa_state.json"
    )


# ---------------------------------------------------------------------------
# Dead-letter (ported from webhook.py)
# ---------------------------------------------------------------------------


def dead_letter(payload: Dict[str, Any], reason: Optional[str] = None) -> None:
    """Persist a failed notification so it is not silently lost.

    Writes to ``AMPA_DEADLETTER_FILE`` (default ``/var/log/ampa_deadletter.log``).
    """
    try:
        try:
            payload_str = json.dumps(payload)
        except Exception:
            payload_str = str(payload)
        LOG.error(
            "dead_letter invoked: reason=%s payload=%s",
            reason,
            payload_str[:1000],
        )
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "reason": reason,
            "payload": payload,
        }
        dl_file = os.getenv("AMPA_DEADLETTER_FILE", "/var/log/ampa_deadletter.log")
        try:
            parent = os.path.dirname(dl_file)
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except Exception:
                    pass
            with open(dl_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            LOG.info("dead_letter: appended failure record to %s", dl_file)
        except Exception:
            LOG.exception("dead_letter: failed to write dead-letter file %s", dl_file)
    except Exception:
        LOG.exception("dead_letter: unexpected error while handling dead letter")


# ---------------------------------------------------------------------------
# Payload builders (ported from webhook.py for backward compatibility during
# migration — callers can continue to use these to build the message content
# and then pass the result to notify()).
# ---------------------------------------------------------------------------


def _truncate_output(output: str, limit: int = 900) -> str:
    if len(output) <= limit:
        return output
    return output[:limit] + "\n... (truncated)"


def build_payload(
    hostname: str,
    timestamp_iso: str,
    work_item_id: Optional[str] = None,
    extra_fields: Optional[List[Dict[str, Any]]] = None,
    title: str = "AMPA Heartbeat",
) -> Dict[str, Any]:
    """Build a simple markdown payload (same output as ``webhook.build_payload``).

    Returns ``{"content": "<markdown>"}`` — compatible with both the legacy
    webhook format and the new bot socket protocol.
    """
    heading = f"# {title}"
    body: List[str] = []
    if extra_fields:
        for field in extra_fields:
            name = field.get("name")
            value = field.get("value")
            if name and value is not None:
                body.append(f"{name}: {value}")
    if body:
        content = heading + "\n\n" + "\n".join(body)
    else:
        content = heading
    return {"content": content}


def build_command_payload(
    hostname: str,
    timestamp_iso: str,
    command_id: Optional[str],
    output: Optional[str],
    exit_code: Optional[int],
    title: str = "AMPA Heartbeat",
) -> Dict[str, Any]:
    """Build a command-oriented payload (same as ``webhook.build_command_payload``).

    Returns ``{"content": "<markdown>"}``.
    """
    heading = f"# {title}" if title else "# AMPA Notification"
    body: List[str] = []
    if output:
        body.append(_truncate_output(output, limit=1000))
    if body:
        content = heading + "\n\n" + "\n".join(body)
    else:
        content = heading
    return {"content": content}


# ---------------------------------------------------------------------------
# Core notification function
# ---------------------------------------------------------------------------


def notify(
    title: str,
    body: str = "",
    message_type: str = "other",
    *,
    payload: Optional[Dict[str, Any]] = None,
) -> bool:
    """Send a notification to Discord via the bot's Unix socket.

    Parameters
    ----------
    title:
        The heading / title for the notification.
    body:
        The body text (markdown).
    message_type:
        A label for the kind of notification (``heartbeat``, ``command``,
        ``startup``, ``error``, ``completion``, ``warning``,
        ``waiting_for_input``, ``engine``, ``other``).  Used for state
        tracking and heartbeat suppression — not sent to Discord directly.
    payload:
        Optional pre-built payload dict.  If provided, this is sent directly
        to the bot socket (must contain ``content`` or ``title``/``body``).
        When *payload* is supplied, *title* and *body* are ignored.

    Returns
    -------
    bool
        ``True`` if the message was accepted by the bot; ``False`` if the
        message was dead-lettered.
    """
    socket_path = os.getenv("AMPA_BOT_SOCKET_PATH", DEFAULT_SOCKET_PATH)

    # Build the payload to send over the socket.
    if payload is not None:
        msg = dict(payload)
    else:
        msg = {}
        if title and body:
            msg["content"] = f"# {title}\n\n{body}"
        elif title:
            msg["content"] = f"# {title}"
        elif body:
            msg["content"] = body
        else:
            LOG.warning("notify() called with empty title and body – skipping")
            return False
    msg["message_type"] = message_type

    # Try to send via Unix socket.
    ok = _send_via_socket(socket_path, msg)

    # Update state file regardless of success/failure (matches legacy behavior
    # where state was updated even on failed attempts).
    state_file = _state_file_path()
    try:
        _write_state(
            state_file,
            {
                "last_message_ts": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
                "last_message_type": message_type,
            },
        )
    except Exception:
        LOG.debug("Failed to update state after notify()")

    if not ok:
        dead_letter(msg, reason="Unix socket unreachable")
        return False

    return True


def _send_via_socket(socket_path: str, msg: Dict[str, Any]) -> bool:
    """Send a single JSON message to the bot via Unix socket.

    Returns ``True`` if the bot acknowledged the message, ``False`` otherwise.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect(socket_path)

        line = json.dumps(msg) + "\n"
        sock.sendall(line.encode("utf-8"))

        # Read the response line.
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

        if not data:
            LOG.warning("Bot socket returned empty response")
            return False

        resp = json.loads(data.strip())
        if resp.get("ok"):
            LOG.debug("Notification sent successfully via bot socket")
            return True
        else:
            LOG.warning("Bot socket returned error: %s", resp.get("error", "unknown"))
            return False

    except FileNotFoundError:
        LOG.warning("Bot socket not found at %s – bot may not be running", socket_path)
        return False
    except ConnectionRefusedError:
        LOG.warning(
            "Bot socket connection refused at %s – bot may not be running",
            socket_path,
        )
        return False
    except OSError as exc:
        LOG.warning("Bot socket error: %s", exc)
        return False
    except Exception:
        LOG.exception("Unexpected error sending notification via bot socket")
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
