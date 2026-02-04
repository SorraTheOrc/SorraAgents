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
import time
from typing import Any, Dict, Optional

try:
    # optional dependency for .env file parsing
    from dotenv import load_dotenv, find_dotenv
except Exception:  # pragma: no cover - optional behavior
    load_dotenv = None
    find_dotenv = None

LOG = logging.getLogger("ampa.daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def get_env_config() -> Dict[str, Any]:
    """Read and validate environment configuration.

    Raises SystemExit (2) if AMPA_DISCORD_WEBHOOK is not set.
    """
    # If an .env file exists in the ampa package, load it so values there
    # override the environment. Loading is optional; if python-dotenv is not
    # installed we skip loading the file.
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if load_dotenv and find_dotenv:
        # prefer package-local .env when present
        pkg_env = find_dotenv(env_path, usecwd=True)
        if pkg_env:
            load_dotenv(pkg_env, override=True)

    webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
    if not webhook:
        LOG.error("AMPA_DISCORD_WEBHOOK is not set; cannot send heartbeats")
        raise SystemExit(2)

    # Default heartbeat interval is 60 minutes (long-running daemon)
    minutes_raw = os.getenv("AMPA_HEARTBEAT_MINUTES", "60")
    try:
        minutes = int(minutes_raw)
        if minutes <= 0:
            raise ValueError("must be positive")
    except Exception:
        LOG.warning(
            "Invalid AMPA_HEARTBEAT_MINUTES=%r, falling back to 60", minutes_raw
        )
        minutes = 60

    work_item = os.getenv("AMPA_WORKITEM_ID")

    return {"webhook": webhook, "minutes": minutes, "work_item": work_item}


def build_payload(
    hostname: str, timestamp_iso: str, work_item_id: Optional[str] = None
) -> Dict[str, Any]:
    """Build a Discord webhook payload (embed format) for a heartbeat.

    The embed includes hostname, ISO timestamp and the optional work item id.
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
    """Send the webhook payload using requests.

    Returns HTTP status code on success. Raises RuntimeError if requests
    is not available.
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

    Returns the HTTP status code (or raises if requests is missing).
    """
    hostname = socket.gethostname()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = build_payload(hostname, ts, config.get("work_item"))
    LOG.info(
        "Sending heartbeat for host=%s work_item=%s", hostname, config.get("work_item")
    )
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
