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

try:
    from ampa import webhook as webhook_module
except Exception:  # pragma: no cover - optional dependency
    webhook_module = None

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


def _waiting_actions_text() -> str:
    return os.getenv(
        "AMPA_WAITING_FOR_INPUT_ACTIONS",
        "Respond via the responder endpoint, or auto-accept/auto-decline.",
    )


def _responder_endpoint_url() -> str:
    return os.getenv("AMPA_RESPONDER_URL", "http://localhost:8081/respond")


def _send_waiting_for_input_notification(metadata: Dict[str, Any]) -> Optional[int]:
    webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
    if not webhook:
        return None
    if webhook_module is None:
        LOG.warning("ampa.webhook is unavailable; cannot send notification")
        return None
    try:
        hostname = os.uname().nodename
    except Exception:
        hostname = "(unknown host)"
    ts = datetime.utcnow().isoformat() + "Z"
    actions = _waiting_actions_text()
    summary = metadata.get("summary") or "(no summary)"
    work_item = metadata.get("work_item") or "(none)"
    session_id = metadata.get("session") or "(unknown)"
    prompt_file = metadata.get("prompt_file") or "(unknown)"
    pending_prompt_file = metadata.get("pending_prompt_file") or prompt_file
    tool_dir = metadata.get("tool_output_dir") or _tool_output_dir()
    responder_url = _responder_endpoint_url()
    output = (
        "Session is waiting for input\n"
        f"Session: {session_id}\n"
        f"Work item: {work_item}\n"
        f"Reason: {summary}\n"
        f"Actions: {actions}\n"
        f"Responder endpoint: {responder_url}\n"
        f"Pending prompt file: {pending_prompt_file}\n"
        f"Tool output dir: {tool_dir}"
    )
    payload = webhook_module.build_command_payload(
        hostname,
        ts,
        "waiting_for_input",
        output,
        0,
        title="Session Waiting For Input",
    )
    try:
        return webhook_module.send_webhook(
            webhook, payload, message_type="waiting_for_input"
        )
    except Exception:
        LOG.exception("Failed to send waiting_for_input notification")
        return None


def detect_and_surface_blocking_prompt(
    session_id: str,
    work_item_id: Optional[str],
    prompt_text: str,
    *,
    choices: Optional[Any] = None,
    context: Optional[Any] = None,
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

    out_dir = _tool_output_dir()
    _ensure_dir(out_dir)

    # filename uses timestamp to avoid races
    stamp = str(int(time.time() * 1000))
    filename = f"pending_prompt_{session_id}_{stamp}.json"
    metadata: Dict[str, Any] = {
        "session": session_id,
        "session_id": session_id,
        "work_item": work_item_id,
        "summary": summary,
        "prompt_text": prompt_text,
        "choices": choices if choices is not None else [],
        "context": context if context is not None else [],
        "state": "waiting_for_input",
        "created_at": ts,
        "stamp": stamp,
    }
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

    metadata["prompt_file"] = path
    metadata["pending_prompt_file"] = path
    metadata["tool_output_dir"] = out_dir

    # set session state and emit event
    set_session_state(session_id, "waiting_for_input")
    emit_internal_event("waiting_for_input", metadata)
    _send_waiting_for_input_notification(metadata)

    return metadata
