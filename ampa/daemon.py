"""AMPA package entry points and core heartbeat sender.

This module contains the same functionality as the top-level script but is
packaged under the `ampa` Python package so it can be imported in tests and
installed if needed.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
from typing import Any, Dict, Optional, List
import tempfile
import urllib.parse

try:
    # optional dependency for .env file parsing
    from dotenv import load_dotenv, find_dotenv
except Exception:  # pragma: no cover - optional behavior
    load_dotenv = None
    find_dotenv = None

# import requests at module level so tests can monkeypatch ampa.daemon.requests.post
try:
    import requests  # type: ignore
except Exception:
    requests = None

LOG = logging.getLogger("ampa.daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

__all__ = [
    "build_command_payload",
    "build_payload",
    "get_env_config",
    "run_once",
    "send_webhook",
    "dead_letter",
]


def get_env_config() -> Dict[str, Any]:
    """Read and validate environment configuration.

    Raises SystemExit (2) if AMPA_DISCORD_WEBHOOK is not set.
    """
    # If an .env file exists in the package directory, load it so values there
    # override the environment. Loading is optional; if python-dotenv is not
    # installed we skip loading the file.
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    # Allow callers/tests to disable loading the package .env file by setting
    # AMPA_LOAD_DOTENV=0. By default the package .env is loaded when python-dotenv
    # is available and a package-local .env exists. Note: .env values override
    # the environment per user request.
    if (
        os.getenv("AMPA_LOAD_DOTENV", "1").lower() in ("1", "true", "yes")
        and load_dotenv
        and find_dotenv
    ):
        # prefer package-local .env when present
        pkg_env = find_dotenv(env_path, usecwd=True)
        if pkg_env:
            load_dotenv(pkg_env, override=True)
        else:
            # Fallback to a repo root .env (e.g. /opt/ampa/.env) when present.
            root_env = os.path.join(os.getcwd(), ".env")
            if os.path.isfile(root_env):
                load_dotenv(root_env, override=True)

    # Read webhook and be tolerant of values coming from Docker `--env-file`
    # which preserve surrounding quotes. Strip whitespace and surrounding
    # single/double quotes so both dotenv and Docker env-file formats work.
    webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
    if webhook:
        webhook = webhook.strip()
        # remove surrounding single/double quotes if present (handles values
        # coming from dotenv or Docker env-file which may include quotes)
        webhook = webhook.strip("'\"")
    if not webhook:
        LOG.error("AMPA_DISCORD_WEBHOOK is not set; cannot send heartbeats")
        raise SystemExit(2)

    minutes_raw = os.getenv("AMPA_HEARTBEAT_MINUTES", "1")
    try:
        minutes = int(minutes_raw)
        if minutes <= 0:
            raise ValueError("must be positive")
    except Exception:
        LOG.warning("Invalid AMPA_HEARTBEAT_MINUTES=%r, falling back to 1", minutes_raw)
        minutes = 1

    return {"webhook": webhook, "minutes": minutes}


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
    """Build a Discord webhook payload (plain text) for a heartbeat.

    The text includes hostname, ISO timestamp and the optional work item id.
    """
    lines = [title, f"Host: {hostname}", f"Timestamp: {timestamp_iso}"]
    if work_item_id:
        lines.append(f"work_item_id: {work_item_id}")
    if extra_fields:
        for field in extra_fields:
            name = field.get("name")
            value = field.get("value")
            if name and value is not None:
                lines.append(f"{name}: {value}")
    return {"content": "\n".join(lines)}


def build_command_payload(
    hostname: str,
    timestamp_iso: str,
    command_id: Optional[str],
    output: Optional[str],
    exit_code: Optional[int],
    title: str = "AMPA Heartbeat",
) -> Dict[str, Any]:
    fields: List[Dict[str, Any]] = []
    if command_id:
        fields.append({"name": "command_id", "value": command_id, "inline": False})
    if exit_code is not None:
        fields.append({"name": "exit_code", "value": str(exit_code), "inline": True})
    if output:
        formatted = "```\n" + _truncate_output(output) + "\n```"
        fields.append({"name": "output", "value": formatted, "inline": False})
    return build_payload(
        hostname,
        timestamp_iso,
        extra_fields=fields,
        title=title,
    )


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


def dead_letter(payload: Dict[str, Any], reason: Optional[str] = None) -> None:
    """Handle final-failure messages by forwarding to a dead-letter webhook or file.

    Behavior:
    - If AMPA_DEADLETTER_WEBHOOK is set, POST the payload (with optional reason) to that URL.
    - Otherwise append a JSON record to AMPA_DEADLETTER_FILE (default: /var/log/ampa_deadletter.log).
    This function is best-effort and will log but not raise on failure.
    """
    try:
        dd_wh = os.getenv("AMPA_DEADLETTER_WEBHOOK")
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "reason": reason,
            "payload": payload,
        }
        if dd_wh:
            if requests is None:
                LOG.error(
                    "dead_letter: requests missing; cannot POST to deadletter webhook"
                )
            else:
                try:
                    sess = requests.Session()
                    sess.trust_env = False
                    resp = sess.post(dd_wh, json=record, timeout=10)
                    try:
                        resp.raise_for_status()
                        LOG.info(
                            "dead_letter: posted to deadletter webhook status=%s",
                            resp.status_code,
                        )
                        return
                    except Exception:
                        LOG.error(
                            "dead_letter: deadletter webhook POST failed: %s %s",
                            getattr(resp, "status_code", None),
                            getattr(resp, "text", ""),
                        )
                except Exception as exc:
                    LOG.exception(
                        "dead_letter: exception posting to deadletter webhook: %s", exc
                    )
        # fallback to local file
        dl_file = os.getenv("AMPA_DEADLETTER_FILE", "/var/log/ampa_deadletter.log")
        try:
            # Ensure parent dir exists when writing to a path we control (may not for /var/log)
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


def send_webhook(
    url: str, payload: Dict[str, Any], timeout: int = 10, message_type: str = "other"
) -> int:
    """Send the webhook payload using requests.

    Returns HTTP status code on success. Raises RuntimeError if requests
    is not available.
    """
    if requests is None:
        LOG.error("requests package is required to send webhook")
        raise RuntimeError("requests missing")

    state_file = os.getenv("AMPA_STATE_FILE") or os.path.join(
        tempfile.gettempdir(), "ampa_state.json"
    )

    def _mask_url(u: str) -> str:
        try:
            # Mask the webhook token portion so logs aren't leaking it
            parts = u.split("/")
            if len(parts) > 0:
                # last two segments are id/token; mask token
                parts[-1] = "<token>"
                return "/".join(parts)
        except Exception:
            pass
        return "<redacted>"

    try:
        session = requests.Session()
        session.trust_env = False
        LOG.debug(
            "Sending webhook to %s (masked=%s); trust_env=%s",
            url,
            _mask_url(url),
            session.trust_env,
        )
        resp = session.post(url, json=payload, timeout=timeout)
        try:
            resp.raise_for_status()
        except Exception:
            # Log a richer set of debug information to help diagnose
            # environments where the webhook appears valid on the host but
            # fails from inside the running container.
            LOG.error(
                "Webhook POST failed: %s %s",
                resp.status_code,
                getattr(resp, "text", ""),
            )
            try:
                # Show the exact URL and headers used for the request (mask token)
                req = getattr(resp, "request", None)
                if req is not None:
                    masked = _mask_url(getattr(req, "url", None) or "")
                    headers = {
                        k: v
                        for k, v in getattr(req, "headers", {}).items()
                        if k.lower() not in ("authorization",)
                    }
                    body_len = (
                        len(getattr(req, "body", b""))
                        if getattr(req, "body", None)
                        else 0
                    )
                    LOG.info(
                        "Request made to %s; headers=%s; body_len=%s",
                        masked,
                        headers,
                        body_len,
                    )
                    # Attempt to resolve the webhook host from inside the process
                    parsed = urllib.parse.urlparse(getattr(req, "url", ""))
                    host = parsed.hostname
                    if host:
                        try:
                            addrs = [
                                a[4][0]
                                for a in __import__("socket").getaddrinfo(
                                    host, parsed.port or 443
                                )
                            ]
                            LOG.info("Resolved %s -> %s", host, addrs)
                        except Exception as _e:
                            LOG.info("Failed to resolve host %s: %s", host, _e)
            except Exception:
                LOG.info("Failed to log request debug info")
            # record attempted send
            _write_state(
                state_file,
                {
                    "last_message_ts": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "last_message_type": message_type,
                },
            )
            return resp.status_code
        LOG.info("Webhook POST succeeded: %s", resp.status_code)
        _write_state(
            state_file,
            {
                "last_message_ts": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
                "last_message_type": message_type,
            },
        )
        return resp.status_code
    except Exception as exc:
        LOG.error("Webhook POST exception: %s", exc)
        # record attempted send even on exception
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
            LOG.debug("Failed to record state after exception")
        raise


def run_once(config: Dict[str, Any]) -> int:
    """Send a single heartbeat using the provided config.

    Returns the HTTP status code (or raises if requests is missing).
    """
    hostname = socket.gethostname()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = build_payload(hostname, ts, None)
    LOG.info("Evaluating whether to send heartbeat for host=%s", hostname)

    state_file = os.getenv("AMPA_STATE_FILE") or os.path.join(
        tempfile.gettempdir(), "ampa_state.json"
    )
    state = _read_state(state_file)
    last_message_ts = None
    last_message_type = None
    last_heartbeat_ts = None
    try:
        if "last_message_ts" in state:
            last_message_ts = datetime.datetime.fromisoformat(state["last_message_ts"])
    except Exception:
        last_message_ts = None
    try:
        if "last_message_type" in state:
            last_message_type = state["last_message_type"]
    except Exception:
        last_message_type = None
    try:
        if "last_heartbeat_ts" in state:
            last_heartbeat_ts = datetime.datetime.fromisoformat(
                state["last_heartbeat_ts"]
            )
    except Exception:
        last_heartbeat_ts = None

    now = datetime.datetime.now(datetime.timezone.utc)

    # Only send the heartbeat if no non-heartbeat message was sent in the last 5 minutes.
    if last_message_ts is not None and last_message_type != "heartbeat":
        if (now - last_message_ts) < datetime.timedelta(minutes=5):
            LOG.info(
                "Skipping heartbeat: other message sent within last 5 minutes (last_message=%s)",
                state.get("last_message_ts"),
            )
            return 0

    # Send heartbeat and update heartbeat timestamp
    status = send_webhook(config["webhook"], payload, message_type="heartbeat")
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _write_state(
            state_file,
            {
                "last_heartbeat_ts": now_iso,
                "last_message_ts": now_iso,
                "last_message_type": "heartbeat",
            },
        )
    except Exception:
        LOG.exception("Failed to update state after heartbeat")
    return status


def main() -> None:
    config = get_env_config()
    LOG.info("Sending AMPA heartbeat once")
    try:
        run_once(config)
    except SystemExit:
        raise
    except Exception:
        LOG.exception("Error while sending heartbeat")


if __name__ == "__main__":
    main()
