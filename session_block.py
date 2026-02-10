"""Utilities to detect and record blocking prompts for interactive sessions.

This module provides a narrowly-scoped implementation used by the
SA-0MLGALPM812GOPDC work-item: it marks a session as `waiting_for_input`,
records a prompt summary and metadata to a tool-output directory, and emits a
simple internal event (written to an events log) so other processes can react.

The implementation is intentionally small and dependency-free so it can be
integrated into existing code paths quickly. The location for persisted
artifacts is taken from the environment variable `AMPA_TOOL_OUTPUT_DIR`; if
unset a directory under the platform temporary directory is used.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Dict, Any, Optional

LOG = logging.getLogger("session_block")


def _tool_output_dir() -> str:
    path = os.getenv("AMPA_TOOL_OUTPUT_DIR")
    if path:
        return path
    default = os.path.join(tempfile.gettempdir(), "opencode_tool_output")
    return default


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        LOG.exception("Failed to create tool-output dir=%s", path)


def _excerpt_text(text: Optional[str], limit: int = 500) -> str:
    if not text:
        return ""
    one = " ".join(str(text).split())
    if len(one) <= limit:
        return one
    return one[:limit].rstrip() + "..."


def emit_internal_event(event_type: str, payload: Dict[str, Any]) -> str:
    """Emit a simple internal event by appending a JSON line to events.log.

    Returns the path to the events log file.
    """
    out_dir = _tool_output_dir()
    _ensure_dir(out_dir)
    events_path = os.path.join(out_dir, "events.jsonl")
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "event": event_type,
        "payload": payload,
    }
    try:
        with open(events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")
        LOG.info("Emitted internal event=%s to %s", event_type, events_path)
    except Exception:
        LOG.exception("Failed to write internal event to %s", events_path)
    return events_path


def set_session_state(session_id: str, state: str) -> str:
    """Record the session state to a small JSON file.

    Returns the path to the state file.
    """
    out_dir = _tool_output_dir()
    _ensure_dir(out_dir)
    state_path = os.path.join(out_dir, f"session_{session_id}.json")
    payload = {
        "session": session_id,
        "state": state,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        LOG.info(
            "Wrote session state for session=%s state=%s path=%s",
            session_id,
            state,
            state_path,
        )
    except Exception:
        LOG.exception("Failed to write session state to %s", state_path)
    return state_path


def detect_and_surface_blocking_prompt(
    session_id: str,
    work_item_id: Optional[str],
    prompt_text: str,
) -> Dict[str, Any]:
    """Record that a prompt is blocking and surface minimal metadata.

    Behaviour:
    - set session state to `waiting_for_input` (writes a session JSON file)
    - write a pending prompt file under the tool-output dir with a short
      summary and metadata (session id, work-item id, timestamp)
    - emit an internal event `waiting_for_input` with the same metadata

    Returns the metadata dictionary written.
    """
    ts = datetime.utcnow().isoformat() + "Z"
    summary = _excerpt_text(prompt_text, limit=500)
    metadata = {
        "session": session_id,
        "work_item": work_item_id,
        "summary": summary,
        "state": "waiting_for_input",
        "created_at": ts,
    }

    out_dir = _tool_output_dir()
    _ensure_dir(out_dir)

    # filename uses timestamp to avoid races
    stamp = str(int(time.time() * 1000))
    filename = f"pending_prompt_{session_id}_{stamp}.json"
    path = os.path.join(out_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2, sort_keys=True)
        LOG.info(
            "Wrote pending prompt for session=%s work_item=%s path=%s",
            session_id,
            work_item_id,
            path,
        )
    except Exception:
        LOG.exception("Failed to write pending prompt to %s", path)

    # set session state and emit event
    set_session_state(session_id, "waiting_for_input")
    emit_internal_event("waiting_for_input", metadata)

    return metadata
