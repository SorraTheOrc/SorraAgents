#!/usr/bin/env python3
"""Unit tests for the agent-log-review session log parser (child 1 of the
agent-log-review skill epic, parent SA-0MUEKTJRD0010CGC).

Work item: SA-0MV6U4PW4007R7FE — "Agent log review: session log parser
(v3 JSONL format)".

Covers the parser-layer acceptance criteria:

- (a) parsing a session with mixed content parts (thinking, toolCall, text)
- (b) toolResult with ``isError``
- (c) malformed JSONL lines (skip + counter, never crash)
- (d) sessions with no usage data (degrade to zeros + counter)
- (e) empty sessions directory (zero sessions, no crash)
- project-slug derivation from CWD (deterministic, follows pi's encoding)
- mtime-sorted selection limited to the 20 most recent sessions
- fail-soft degradation for sessions without a session header

All tests run offline against ``tmp_path`` fixtures.
"""  # noqa: EXE001
from __future__ import annotations

import json
import os
from pathlib import Path

import agent_log_review as alr

# ── fixture helpers ────────────────────────────────────────────────────────

def _header(cwd: str = "/home/operator/project",
            session_id: str = "sess-0001",
            ts: str = "2026-01-01T00:00:00.000Z") -> dict:
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": ts,
        "cwd": cwd,
    }


def _model_change(model_id: str = "code", provider: str = "Local Proxy") -> dict:
    return {
        "type": "model_change",
        "id": "m1",
        "parentId": None,
        "timestamp": "2026-01-01T00:00:01.000Z",
        "provider": provider,
        "modelId": model_id,
    }


def _thinking_level_change() -> dict:
    return {
        "type": "thinking_level_change",
        "id": "t1",
        "parentId": "m1",
        "timestamp": "2026-01-01T00:00:02.000Z",
        "thinkingLevel": "medium",
    }


def _thinking_part(text: str) -> dict:
    return {"type": "thinking", "thinking": text, "thinkingSignature": "sig"}


def _tool_call_part(name: str, arguments: object, call_id: str = "call-1") -> dict:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}


def _text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def _assistant_msg(parts: list, usage: dict | None = None, ts: str = "2026-01-01T00:00:03.000Z") -> dict:
    msg: dict = {"role": "assistant", "content": parts, "timestamp": ts}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "message", "id": "msg-1", "parentId": None, "timestamp": ts, "message": msg}


def _tool_result_msg(tool_name: str,
                     content_text: str,
                     is_error: bool = False,
                     tool_call_id: str = "call-1",
                     ts: str = "2026-01-01T00:00:04.000Z") -> dict:
    msg = {
        "role": "toolResult",
        "toolCallId": tool_call_id,
        "toolName": tool_name,
        "content": [{"type": "text", "text": content_text}],
        "isError": is_error,
        "timestamp": ts,
    }
    return {"type": "message", "id": "msg-2", "parentId": None, "timestamp": ts, "message": msg}


