"""Core Heartbeat Sender (minimal implementation).

Reads env vars and posts a heartbeat payload to a Discord webhook URL.

This module is intentionally small and structured so its formatting and
env-var handling can be unit-tested without performing network calls.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import sys
import time
from typing import Any, Dict, Optional

LOG = logging.getLogger("ampa_daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def get_env_config() -> Dict[str, Any]:
    """Read and validate environment configuration.

    Exits with non-zero status if AMPA_DISCORD_WEBHOOK is not set.
    """
    webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
    if not webhook:
        LOG.error("AMPA_DISCORD_WEBHOOK is not set; cannot send heartbeats")
        # Exit with non-zero status per acceptance criteria.
        raise SystemExit(2)

    minutes_raw = os.getenv("AMPA_HEARTBEAT_MINUTES", "1")
    try:
        minutes = int(minutes_raw)
        if minutes <= 0:
            raise ValueError("must be positive")
    except Exception:
        LOG.warning("Invalid AMPA_HEARTBEAT_MINUTES=%r, falling back to 1", minutes_raw)
        minutes = 1

    work_item = os.getenv("AMPA_WORKITEM_ID")

    return {"webhook": webhook, "minutes": minutes, "work_item": work_item}


def build_payload(
    hostname: str, timestamp_iso: str, work_item_id: Optional[str] = None
) -> Dict[str, Any]:
    """Build a Discord webhook payload (embed format) for a heartbeat.

    The exact embed structure is minimal but includes hostname, ISO timestamp
    and the optional work item id.
    """
    embed = {
        "title": "AMPA Heartbeat",
        "description": f"Host: {hostname}\nTimestamp: {timestamp_iso}",
        "color": 5814783,
        "fields": [],
    }
    if work_item_id:
        embed["fields"].append(
            {"name": "work_item_id", "value": work_item_id, "inline": False}
        )

    payload = {"embeds": [embed]}
    return payload


def send_webhook(url: str, payload: Dict[str, Any], timeout: int = 10) -> int:
    """Send the webhook payload using requests if available.

    Returns HTTP status code on success.
    Raises RuntimeError if the requests package is not installed or network call fails.
    """
    try:
        import requests
    except Exception as exc:  # pragma: no cover - environment dependent
        LOG.error("requests package is required to send webhook: %s", exc)
        raise RuntimeError("requests missing") from exc

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        try:
            resp.raise_for_status()
        except Exception:
            LOG.error(
                "Webhook POST failed: %s %s",
                resp.status_code,
                getattr(resp, "text", ""),
            )
            return resp.status_code
        LOG.info("Webhook POST succeeded: %s", resp.status_code)
        return resp.status_code
    except Exception as exc:
        LOG.error("Webhook POST exception: %s", exc)
        raise


def run_once(config: Dict[str, Any]) -> int:
    """Send a single heartbeat using the provided config.

    Returns the HTTP status code (0 indicates that no HTTP call was made).
    """
    hostname = socket.gethostname()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = build_payload(hostname, ts, config.get("work_item"))
    LOG.info(
        "Sending heartbeat for host=%s work_item=%s", hostname, config.get("work_item")
    )
    # Attempt to send; if requests is missing, surface the error to caller.
    return send_webhook(config["webhook"], payload)


def main() -> None:
    config = get_env_config()
    interval = config["minutes"] * 60
    LOG.info("Starting AMPA heartbeat sender; interval=%s seconds", interval)
    try:
        while True:
            try:
                run_once(config)
            except SystemExit:
                raise
            except Exception:
                LOG.exception("Error while sending heartbeat")
            time.sleep(interval)
    except KeyboardInterrupt:
        LOG.info("AMPA heartbeat sender stopped by user")


if __name__ == "__main__":
    main()
