"""Pi-side signal consumer for Ralph event notifications.

This module implements periodic polling of Ralph's signal file, event
deduplication, and automatic relaying of ``ralph status`` output to the
operator when a significant event occurs.

## Overview

When Ralph detects a significant event (loop start/complete, phase
transition, error, cancellation, max attempts, status change), it writes a
JSON signal file (default: ``.ralph/event.pending``). This module:

1. Reads the signal file path from Ralph's runtime context
   (``.worklog/ralph/current.json`` → field ``signal_file_path``).
2. Periodically polls the signal file for new events.
3. Deduplicates by comparing ``event_type`` + ``timestamp``.
4. When a new event is detected, runs ``ralph status`` and relays the output.
5. Clears the signal file after processing.

## Usage

Run as a standalone script to start continuous polling:

.. code-block:: bash

   python3 skill/ralph/scripts/signal_consumer.py

Or invoke the ``consume_once`` function from code for a single poll cycle:

.. code-block:: python

   from skill.ralph.scripts.signal_consumer import consume_once
   consume_once()

## Configuration

Environment variables:

- ``RALPH_POLL_INTERVAL``: Polling interval in seconds (default: 30).
- ``RALPH_RUNTIME_DIR``: Path to Ralph's runtime directory
  (default: ``.worklog/ralph/`` relative to CWD).
- ``RALPH_DEDUP_STORE``: Path to the deduplication store file
  (default: ``.worklog/ralph/.last_signal_consumed.json``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal as sig_module
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("signal_consumer")

# ── Defaults ────────────────────────────────────────────────────────────────

_DEFAULT_POLL_INTERVAL = 30  # seconds
_DEFAULT_RUNTIME_DIR = ".worklog/ralph"
_DEFAULT_DEDUP_STORE_NAME = ".last_signal_consumed.json"


def _get_env_int(name: str, default: int) -> int:
    """Read an integer environment variable with a fallback default."""
    value = os.environ.get(name)
    if value is not None:
        try:
            return int(value)
        except (ValueError, TypeError):
            pass
    return default


# ── Runtime context helpers ─────────────────────────────────────────────────


def resolve_runtime_dir(cwd: Path | None = None) -> Path:
    """Resolve the Ralph runtime directory.

    Uses the ``RALPH_RUNTIME_DIR`` environment variable if set, otherwise
    defaults to ``.worklog/ralph/`` relative to the current working directory
    (or the provided ``cwd``).

    Args:
        cwd: Optional override for the base directory.  Defaults to CWD.

    Returns:
        Path to the runtime directory.
    """
    env_dir = os.environ.get("RALPH_RUNTIME_DIR")
    if env_dir:
        return Path(env_dir)
    base = cwd if cwd is not None else Path.cwd()
    return base / _DEFAULT_RUNTIME_DIR


def resolve_signal_file_path(runtime_dir: Path) -> Path | None:
    """Resolve the signal file path from Ralph's runtime context.

    Reads ``signal_file_path`` from ``current.json`` in the runtime
    directory. Falls back to the default ``.ralph/event.pending`` relative
    to CWD when the context is missing or the field is empty.

    Args:
        runtime_dir: Path to Ralph's runtime directory.

    Returns:
        Path to the signal file, or ``None`` if it cannot be determined.
    """
    state_path = runtime_dir / "current.json"
    if not state_path.exists():
        logger.debug("No runtime context at %s", state_path)
        return None

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read runtime context %s: %s", state_path, exc)
        return None

    if not isinstance(payload, dict):
        logger.warning("Invalid runtime context payload in %s", state_path)
        return None

    signal_path_str = payload.get("signal_file_path")
    if signal_path_str and isinstance(signal_path_str, str) and signal_path_str.strip():
        return Path(signal_path_str.strip())

    # Fallback to the well-known default
    logger.debug("No signal_file_path in context; using default")
    return Path(".ralph") / "event.pending"


# ── Deduplication helpers ───────────────────────────────────────────────────


def _dedup_store_path(runtime_dir: Path) -> Path:
    """Resolve the deduplication store file path.

    Uses the ``RALPH_DEDUP_STORE`` environment variable if set, otherwise
    defaults to a hidden JSON file inside the runtime directory.
    """
    env_path = os.environ.get("RALPH_DEDUP_STORE")
    if env_path:
        return Path(env_path)
    return runtime_dir / _DEFAULT_DEDUP_STORE_NAME


def load_last_consumed(store_path: Path) -> dict[str, str]:
    """Load the last consumed event key from the dedup store.

    Returns a dict mapping ``event_type`` → ``timestamp`` for the most
    recently consumed event.  Returns an empty dict when the store does not
    exist or is invalid.
    """
    if not store_path.exists():
        return {}
    try:
        payload = json.loads(store_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_last_consumed(store_path: Path, event_type: str, timestamp: str) -> None:
    """Save the event key to the dedup store.

    Args:
        store_path: Path to the dedup store file.
        event_type: The event type string.
        timestamp: The ISO8601 timestamp string.
    """
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, str] = {
            "event_type": event_type,
            "timestamp": timestamp,
        }
        store_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Failed to write dedup store %s: %s", store_path, exc)


def is_new_event(last_consumed: dict[str, str], event_type: str, timestamp: str) -> bool:
    """Check whether the given event is different from the last consumed.

    Two events are considered duplicates when both ``event_type`` and
    ``timestamp`` match the last consumed entry.
    """
    if not last_consumed:
        return True
    return (
        last_consumed.get("event_type") != event_type
        or last_consumed.get("timestamp") != timestamp
    )


# ── Signal file reading ─────────────────────────────────────────────────────


def read_signal_file(signal_path: Path) -> dict | None:
    """Read and parse the signal file.

    Args:
        signal_path: Path to the signal file.

    Returns:
        Parsed JSON dict, or ``None`` when the file doesn't exist or is
        invalid JSON.
    """
    if not signal_path.exists():
        return None
    try:
        content = signal_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Invalid signal file %s: %s", signal_path, exc)
    return None


# ── Status invocation ───────────────────────────────────────────────────────


def invoke_ralph_status() -> tuple[int, str, str]:
    """Run ``ralph status`` and return the result.

    Returns:
        A tuple of ``(returncode, stdout, stderr)``.
    """
    script_path = Path(__file__).resolve().parents[1] / "ralph"
    if script_path.exists():
        cmd = [str(script_path), "status"]
    else:
        # Fallback: use the ralph_control script directly
        control_path = Path(__file__).resolve().parent / "ralph_control.py"
        if control_path.exists():
            cmd = [sys.executable, str(control_path), "status"]
        else:
            cmd = ["ralph", "status"]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        logger.warning("ralph status command not found")
        return 1, "", "ralph status command not found"
    except subprocess.TimeoutExpired:
        logger.warning("ralph status timed out after 30s")
        return 1, "", "ralph status timed out"
    except Exception as exc:
        logger.warning("ralph status failed: %s", exc)
        return 1, "", str(exc)


# ── Signal clearing ─────────────────────────────────────────────────────────


def clear_signal_file(signal_path: Path) -> None:
    """Clear the signal file to prevent re-triggering.

    Attempts to delete the file first.  If deletion fails (e.g. permission
    issues), falls back to overwriting with an empty sentinel ``{}``.
    """
    try:
        signal_path.unlink(missing_ok=True)
        return
    except OSError:
        pass
    # Fallback: overwrite with empty object
    try:
        signal_path.write_text("{}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to clear signal file %s: %s", signal_path, exc)


# ── Main consume function ──────────────────────────────────────────────────


def consume_once(
    runtime_dir: Path | None = None,
    signal_file_path: Path | None = None,
    dedup_store_path: Path | None = None,
) -> dict | None:
    """Consume a single signal event if one is present and new.

    This is the core polling function.  It reads the signal file, checks
    for deduplication, runs ``ralph status`` if a new event is found,
    saves the dedup key, and clears the signal file.

    Args:
        runtime_dir: Override for the runtime directory path.
        signal_file_path: Override for the signal file path.
        dedup_store_path: Override for the dedup store path.

    Returns:
        A dict with keys:

        - ``consumed`` (bool): Whether a signal was consumed.
        - ``event_type`` (str | None): The event type of the consumed signal.
        - ``ralph_output`` (str | None): The ``ralph status`` output, if
          a new signal was consumed.
        - ``error`` (str | None): Error message if something went wrong.
    """
    # Resolve paths
    if runtime_dir is None:
        runtime_dir = resolve_runtime_dir()
    if signal_file_path is None:
        signal_file_path = resolve_signal_file_path(runtime_dir)
    if dedup_store_path is None:
        dedup_store_path = _dedup_store_path(runtime_dir)

    result: dict[str, object] = {"consumed": False}

    # Check if signal file path is available
    if signal_file_path is None:
        result["error"] = "No signal file path resolved"
        return result

    # Check if signal file exists
    signal_data = read_signal_file(signal_file_path)
    if signal_data is None:
        return result

    # Parse event fields
    event_type = signal_data.get("event_type", "")
    timestamp = signal_data.get("timestamp", "")

    if not event_type or not timestamp:
        logger.warning("Signal file missing event_type or timestamp; clearing")
        clear_signal_file(signal_file_path)
        result["error"] = "Signal file missing required fields"
        return result

    # Deduplication check
    last_consumed = load_last_consumed(dedup_store_path)
    if not is_new_event(last_consumed, event_type, timestamp):
        result["skipped"] = "duplicate"
        return result

    # Invoke ralph status
    ralph_rc, ralph_stdout, ralph_stderr = invoke_ralph_status()
    if ralph_rc != 0:
        logger.warning("ralph status failed (rc=%d): %s", ralph_rc, ralph_stderr.strip())
        result["ralph_output"] = ralph_stdout or ralph_stderr
        result["ralph_error"] = True

    # Save dedup key
    save_last_consumed(dedup_store_path, event_type, timestamp)

    # Clear signal file
    clear_signal_file(signal_file_path)

    result["consumed"] = True
    result["event_type"] = event_type
    result["timestamp"] = timestamp
    result["ralph_output"] = ralph_stdout or None
    return result


# ── Continuous polling ─────────────────────────────────────────────────────


_SHUTDOWN = False


def _handle_signal(signum: int, frame: object) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _SHUTDOWN
    _SHUTDOWN = True


def run_polling_loop(
    poll_interval: int | None = None,
    runtime_dir: Path | None = None,
) -> None:
    """Run the signal consumer polling loop.

    Continuously polls for new signals at the configured interval.  Press
    Ctrl+C or send SIGTERM to stop the loop gracefully.

    Args:
        poll_interval: Polling interval in seconds (overrides env var).
        runtime_dir: Override for the runtime directory path.
    """
    if poll_interval is None:
        poll_interval = _get_env_int("RALPH_POLL_INTERVAL", _DEFAULT_POLL_INTERVAL)

    if runtime_dir is None:
        runtime_dir = resolve_runtime_dir()

    # Set up signal handlers for graceful shutdown
    old_term = sig_module.signal(sig_module.SIGTERM, _handle_signal)
    old_int = sig_module.signal(sig_module.SIGINT, _handle_signal)

    logger.info("Starting signal consumer polling loop (interval=%ds)", poll_interval)

    try:
        while not _SHUTDOWN:
            result = consume_once(runtime_dir=runtime_dir)
            if result:
                if result.get("consumed"):
                    logger.info(
                        "Signal consumed: event_type=%s ralph_rc=%s",
                        result.get("event_type"),
                        "ok" if not result.get("ralph_error") else "error",
                    )
                    output = result.get("ralph_output")
                    if output:
                        print(output, end="")
                        print()
                elif result.get("skipped") == "duplicate":
                    logger.debug("Duplicate signal event, skipping")
                elif result.get("error"):
                    logger.debug("No signal or error: %s", result["error"])
            # Sleep in small increments so we can respond to signals quickly
            for _ in range(poll_interval * 10):
                if _SHUTDOWN:
                    break
                time.sleep(0.1)
    finally:
        # Restore original signal handlers
        try:
            sig_module.signal(sig_module.SIGTERM, old_term)
        except (ValueError, OSError):
            pass
        try:
            sig_module.signal(sig_module.SIGINT, old_int)
        except (ValueError, OSError):
            pass
        logger.info("Signal consumer polling loop stopped")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the signal consumer script."""
    parser = argparse.ArgumentParser(
        description="Poll Ralph's signal file and relay status updates.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help=f"Polling interval in seconds (default: {_DEFAULT_POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--runtime-dir",
        type=str,
        default=None,
        help=f"Path to Ralph's runtime directory (default: {_DEFAULT_RUNTIME_DIR}).",
    )
    parser.add_argument(
        "--consume-once",
        action="store_true",
        help="Consume a single signal event and exit (don't poll continuously).",
    )
    parser.add_argument(
        "--signal-file",
        type=str,
        default=None,
        help="Override the signal file path.",
    )
    parser.add_argument(
        "--dedup-store",
        type=str,
        default=None,
        help="Override the dedup store path.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Main entry point for the signal consumer CLI.

    Args:
        argv: Optional list of command-line arguments.

    Returns:
        Exit code: 0 on success, 1 on error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Resolve runtime dir
    runtime_dir = None
    if args.runtime_dir:
        runtime_dir = Path(args.runtime_dir)
    else:
        env_dir = os.environ.get("RALPH_RUNTIME_DIR")
        if env_dir:
            runtime_dir = Path(env_dir)
        else:
            runtime_dir = resolve_runtime_dir()

    if args.consume_once:
        # Single consumption mode
        signal_path = Path(args.signal_file) if args.signal_file else None
        dedup_path = Path(args.dedup_store) if args.dedup_store else None
        result = consume_once(
            runtime_dir=runtime_dir,
            signal_file_path=signal_path,
            dedup_store_path=dedup_path,
        )
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("consumed"):
                return 0
            return 1
        return 0

    # Continuous polling mode
    run_polling_loop(
        poll_interval=args.poll_interval,
        runtime_dir=runtime_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