def _write_session(session_dir: Path, name: str, entries: list) -> Path:
    """Write *entries* (dicts) plus one raw malformed line as a JSONL file."""
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / name
    lines = [json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


# ── slug derivation ────────────────────────────────────────────────────────

def test_project_slug_matches_pi_encoding():
    assert alr.project_slug("/home/operator/project") == "--home-operator-project--"


def test_project_slug_is_deterministic():
    a = alr.project_slug("/home/operator/project")
    b = alr.project_slug("/home/operator/project")
    assert a == b
    assert a == alr.project_slug("/home/operator/project/")


def test_project_slug_handles_nested_worktree_paths():
    cwd = "/home/operator/projects/AI_Hell/.worklog/worktrees/wl-AH-0MTGBPRYC000QIHU-player-audio"
    assert alr.project_slug(cwd) == (
        "--home-operator-projects-AI_Hell-.worklog-worktrees-wl-AH-0MTGBPRYC000QIHU-player-audio--"
    )


# ── session file discovery ─────────────────────────────────────────────────

def test_find_session_files_sorted_by_mtime_limited(tmp_path):
    sdir = tmp_path / "sessions" / "--home-operator-project--"
    sdir.mkdir(parents=True)
    # 25 files, newest first by mtime.
    for i in range(25):
        path = _write_session(sdir, f"session-{i:03d}.jsonl", [_header(session_id=f"s{i}")])
        _set_mtime(path, 1000.0 + i)  # later file = newer
    files = alr.find_session_files(sdir, max_sessions=20)
    assert len(files) == 20
    # Most-recent first: file with highest mtime comes first.
    assert files[0].name == "session-024.jsonl"
    assert files[-1].name == "session-005.jsonl"


def test_find_session_files_default_max_sessions(tmp_path):
    sdir = tmp_path / "sessions" / "--home-operator-project--"
    sdir.mkdir(parents=True)
    for i in range(25):
        path = _write_session(sdir, f"session-{i:03d}.jsonl", [_header(session_id=f"s{i}")])
        _set_mtime(path, 1000.0 + i)
    # Default limit is 20 (module constant).
    files = alr.find_session_files(sdir)
    assert len(files) == 20


def test_find_session_files_excludes_non_jsonl(tmp_path):
    sdir = tmp_path / "sessions" / "--home-operator-project--"
    sdir.mkdir(parents=True)
    _write_session(sdir, "a.jsonl", [_header(session_id="s1")])
    (sdir / "README.md").write_text("not a session\n", encoding="utf-8")
    assert len(alr.find_session_files(sdir)) == 1


def test_find_session_files_missing_dir_returns_empty(tmp_path):
    assert alr.find_session_files(tmp_path / "does-not-exist") == []


# ── parsing ────────────────────────────────────────────────────────────────

def test_parse_mixed_content_parts(tmp_path):
    """(a) A session with thinking + toolCall + text parts is parsed fully."""
    entries = [
        _header(),
        _model_change(),
        _thinking_level_change(),
        _assistant_msg([
            _thinking_part("Hmm, let me think about this."),
            _tool_call_part("bash", {"cmd": "ls -la"}, call_id="call-1"),
            _tool_call_part("read", {"path": "README.md"}, call_id="call-2"),
            _text_part("I'll list the directory."),
        ]),
    ]
    path = _write_session(tmp_path, "session.jsonl", entries)
    parsed = alr.parse_session(path)
    assert parsed.session_id == "sess-0001"
    assert parsed.cwd == "/home/operator/project"
    assert parsed.version == 3
    assert parsed.model == {"provider": "Local Proxy", "modelId": "code"}

    assert [tc.name for tc in parsed.tool_calls] == ["bash", "read"]
    assert parsed.tool_calls[0].arguments == {"cmd": "ls -la"}
    assert parsed.tool_calls[1].arguments == {"path": "README.md"}
    assert parsed.tool_calls[0].timestamp == "2026-01-01T00:00:03.000Z"

    assert len(parsed.thinking) == 1
    assert parsed.thinking[0].text == "Hmm, let me think about this."

    assert len(parsed.text) == 1
    assert parsed.text[0].text == "I'll list the directory."

    assert parsed.messages == 1
    assert parsed.malformed_lines == 0


def test_parse_tool_result_with_is_error(tmp_path):
    """(b) toolResult isError flag and content are extracted."""
    entries = [
        _header(),
        _tool_result_msg("bash", "Traceback (most recent call last)...",
                         is_error=True, tool_call_id="call-1"),
        _tool_result_msg("read", "file contents here",
                         is_error=False, tool_call_id="call-2"),
    ]
    path = _write_session(tmp_path, "session.jsonl", entries)
    parsed = alr.parse_session(path)
    results = parsed.tool_results
    assert len(results) == 2
    assert results[0].is_error is True
    assert results[0].tool_name == "bash"
    assert "Traceback" in results[0].content
    assert results[0].tool_call_id == "call-1"
    assert results[1].is_error is False
    assert results[1].content == "file contents here"


def test_parse_malformed_jsonl_lines_skipped(tmp_path):
    """(c) A malformed JSONL line is skipped and counted, not fatal."""
    sdir = tmp_path
    path = _write_session(sdir, "session.jsonl", [
        _header(),
        _assistant_msg([_tool_call_part("bash", {"cmd": "ls"})]),
    ])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{ this is not valid json !!!\n")
        fh.write("{\n")  # truncated object
    parsed = alr.parse_session(path)
    assert parsed.malformed_lines == 2
    # Valid entries before the malformed lines are still present.
    assert len(parsed.tool_calls) == 1
    assert parsed.session_id == "sess-0001"


def test_parse_empty_and_blank_lines_ignored(tmp_path):
    sdir = tmp_path
    path = sdir / "session.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    parsed = alr.parse_session(path)
    assert parsed.malformed_lines == 0
    assert parsed.tool_calls == []


def test_parse_missing_usage_degrades_to_zero(tmp_path):
    """(d) Assistant messages without a usage block degrade to zeros + counter."""
    entries = [
        _header(),
        _assistant_msg([_text_part("no usage here")]),
    ]
    path = _write_session(tmp_path, "session.jsonl", entries)
    parsed = alr.parse_session(path)
    assert parsed.usage == {"input": 0, "output": 0, "totalTokens": 0}

    # The scan looks under <session_root>/<slug>/ — place the file there.
    sdir = tmp_path / alr.project_slug("/home/operator/project")
    _write_session(sdir, "session.jsonl", entries)
    scan = alr.read_sessions(cwd="/home/operator/project",
                             session_root=tmp_path,
                             max_sessions=20)
    assert scan.sessions[0].usage == {"input": 0, "output": 0, "totalTokens": 0}
    assert scan.missing_usage == 1


def test_parse_usage_aggregated_across_messages(tmp_path):
    entries = [
        _header(),
        _assistant_msg([_text_part("first")], usage={"input": 10, "output": 20, "totalTokens": 30}),
        _assistant_msg([_text_part("second")], usage={"input": 1, "output": 2, "totalTokens": 3}),
    ]
    path = _write_session(tmp_path, "session.jsonl", entries)
    parsed = alr.parse_session(path)
    assert parsed.usage == {"input": 11, "output": 22, "totalTokens": 33}
    assert parsed.has_usage is True


def test_parse_missing_session_header_degrades(tmp_path):
    """Sessions without a session header degrade (ids empty), never crash."""
    entries = [
        _model_change(),
        _assistant_msg([_text_part("no header before me")]),
    ]
    path = _write_session(tmp_path, "session.jsonl", entries)
    parsed = alr.parse_session(path)
    assert parsed.session_id is None
    assert parsed.cwd is None
    assert len(parsed.text) == 1  # content still extracted


def test_parse_events_interleaving_order(tmp_path):
    """Tool calls and results are interleaved in file order (seq preserved)."""
    entries = [
        _header(),
        _assistant_msg([
            _tool_call_part("bash", {"cmd": "ls"}, call_id="call-1"),
        ]),
        _tool_result_msg("bash", "file list", is_error=False, tool_call_id="call-1"),
        _assistant_msg([
            _tool_call_part("read", {"path": "x"}, call_id="call-2"),
        ]),
        _tool_result_msg("read", "contents", is_error=False, tool_call_id="call-2"),
    ]
    path = _write_session(tmp_path, "session.jsonl", entries)
    parsed = alr.parse_session(path)
    assert len(parsed.events) == 4
    names = [(type(e).__name__, getattr(e, "tool_name", None) or getattr(e, "name", None))
             for e in parsed.events]
    assert names == [
        ("ToolCall", "bash"),
        ("ToolResult", "bash"),
        ("ToolCall", "read"),
        ("ToolResult", "read"),
    ]
    # seq is strictly increasing in file order.
    seqs = [e.seq for e in parsed.events]
    assert seqs == sorted(seqs)


# ── end-to-end scan ────────────────────────────────────────────────────────

def test_read_sessions_empty_directory(tmp_path):
    """(e) Empty sessions directory yields zero sessions, no crash."""
    scan = alr.read_sessions(cwd="/home/operator/project",
                             session_root=tmp_path,
                             max_sessions=20)
    assert scan.sessions == []
    assert scan.files_found == 0
    assert scan.malformed_lines == 0


def test_read_sessions_end_to_end(tmp_path):
    cwd = "/home/operator/project"
    sdir = tmp_path / alr.project_slug(cwd)
    _write_session(sdir, "a.jsonl", [
        _header(session_id="s1"),
        _assistant_msg([_tool_call_part("bash", {"cmd": "pwd"})]),
        _tool_result_msg("bash", "ok", is_error=False),
    ])
    _write_session(sdir, "b.jsonl", [
        _header(session_id="s2"),
        _assistant_msg([_text_part("hello")]),
    ])
    scan = alr.read_sessions(cwd=cwd, session_root=tmp_path, max_sessions=20)
    assert scan.files_found == 2
    assert scan.files_analysed == 2
    assert sorted(s.session_id for s in scan.sessions) == ["s1", "s2"]
    # The session dir is derived from the slug.
    assert scan.session_dir == str(sdir)
    assert scan.cwd == cwd


def test_read_sessions_respects_max_sessions(tmp_path):
    cwd = "/home/operator/project"
    sdir = tmp_path / alr.project_slug(cwd)
    for i in range(5):
        path = _write_session(sdir, f"s{i}.jsonl", [_header(session_id=f"s{i}")])
        _set_mtime(path, 1000.0 + i)
    scan = alr.read_sessions(cwd=cwd, session_root=tmp_path, max_sessions=2)
    assert scan.files_found == 2
    assert [s.session_id for s in scan.sessions] == ["s4", "s3"]


def test_session_root_honours_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path))
    assert alr.get_session_root() == tmp_path
