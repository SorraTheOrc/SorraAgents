import json
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from ampa import conversation_manager
import session_block
from ampa import responder


def test_start_and_resume(tmp_path, monkeypatch):
    tool_dir = str(tmp_path)
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", tool_dir)

    session_id = "s-123"
    prompt = "Please confirm this change"

    meta = conversation_manager.start_conversation(
        session_id, prompt, {"work_item": "WL-1"}
    )
    assert meta["session"] == session_id
    assert meta["state"] == "waiting_for_input"

    # ensure pending prompt file exists
    prompt_file = meta["prompt_file"]
    assert os.path.exists(prompt_file)

    # resume
    res = conversation_manager.resume_session(session_id, "yes")
    assert res["status"] == "resumed"
    assert res["session"] == session_id
    assert os.path.exists(os.path.join(tool_dir, "events.jsonl"))


def test_resume_no_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    with pytest.raises(conversation_manager.NotFoundError):
        conversation_manager.resume_session("no-such", "x")


def test_resume_with_sdk_client(tmp_path, monkeypatch):
    tool_dir = str(tmp_path)
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", tool_dir)

    class DummySDK:
        def __init__(self):
            self.calls = []

        def start_conversation(self, session_id, prompt, metadata):
            self.calls.append(("start", session_id, prompt, metadata))

        def resume_session(self, session_id, response, metadata):
            self.calls.append(("resume", session_id, response, metadata))

    sdk = DummySDK()
    session_id = "s-sdk"
    conversation_manager.start_conversation(
        session_id, "prompt", {"work_item": "WL-1", "sdk_client": sdk}
    )
    res = conversation_manager.resume_session(session_id, "ok", {"sdk_client": sdk})

    assert res["status"] == "resumed"
    assert sdk.calls[0][0] == "start"
    assert sdk.calls[1][0] == "resume"


def test_resume_invalid_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    session_id = "s-foo"
    # create a pending prompt file but set session state to something else
    meta = {
        "session": session_id,
        "work_item": None,
        "summary": "x",
        "state": "waiting_for_input",
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    prompt_file = os.path.join(str(tmp_path), f"pending_prompt_{session_id}_1.json")
    with open(prompt_file, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)

    # write session state as completed
    session_block.set_session_state(session_id, "completed")

    with pytest.raises(conversation_manager.InvalidStateError):
        conversation_manager.resume_session(session_id, "ok")


def test_resume_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", str(tmp_path))
    session_id = "s-timeout"
    created_at = (datetime.utcnow() - timedelta(days=2)).isoformat() + "Z"
    meta = {
        "session": session_id,
        "work_item": None,
        "summary": "x",
        "state": "waiting_for_input",
        "created_at": created_at,
    }
    prompt_file = os.path.join(str(tmp_path), f"pending_prompt_{session_id}_1.json")
    with open(prompt_file, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)

    session_block.set_session_state(session_id, "waiting_for_input")

    with pytest.raises(conversation_manager.TimedOutError):
        conversation_manager.resume_session(session_id, "ok", timeout_seconds=10)


def test_responder_payload_resume(tmp_path, monkeypatch):
    tool_dir = str(tmp_path)
    monkeypatch.setenv("AMPA_TOOL_OUTPUT_DIR", tool_dir)

    session_id = "s-responder"
    conversation_manager.start_conversation(
        session_id, "Approve deploy?", {"work_item": "WL-2"}
    )

    payload = {"session_id": session_id, "response": "yes"}
    result = responder.resume_from_payload(payload)

    assert result["status"] == "resumed"
    assert result["session"] == session_id
