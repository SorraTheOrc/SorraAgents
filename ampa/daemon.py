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

LOG = logging.getLogger("ampa.daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

__all__ = ["get_env_config", "run_once"]

# Use webhook helpers from ampa.webhook as the single source of truth.
from .webhook import (
    build_command_payload,
    build_payload,
    send_webhook,
    dead_letter,
    _read_state,
    _write_state,
)


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
    """Daemon entrypoint.

    Supports:
    - `--once`: send one heartbeat and exit
    - `--start-scheduler`: start the scheduler loop under the daemon runtime
    If neither flag is provided the default behaviour is to send a single heartbeat.
    """
    import argparse

    parser = argparse.ArgumentParser(description="AMPA daemon")
    parser.add_argument(
        "--once", action="store_true", help="Send one heartbeat and exit"
    )
    parser.add_argument(
        "--start-scheduler",
        action="store_true",
        help="Start the scheduler loop under the daemon runtime",
    )
    args = parser.parse_args()

    try:
        config = get_env_config()
    except SystemExit:
        # get_env_config logs and exits when misconfigured
        raise

    # If requested, start scheduler as a long-running worker managed by daemon
    if args.start_scheduler or os.getenv("AMPA_RUN_SCHEDULER", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        try:
            # Import locally to avoid side-effects during test imports
            from . import scheduler

            LOG.info("Starting scheduler under daemon runtime")
            sched = scheduler.load_scheduler(command_cwd=os.getcwd())
            sched.run_forever()
            return
        except SystemExit:
            raise
        except Exception:
            LOG.exception("Failed to start scheduler from daemon")
            return

    # Default: send a single heartbeat
    LOG.info("Sending AMPA heartbeat once")
    try:
        run_once(config)
    except SystemExit:
        raise
    except Exception:
        LOG.exception("Error while sending heartbeat")


if __name__ == "__main__":
    main()
