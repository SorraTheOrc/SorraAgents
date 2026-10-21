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
import sys
from pathlib import Path

import agent_log_review as alr
import pytest

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


# ══════════════════════════════════════════════════════════════════════════
# Child 2 — heuristic confusion-pattern extraction
# (SA-0MV6UDSAF0064B85)
# ══════════════════════════════════════════════════════════════════════════


def _tool_call(filename, name, args, seq, call_id="c"):
    return alr.ToolCall(filename=filename, seq=seq, id=call_id, name=name,
                        arguments=args, timestamp="2026-01-01T00:00:00Z")


def _tool_result(filename, tool_name, content, seq, is_error=False):
    return alr.ToolResult(filename=filename, seq=seq, tool_call_id="c",
                          tool_name=tool_name, content=content, is_error=is_error,
                          timestamp="2026-01-01T00:00:01Z")


def _session(filename, events=None, thinking_texts=None, seq_start=0):
    events = events or []
    thinking = [
        alr.ThinkingBlock(filename=filename, seq=seq_start + i, text=text,
                          timestamp="t")
        for i, text in enumerate(thinking_texts or [])
    ]
    return alr.ParsedSession(
        filename=filename, path=f"/tmp/{filename}", session_id="s",
        cwd="/home/operator/project", timestamp="t", version=3,
        model={"provider": "x", "modelId": "y"},
        messages=0, thinking=thinking, text=[], events=events,
        malformed_lines=0, has_usage=True,
    )


# ── tool call distribution ────────────────────────────────────────────────

def test_tool_distribution_counts_and_error_rates():
    s1 = _session("a.jsonl", events=[
        _tool_call("a.jsonl", "bash", {"cmd": "ls"}, 1),
        _tool_result("a.jsonl", "bash", "Traceback (most recent call last)", 2, is_error=True),
        _tool_call("a.jsonl", "read", {"path": "x"}, 3),
        _tool_result("a.jsonl", "read", "file contents", 4),
        _tool_call("a.jsonl", "bash", {"cmd": "ls -la"}, 5),
        _tool_result("a.jsonl", "bash", "ok", 6),
    ])
    s2 = _session("b.jsonl", events=[
        _tool_call("b.jsonl", "bash", {"cmd": "pwd"}, 1),
        _tool_result("b.jsonl", "bash", "Permission denied", 2),
    ])
    dist = alr.extract_tool_distribution([s1, s2])
    assert dist["total_calls"] == 4
    assert dist["total_error_results"] == 2  # explicit isError + pattern match
    by_tool = dist["by_tool"]
    assert by_tool["bash"]["calls"] == 3
    assert by_tool["bash"]["error_results"] == 2
    assert by_tool["bash"]["error_rate"] == round(2 / 3, 3)
    assert by_tool["read"]["calls"] == 1
    assert by_tool["read"]["error_results"] == 0
    assert by_tool["read"]["error_rate"] == 0.0


def test_tool_distribution_deterministic_tool_ordering():
    s = _session("a.jsonl", events=[
        _tool_call("a.jsonl", "read", {"path": "x"}, 1),
        _tool_call("a.jsonl", "bash", {"cmd": "ls"}, 2),
    ])
    dist = alr.extract_tool_distribution([s])
    assert list(dist["by_tool"].keys()) == ["bash", "read"]


# ── thinking-content error signals ─────────────────────────────────────────

def test_thinking_signals_keyword_hits_with_context():
    s = _session("a.jsonl", thinking_texts=[
        "Hmm, that approach is wrong, let me fix it.",
        "Nothing to see here, everything worked.",
    ])
    sig = alr.extract_thinking_signals([s])
    hits = sig["hits"]
    assert sig["total_hits"] > 0
    kw_hits = {h["keyword"]: h["count"] for h in hits}
    assert kw_hits.get("wrong", 0) >= 1
    assert kw_hits.get("fix", 0) >= 1
    assert kw_hits.get("hmm", 0) >= 1
    # Context window preserves surrounding text (evidence for the LLM).
    wrong_hits = [h for h in hits if h["keyword"] == "wrong"]
    assert wrong_hits and "approach is wrong, let me fix" in wrong_hits[0]["context"]
    # Deterministic ordering by (session, keyword).
    sessions = [h["session"] for h in hits]
    assert sessions == sorted(sessions)


def test_thinking_signals_borderline_false_positive_kept_with_context():
    """Legitimate uses ("error handling") stay visible so the LLM decides."""
    s = _session("a.jsonl", thinking_texts=[
        "This code does proper error handling for network calls.",
    ])
    sig = alr.extract_thinking_signals([s])
    error_hits = [h for h in sig["hits"] if h["keyword"] == "error"]
    assert error_hits
    assert "proper error handling" in error_hits[0]["context"]


def test_thinking_signals_no_keyword_hits():
    s = _session("a.jsonl", thinking_texts=[
        "All checks passed with no concerns.",
    ])
    sig = alr.extract_thinking_signals([s])
    assert sig["total_hits"] == 0
    assert sig["hits"] == []


def test_thinking_signals_case_insensitive():
    s = _session("a.jsonl", thinking_texts=["ERROR in the log"])
    sig = alr.extract_thinking_signals([s])
    assert any(h["keyword"] == "error" and h["count"] >= 1 for h in sig["hits"])


def test_thinking_signals_excerpt_capped_per_session():
    big = "hmm " + ("error " * 500)
    s = _session("a.jsonl", thinking_texts=[big])
    sig = alr.extract_thinking_signals([s])
    for h in sig["hits"]:
        assert len(h["context"].encode("utf-8")) <= alr.EXCERPT_BYTES_PER_SESSION


# ── tool result errors ─────────────────────────────────────────────────────

def test_tool_errors_detects_is_error_flag():
    s = _session("a.jsonl", events=[
        _tool_result("a.jsonl", "bash", "Traceback (most recent call last):\n  File \"x.py\"", 1, is_error=True),
    ])
    err = alr.extract_tool_errors([s])
    assert err["total"] == 1
    entry = err["errors"][0]
    assert entry["tool"] == "bash"
    assert entry["is_error"] is True
    assert entry["kind"] == "traceback"
    assert "Traceback" in entry["excerpt"]


def test_tool_errors_detects_pattern_without_is_error_flag():
    s = _session("a.jsonl", events=[
        _tool_result("a.jsonl", "read",
                     "bash: line 1: cd: /nonexistent: No such file or directory", 1,
                     is_error=False),
    ])
    err = alr.extract_tool_errors([s])
    assert err["total"] == 1
    assert err["errors"][0]["kind"] == "command_not_found"
    assert err["errors"][0]["is_error"] is False


def test_tool_errors_permission_denied():
    s = _session("a.jsonl", events=[
        _tool_result("a.jsonl", "bash", "Permission denied", 1, is_error=True),
    ])
    err = alr.extract_tool_errors([s])
    assert err["total"] == 1
    assert err["errors"][0]["kind"] == "permission_denied"


def test_tool_errors_ignores_benign_results():
    s = _session("a.jsonl", events=[
        _tool_result("a.jsonl", "bash", "ok all good", 1, is_error=False),
        _tool_result("a.jsonl", "read", "success", 2, is_error=False),
    ])
    err = alr.extract_tool_errors([s])
    assert err["total"] == 0


# ── retry / self-correction patterns ──────────────────────────────────────

def test_retry_repeated_identical_call():
    s = _session("a.jsonl", events=[
        _tool_call("a.jsonl", "bash", {"cmd": "ls"}, 1),
        _tool_result("a.jsonl", "bash", "ok", 2),
        _tool_call("a.jsonl", "bash", {"cmd": "ls"}, 3),
        _tool_result("a.jsonl", "bash", "ok", 4),
        _tool_call("a.jsonl", "bash", {"cmd": "ls"}, 5),
    ])
    pat = alr.extract_retry_patterns([s])
    repeated = [p for p in pat["patterns"] if p["kind"] == "repeated_call"]
    assert len(repeated) == 1
    assert repeated[0]["tool"] == "bash"
    assert repeated[0]["count"] == 3
    assert repeated[0]["arguments"] == '{"cmd": "ls"}'


def test_retry_call_error_modify():
    """Tool call → error result → modified tool call is detected."""
    s = _session("a.jsonl", events=[
        _tool_call("a.jsonl", "bash", {"cmd": "cat /nonexistent"}, 1),
        _tool_result("a.jsonl", "bash", "No such file or directory", 2, is_error=True),
        _tool_call("a.jsonl", "bash", {"cmd": "cat /existing"}, 3),
        _tool_result("a.jsonl", "bash", "ok", 4),
    ])
    pat = alr.extract_retry_patterns([s])
    modify = [p for p in pat["patterns"] if p["kind"] == "call_error_modify"]
    assert len(modify) == 1
    assert modify[0]["tool"] == "bash"
    assert "nonexistent" in modify[0]["original_arguments"]
    assert "existing" in modify[0]["modified_arguments"]


def test_retry_no_self_correction_when_args_unchanged():
    s = _session("a.jsonl", events=[
        _tool_call("a.jsonl", "bash", {"cmd": "ls"}, 1),
        _tool_result("a.jsonl", "bash", "error", 2, is_error=True),
        _tool_call("a.jsonl", "bash", {"cmd": "ls"}, 3),  # identical retry
    ])
    pat = alr.extract_retry_patterns([s])
    modify = [p for p in pat["patterns"] if p["kind"] == "call_error_modify"]
    assert modify == []
    # The identical retry still counts as a repeated call.
    repeated = [p for p in pat["patterns"] if p["kind"] == "repeated_call"]
    assert len(repeated) == 1
    assert repeated[0]["count"] == 2


def test_retry_string_arguments_canonicalised():
    s = _session("a.jsonl", events=[
        _tool_call("a.jsonl", "bash", "ls", 1),
        _tool_call("a.jsonl", "bash", "ls", 2),
    ])
    pat = alr.extract_retry_patterns([s])
    repeated = [p for p in pat["patterns"] if p["kind"] == "repeated_call"]
    assert len(repeated) == 1
    assert repeated[0]["arguments"] == "ls"


# ── orchestrator (max_sessions / errors_only) ─────────────────────────────

def test_extract_signals_max_sessions_truncates():
    s1 = _session("a.jsonl", thinking_texts=["error one"])
    s2 = _session("b.jsonl", thinking_texts=["error two"])
    sig = alr.extract_signals([s1, s2], max_sessions=1)
    assert sig["session_count"] == 1


def test_extract_signals_errors_only_filters_to_error_sessions():
    ok = _session("ok.jsonl", events=[
        _tool_call("ok.jsonl", "bash", {"cmd": "ls"}, 1),
        _tool_result("ok.jsonl", "bash", "all good", 2),
    ])
    bad = _session("bad.jsonl", events=[
        _tool_call("bad.jsonl", "bash", {"cmd": "x"}, 1),
        _tool_result("bad.jsonl", "bash", "Permission denied", 2, is_error=True),
    ])
    sig = alr.extract_signals([ok, bad], errors_only=True)
    assert sig["session_count"] == 1
    assert sig["tool_errors"]["total"] == 1
    assert sig["tool_errors"]["errors"][0]["session"] == "bad.jsonl"


def test_extract_signals_bundles_all_signal_types():
    s = _session("a.jsonl",
                 events=[
                     _tool_call("a.jsonl", "bash", {"cmd": "ls"}, 1),
                     _tool_result("a.jsonl", "bash", "Traceback", 2, is_error=True),
                 ],
                 thinking_texts=["hmm, this is wrong"])
    sig = alr.extract_signals([s])
    assert set(sig.keys()) >= {
        "session_count", "session_files", "tool_distribution", "thinking_signals",
        "tool_errors", "retry_patterns",
    }
    assert sig["session_files"] == ["a.jsonl"]
    assert sig["tool_distribution"]["total_calls"] == 1
    assert sig["tool_errors"]["total"] == 1
    assert sig["thinking_signals"]["total_hits"] >= 2


# ══════════════════════════════════════════════════════════════════════════
# Child 3 — LLM-assisted pattern classification
# (SA-0MV6UETWV002CF5T)
# ══════════════════════════════════════════════════════════════════════════

import types


def _fake_runner(stdout: str, rc: int = 0):
    """A pi runner that records the command and returns a canned response."""
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


def _capture_runner(responses):
    """A runner that appends each command to a list and returns responses."""
    captured = []

    def runner(cmd):
        captured.append(cmd)
        return _fake_runner(responses.pop(0))

    return runner, captured


class _Runner:
    """Duck-typed runner wrapper for fixed responses."""

    def __init__(self, stdout="", rc=0):
        self.stdout = stdout
        self.rc = rc
        self.commands = []

    def __call__(self, cmd):
        self.commands.append(cmd)
        return _fake_runner(self.stdout, self.rc)


WELL_FORMED_RESPONSE = json.dumps({
    "categories": [
        {
            "name": "shell quoting issues",
            "confidence": 0.9,
            "evidence_sessions": ["a.jsonl"],
            "summary": "Backticks expanded in shell commands.",
        }
    ],
    "recommendations": [
        {
            "title": "Warn when backticks are used in bash commands",
            "issue_type": "chore",
            "target_skill": "implement",
            "rationale": "Backtick expansion caused repeated retries.",
            "evidence_sessions": ["a.jsonl"],
        }
    ],
})


def _classification_fixture():
    s1 = _session("a.jsonl",
                  events=[
                      _tool_call("a.jsonl", "bash", {"cmd": "echo `ls`"}, 1),
                      _tool_result("a.jsonl", "bash", "Traceback (command not found: ls`)", 2, is_error=True),
                      _tool_call("a.jsonl", "bash", {"cmd": "echo \"$(ls)\""}, 3),
                  ],
                  thinking_texts=["hmm, the backticks expanded — that is wrong"])
    s2 = _session("b.jsonl",
                  events=[_tool_call("b.jsonl", "read", {"path": "x.py"}, 1)],
                  thinking_texts=["nothing concerning here"])
    return alr.extract_signals([s1, s2])


def test_classify_prompt_excludes_raw_log_content_and_cites_sessions():
    signals = _classification_fixture()
    runner = _Runner()
    alr.classify_patterns(signals, runner=runner)
    assert runner.commands
    prompt = runner.commands[0][-1]
    # Evidence mapping: session file names present.
    assert "a.jsonl" in prompt
    assert "b.jsonl" in prompt
    # Prompt carries only distilled signals, never raw thinking text... the
    # capped context keeps short excerpts, so assert full sentences are absent.
    assert "nothing concerning here" not in prompt
    assert "echo \"$(ls)\"" not in prompt


def test_classify_prompt_stays_under_budget():
    signals = _classification_fixture()
    runner = _Runner()
    result = alr.classify_patterns(signals, runner=runner)
    assert result["prompt_bytes"] <= alr.PROMPT_BUDGET_BYTES
    prompt = runner.commands[0][-1]
    assert len(prompt.encode("utf-8")) <= alr.PROMPT_BUDGET_BYTES


def test_classify_patterns_parses_well_formed_response():
    signals = _classification_fixture()
    runner = _Runner(stdout=WELL_FORMED_RESPONSE)
    result = alr.classify_patterns(signals, runner=runner)
    assert result["error"] is None
    assert len(result["categories"]) == 1
    cat = result["categories"][0]
    assert cat["name"] == "shell quoting issues"
    assert cat["confidence"] == 0.9
    assert cat["evidence_sessions"] == ["a.jsonl"]
    assert len(result["recommendations"]) == 1
    rec = result["recommendations"][0]
    assert rec["issue_type"] == "chore"
    assert rec["target_skill"] == "implement"


def test_classify_patterns_tolerates_code_fence():
    signals = _classification_fixture()
    fenced = "```json\n" + WELL_FORMED_RESPONSE + "\n```"
    result = alr.classify_patterns(signals, runner=_Runner(stdout=fenced))
    assert result["error"] is None
    assert result["categories"][0]["name"] == "shell quoting issues"


def test_classify_patterns_malformed_response_is_diagnostic():
    signals = _classification_fixture()
    result = alr.classify_patterns(signals, runner=_Runner(stdout="{ not json !!!"))
    assert result["error"] is not None
    assert "malformed JSON" in result["error"]
    assert result["categories"] == []
    assert result["recommendations"] == []


def test_classify_patterns_empty_response_is_diagnostic():
    signals = _classification_fixture()
    result = alr.classify_patterns(signals, runner=_Runner(stdout=""))
    assert result["error"] is not None
    assert result["categories"] == []


def test_classify_patterns_pi_failure_is_diagnostic():
    signals = _classification_fixture()
    result = alr.classify_patterns(signals, runner=_Runner(rc=1))
    assert result["error"] is not None
    assert "pi classification call failed" in result["error"]
    assert result["categories"] == []


def test_classify_patterns_prompt_budget_overflow_raises():
    signals = _classification_fixture()
    with pytest.raises(alr.ClassificationError):
        alr.classify_patterns(signals, runner=_Runner(), prompt_budget=100)


def test_call_pi_builds_context_reduced_command():
    signals = _classification_fixture()
    runner = _Runner(stdout=WELL_FORMED_RESPONSE)
    alr.classify_patterns(signals, model="m/model", runner=runner)
    cmd = runner.commands[0]
    assert "-p" in cmd and "--print" in cmd
    assert "--model" in cmd and "m/model" in cmd
    assert "--no-tools" in cmd
    for flag in ("--no-context-files", "--no-skills", "--no-extensions"):
        assert flag in cmd
    assert cmd[-1] == runner.commands[0][-1]  # prompt is the last arg


def test_redact_secrets_basic():
    text = (
        "key=sk-abcdef1234567890abcdef1234567890 token=Bearer eyJhbGciOiJIUzI1NiJ9 "
        "path1=/home/u/.ssh/id_rsa path2=/.env.local"
    )
    redacted = alr.redact_secrets(text)
    assert "sk-abcdef1234567890abcdef1234567890" not in redacted
    assert "<API_KEY>" in redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "Bearer <TOKEN>" in redacted
    assert ".ssh" in redacted and "id_rsa" not in redacted
    assert "env<redacted>" in redacted


def test_prompt_is_redacted_before_send():
    signals = _classification_fixture()
    # Inject a secret-looking thinking excerpt through the fixture.
    s = _session("secret.jsonl",
                 thinking_texts=["hmm, my token sk-abcdef1234567890abcdef1234567890 leaked"])
    signals = alr.extract_signals([s])
    runner = _Runner(stdout=WELL_FORMED_RESPONSE)
    alr.classify_patterns(signals, runner=runner)
    prompt = runner.commands[0][-1]
    assert "sk-abcdef1234567890abcdef1234567890" not in prompt
    assert "<API_KEY>" in prompt




# ══════════════════════════════════════════════════════════════════════════
# Child 4 — structured report rendering
# (SA-0MV6UFVV30059ZPC)
# ══════════════════════════════════════════════════════════════════════════


def _analysis_fixture() -> dict:
    """A realistic analysis document for rendering tests."""
    return {
        "scan": {
            "cwd": "/home/operator/project",
            "session_dir": "/home/operator/.pi/agent/sessions/--home-operator-project--",
            "files_found": 2,
            "files_analysed": 2,
            "malformed_lines": 1,
            "missing_usage": 0,
            "sessions": [
                {"filename": "a.jsonl", "session_id": "s1",
                 "started_at": "2026-01-01T00:00:00.000Z", "cwd": "/home/operator/project"},
                {"filename": "b.jsonl", "session_id": "s2",
                 "started_at": "2026-01-02T00:00:00.000Z", "cwd": "/home/operator/project"},
            ],
        },
        "signals": {
            "session_count": 2,
            "session_files": ["a.jsonl", "b.jsonl"],
            "tool_distribution": {
                "total_calls": 3,
                "total_error_results": 2,
                "by_tool": {"bash": {"calls": 2, "error_results": 2, "error_rate": 1.0}},
            },
            "thinking_signals": {
                "total_hits": 3,
                "hits": [
                    {"session": "a.jsonl", "keyword": "hmm", "count": 2, "context": "hmm ..."},
                    {"session": "b.jsonl", "keyword": "hmm", "count": 1, "context": "hmm?"},
                ],
            },
            "tool_errors": {
                "total": 2,
                "errors": [
                    {"session": "a.jsonl", "tool": "bash", "kind": "command_not_found",
                     "is_error": True, "excerpt": "not found"},
                    {"session": "a.jsonl", "tool": "bash", "kind": "command_not_found",
                     "is_error": True, "excerpt": "not found too"},
                ],
            },
            "retry_patterns": {
                "total": 1,
                "patterns": [
                    {"session": "b.jsonl", "kind": "repeated_call", "tool": "bash",
                     "arguments": "x", "count": 3},
                ],
            },
        },
        "classification": {
            "error": None,
            "categories": [
                {"name": "shell quoting issues", "confidence": 0.9,
                 "evidence_sessions": ["a.jsonl"], "summary": "Backticks expanded."},
            ],
            "recommendations": [
                {"title": "Warn about backticks in bash commands",
                 "issue_type": "chore", "target_skill": "implement",
                 "rationale": "Backtick expansion caused retries.",
                 "evidence_sessions": ["a.jsonl"]},
            ],
        },
    }


def test_render_report_markdown_has_required_sections():
    md = alr.render_report(_analysis_fixture(), fmt="markdown")
    assert "# Agent Log Review" in md
    assert "## Summary" in md
    assert "Sessions analysed: 2" in md
    assert "Date range: 2026-01-01T00:00:00.000Z .. 2026-01-02T00:00:00.000Z" in md
    assert "malformed line(s) skipped" in md
    assert "## Top Patterns" in md
    assert "## Recommended Improvements" in md
    assert "Warn about backticks in bash commands" in md
    assert "## Evidence Citations" in md
    assert "a.jsonl" in md


def test_render_report_top_patterns_deterministic_ordering():
    parsed = json.loads(alr.render_report(_analysis_fixture(), fmt="json"))
    patterns = parsed["top_patterns"]
    counts = [p["count"] for p in patterns]
    assert counts == sorted(counts, reverse=True)
    for i in range(len(patterns) - 1):
        if patterns[i]["count"] == patterns[i + 1]["count"]:
            assert patterns[i]["name"] < patterns[i + 1]["name"]
    by_name = {p["name"]: p for p in patterns}
    assert by_name["thinking:hmm"]["count"] == 3
    assert by_name["thinking:hmm"]["sessions"] == ["a.jsonl", "b.jsonl"]
    assert by_name["tool_error:command_not_found"]["sessions"] == ["a.jsonl"]


def test_render_report_json_structure():
    parsed = json.loads(alr.render_report(_analysis_fixture(), fmt="json"))
    assert set(parsed.keys()) == {
        "summary", "top_patterns", "recommendations", "evidence_citations",
    }
    assert parsed["summary"]["sessions_analysed"] == 2
    assert parsed["summary"]["date_range"] == {
        "from": "2026-01-01T00:00:00.000Z", "to": "2026-01-02T00:00:00.000Z",
    }
    assert parsed["recommendations"][0]["title"] == "Warn about backticks in bash commands"
    assert parsed["recommendations"][0]["evidence_sessions"] == ["a.jsonl"]
    assert len(parsed["evidence_citations"]) >= 1
    for cit in parsed["evidence_citations"]:
        assert "/" not in cit["session"]


def test_render_report_redacts_secrets():
    analysis = _analysis_fixture()
    # Secrets in a *rendered* field (recommendation rationale) are redacted.
    analysis["classification"]["recommendations"][0]["rationale"] = (
        "saw token sk-abcdef1234567890abcdef1234567890 under /home/u/.ssh/id_rsa"
    )
    md = alr.render_report(analysis, fmt="markdown")
    assert "sk-abcdef1234567890abcdef1234567890" not in md
    assert "<API_KEY>" in md
    assert "id_rsa" not in md


def test_render_report_redacts_secrets_in_json():
    analysis = _analysis_fixture()
    analysis["classification"]["recommendations"][0]["rationale"] = (
        "saw token sk-abcdef1234567890abcdef1234567890"
    )
    parsed = json.loads(alr.render_report(analysis, fmt="json"))
    rationale = parsed["recommendations"][0]["rationale"]
    assert "sk-abcdef1234567890abcdef1234567890" not in rationale
    assert "<API_KEY>" in rationale


def test_render_report_deterministic_same_input():
    a = alr.render_report(_analysis_fixture(), fmt="markdown")
    b = alr.render_report(_analysis_fixture(), fmt="markdown")
    assert a == b
    assert alr.render_report(_analysis_fixture(), fmt="json") == alr.render_report(
        _analysis_fixture(), fmt="json"
    )


def test_render_report_unknown_format_raises():
    with pytest.raises(ValueError):
        alr.render_report(_analysis_fixture(), fmt="xml")


def test_render_report_missing_usage_in_summary():
    analysis = _analysis_fixture()
    analysis["scan"]["missing_usage"] = 1
    md = alr.render_report(analysis, fmt="markdown")
    assert "1 session(s) missing usage data" in md


def test_render_report_classification_error_diagnostic():
    analysis = _analysis_fixture()
    analysis["classification"]["error"] = "model returned malformed JSON: nope"
    analysis["classification"]["categories"] = []
    analysis["classification"]["recommendations"] = []
    md = alr.render_report(analysis, fmt="markdown")
    assert "Classification: not available" in md
    assert "model returned malformed JSON" in md


def test_build_analysis_and_report_end_to_end():
    s1 = _session("a.jsonl",
                  events=[
                      _tool_call("a.jsonl", "bash", {"cmd": "ls `pwd`"}, 1),
                      _tool_result("a.jsonl", "bash", "command not found", 2, is_error=True),
                  ],
                  thinking_texts=["hmm, wrong quoting"])
    scan = alr.SessionScan(
        cwd="/home/operator/project",
        session_dir="/x/sessions",
        files_found=1,
        files_analysed=1,
        malformed_lines=0,
        missing_usage=0,
        sessions=[s1],
    )
    runner = _Runner(stdout=WELL_FORMED_RESPONSE)
    signals = alr.extract_signals([s1])
    classification = alr.classify_patterns(signals, runner=runner)
    analysis = alr.build_analysis(scan, classification=classification)
    md = alr.render_report(analysis, fmt="markdown")
    assert "Sessions analysed: 1" in md
    assert "Warn when backticks are used in bash commands" in md


# ══════════════════════════════════════════════════════════════════════════
# Child 5 — worklog integration (comments + child items)
# (SA-0MV6UGWC4000GU0G)
# ══════════════════════════════════════════════════════════════════════════


class _WlStub:
    """A mocked ``wl`` runner that records commands and simulates a store."""

    def __init__(self, children=None):
        self.commands = []
        self.children = list(children or [])
        self.created = []
        self._next = 1

    def __call__(self, cmd):
        self.commands.append(list(cmd))
        if "show" in cmd and "--children" in cmd:
            payload = {"success": True, "children": list(self.children)}
            return _fake_runner(json.dumps(payload))
        if "comment" in cmd and "add" in cmd:
            return _fake_runner(json.dumps({"success": True}))
        if "create" in cmd:
            title = ""
            for i, tok in enumerate(cmd):
                if tok in ("-t", "--title") and i + 1 < len(cmd):
                    title = cmd[i + 1]
            new_id = f"NEW-{self._next:03d}"
            self._next += 1
            self.created.append((new_id, title))
            self.children.append({"id": new_id, "title": title})
            return _fake_runner(json.dumps({"success": True, "workItem": {"id": new_id}}))
        return _fake_runner(json.dumps({"success": True}))


RECS = [
    {"title": "Warn about backticks in bash commands", "issue_type": "chore",
     "target_skill": "implement", "rationale": "Backtick expansion caused retries.",
     "evidence_sessions": ["a.jsonl"]},
    {"title": "Add python3 import guard", "issue_type": "feature",
     "target_skill": "shared", "rationale": "NameError from missing imports.",
     "evidence_sessions": ["b.jsonl"]},
]

PARENT = "SA-0MUEKTJRD0010CGC"


def test_file_recommendations_files_comment():
    stub = _WlStub()
    result = alr.file_recommendations(RECS, PARENT, report_markdown="## Report\nbody",
                                      wl_runner=stub, worklog_flags=[])
    assert result["comment_posted"] is True
    comment_cmds = [c for c in stub.commands if "comment" in c and "add" in c]
    assert len(comment_cmds) == 1
    cmd = comment_cmds[0]
    assert PARENT in cmd
    # The report body is included in the comment.
    assert any("## Report" in tok for tok in cmd)


def test_file_recommendations_creates_children_with_linking():
    stub = _WlStub()
    result = alr.file_recommendations(RECS, PARENT, wl_runner=stub, worklog_flags=[])
    assert len(result["created"]) == 2
    create_cmds = [c for c in stub.commands if "create" in c]
    assert len(create_cmds) == 2
    for cmd in create_cmds:
        assert "--parent" in cmd and PARENT in cmd
        assert "--assignee" in cmd and "Map" in cmd
        assert "--issue-type" in cmd
        joined = " ".join(cmd)
        assert f"related-to:{PARENT}" in joined
    chore_cmd = next(c for c in create_cmds
                    if any("Warn about backticks" in tok for tok in c))
    assert chore_cmd[chore_cmd.index("--issue-type") + 1] == "chore"
    assert chore_cmd[chore_cmd.index("--priority") + 1] == "low"
    feat_cmd = next(c for c in create_cmds
                    if any("import guard" in tok for tok in c))
    assert feat_cmd[feat_cmd.index("--issue-type") + 1] == "feature"
    assert feat_cmd[feat_cmd.index("--priority") + 1] == "medium"


def test_file_recommendations_idempotent_reuses_existing():
    # First run creates two children.
    stub = _WlStub()
    first = alr.file_recommendations(RECS, PARENT, wl_runner=stub, worklog_flags=[])
    assert len(first["created"]) == 2
    # Re-run against the same (now-populated) stub store.
    create_before = len([c for c in stub.commands if "create" in c])
    second = alr.file_recommendations(RECS, PARENT, wl_runner=stub, worklog_flags=[])
    assert second["created"] == []
    assert len(second["reused"]) == 2
    assert len([c for c in stub.commands if "create" in c]) == create_before


def test_file_recommendations_dry_run_no_mutations():
    stub = _WlStub()
    result = alr.file_recommendations(RECS, PARENT, wl_runner=stub, dry_run=True,
                                      worklog_flags=[])
    assert result["dry_run"] is True
    assert result["comment_posted"] is False
    assert result["created"] == []
    # No mutating commands were issued.
    assert not any("create" in c for c in stub.commands)
    assert not any("comment" in c and "add" in c for c in stub.commands)
    # The planned actions are reported.
    assert sum(1 for a in result["actions"] if a.startswith("would create")) == 2


def test_file_recommendations_confidence_threshold_skips():
    recs = [dict(RECS[0], confidence=0.2), dict(RECS[1], confidence=0.95)]
    stub = _WlStub()
    result = alr.file_recommendations(recs, PARENT, wl_runner=stub,
                                      min_confidence=0.5, worklog_flags=[])
    titles = [c["title"] for c in result["created"]]
    assert titles == ["Add python3 import guard"]
    skipped = [s["title"] for s in result["skipped"]]
    assert "Warn about backticks in bash commands" in skipped


def test_file_recommendations_invalid_issue_type_defaults_to_chore():
    recs = [dict(RECS[0], issue_type="banana")]
    stub = _WlStub()
    result = alr.file_recommendations(recs, PARENT, wl_runner=stub, worklog_flags=[])
    cmd = next(c for c in stub.commands if "create" in c)
    assert cmd[cmd.index("--issue-type") + 1] == "chore"
    assert result["created"][0]["title"] == RECS[0]["title"]


def test_file_recommendations_empty_recs_no_create():
    stub = _WlStub()
    result = alr.file_recommendations([], PARENT, wl_runner=stub, worklog_flags=[])
    assert result["created"] == []
    assert not any("create" in c for c in stub.commands)
    # Still files a report comment (empty recommendation summary).
    assert result["comment_posted"] is True


def test_wl_flags_are_injected_before_subcommand():
    stub = _WlStub()
    alr.file_recommendations([RECS[0]], PARENT, wl_runner=stub,
                             worklog_flags=["--worklog-dir", "/main/.worklog"])
    for cmd in stub.commands:
        assert cmd[0] == "wl"
        assert cmd[1:3] == ["--worklog-dir", "/main/.worklog"]
        # subcommand follows the flags
        assert cmd[3] in ("show", "comment", "create")


def test_file_recommendations_create_failure_is_reported():
    """A failed wl create is recorded, not raised."""

    class FailingCreate(_WlStub):
        def __call__(self, cmd):
            if "create" in cmd:
                self.commands.append(list(cmd))
                return _fake_runner(json.dumps({"success": False, "error": "boom"}), rc=1)
            return super().__call__(cmd)

    stub = FailingCreate()
    result = alr.file_recommendations([RECS[0]], PARENT, wl_runner=stub, worklog_flags=[])
    assert result["created"] == []
    assert result["errors"]
    assert "boom" in result["errors"][0]["error"] or "rc=1" in result["errors"][0]["error"]


# ── CLI smoke tests (script entry point) ───────────────────────────────────

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "agent_log_review.py"


def test_cli_help_prints_usage():
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0
    for flag in ("--max-sessions", "--json", "--dry-run", "--parent-id",
                 "--model", "--errors-only"):
        assert flag in proc.stdout, f"missing {flag} in --help"


def test_cli_no_classify_end_to_end(tmp_path):
    import subprocess
    cwd = "/home/operator/project"
    sdir = tmp_path / alr.project_slug(cwd)
    _write_session(sdir, "a.jsonl", [
        _header(session_id="s1"),
        _assistant_msg([_tool_call_part("bash", {"cmd": "ls"})]),
        _tool_result_msg("bash", "command not found", is_error=True),
    ])
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--no-classify", "--json",
         "--cwd", cwd, "--session-root", str(tmp_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr[:400]
    report = json.loads(proc.stdout)
    assert report["summary"]["sessions_analysed"] == 1
    assert report["summary"]["classification_error"]


def test_cli_dry_run_does_not_mutate_worklog(tmp_path):
    import subprocess
    cwd = "/home/operator/project"
    sdir = tmp_path / alr.project_slug(cwd)
    _write_session(sdir, "a.jsonl", [_header(session_id="s1")])
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--no-classify", "--json", "--dry-run",
         "--parent-id", PARENT, "--cwd", cwd, "--session-root", str(tmp_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr[:400]
    # dry-run prints "would comment" to stderr, never an actual mutation.
    assert "would comment" in proc.stderr


# ── Prompt-budget regression (SA-0MVCAIMKL004HO8L) ─────────────────────────
# First real 20-session run hit: "classification prompt of 663931 bytes
# exceeds the 32768-byte budget". The per-session caps multiply; classify
# must trim the TOTAL payload (deterministic) instead of hard-raising.

def _large_signals_fixture() -> dict:
    sessions = [f"s{i:02d}.jsonl" for i in range(20)]
    hits = []
    errors = []
    for s in sessions:
        for kw in alr.THINKING_KEYWORDS:
            hits.append({"session": s, "keyword": kw, "count": 800,
                         "context": "hmm " + ("error " * 300)})
        for _ in range(25):
            errors.append({"session": s, "tool": "bash", "kind": "traceback",
                           "is_error": True,
                           "excerpt": "Traceback (most recent call last): " + "x" * 1500})
    return {
        "session_count": len(sessions),
        "session_files": sessions,
        "tool_distribution": {
            "total_calls": 5000, "total_error_results": 900,
            "by_tool": {"bash": {"calls": 4500, "error_results": 900, "error_rate": 0.2}},
        },
        "thinking_signals": {"total_hits": len(hits) * 2, "hits": hits},
        "tool_errors": {"total": len(errors), "errors": errors},
        "retry_patterns": {
            "total": 1,
            "patterns": [{"session": "s00.jsonl", "kind": "repeated_call",
                          "tool": "bash", "arguments": "x", "count": 3}],
        },
    }


def test_classify_large_signals_stays_under_budget():
    """A real-scale signals dict must not raise; the prompt fits the budget."""
    result = alr.classify_patterns(_large_signals_fixture(), runner=_Runner(stdout=WELL_FORMED_RESPONSE))
    assert result["error"] is None
    assert result["prompt_bytes"] <= alr.PROMPT_BUDGET_BYTES
    assert result["categories"][0]["name"] == "shell quoting issues"


def test_trim_signals_preserves_counts_and_session_names():
    trimmed = alr._trim_signals_for_prompt(_large_signals_fixture())
    assert trimmed["session_count"] == 20
    assert len(trimmed["session_files"]) == 20
    # Totals are recomputed from the retained rows (not wrong or stale).
    assert trimmed["thinking_signals"]["total_hits"] == sum(
        h["count"] for h in trimmed["thinking_signals"]["hits"]
    )
    assert trimmed["tool_errors"]["total"] == len(trimmed["tool_errors"]["errors"])
    # Retained rows still carry session names for evidence citations.
    assert all(h["session"] in [f"s{i:02d}.jsonl" for i in range(20)]
               for h in trimmed["thinking_signals"]["hits"])


def test_trim_signals_is_deterministic():
    a = alr._trim_signals_for_prompt(_large_signals_fixture())
    b = alr._trim_signals_for_prompt(_large_signals_fixture())
    assert a == b
    ra = alr.classify_patterns(_large_signals_fixture(), runner=_Runner(stdout=WELL_FORMED_RESPONSE))
    rb = alr.classify_patterns(_large_signals_fixture(), runner=_Runner(stdout=WELL_FORMED_RESPONSE))
    assert ra["prompt_bytes"] == rb["prompt_bytes"]


def test_trim_signals_does_not_mutate_input():
    fixture = _large_signals_fixture()
    before = json.dumps(fixture)
    alr._trim_signals_for_prompt(fixture)
    assert json.dumps(fixture) == before


def test_prompt_budget_overflow_still_raises_on_pathological_input():
    # Even trimmed, an absurd budget below the floor must still hard-fail.
    with pytest.raises(alr.ClassificationError):
        alr.classify_patterns(_large_signals_fixture(), runner=_Runner(stdout=WELL_FORMED_RESPONSE), prompt_budget=100)


# ── Impact-scope classification (SA-0MVCDZKN6005V5TB) ──────────────────────
# Recommendations now carry target_skill + impact_scope (global/local) +
# priority, carried through classification, rendering and filing.

SCOPED_RESPONSE = json.dumps({
    "categories": [{"name": "shell quoting issues", "confidence": 0.9,
                    "evidence_sessions": ["a.jsonl"], "summary": "Backticks."}],
    "recommendations": [
        {"title": "Add max re-ask guard to audit", "issue_type": "feature",
         "target_skill": "audit", "impact_scope": "global", "priority": "high",
         "rationale": "Repeated deep reruns cost.", "evidence_sessions": ["a.jsonl"]},
        {"title": "Local polish tweak", "issue_type": "chore",
         "target_skill": "this-repo", "impact_scope": "local", "priority": "low",
         "rationale": "Project-only.", "evidence_sessions": []},
    ],
})


def test_classify_parses_impact_scope_and_priority():
    result = alr.classify_patterns(
        _classification_fixture(), runner=_Runner(stdout=SCOPED_RESPONSE)
    )
    assert result["error"] is None
    recs = result["recommendations"]
    assert len(recs) == 2
    assert recs[0]["target_skill"] == "audit"
    assert recs[0]["impact_scope"] == "global"
    assert recs[0]["priority"] == "high"
    assert recs[1]["impact_scope"] == "local"
    assert recs[1]["priority"] == "low"


def test_normalise_recommendation_defaults_invalid_scope():
    rec = {"title": "x", "issue_type": "banana", "impact_scope": "sideways",
           "priority": "urgent"}
    norm = alr._normalise_recommendation(rec)
    assert norm["issue_type"] == "chore"
    assert norm["impact_scope"] == "unknown"
    assert norm["priority"] == "low"  # derived from chore


def test_normalise_recommendation_default_priority_by_issue_type():
    rec = {"title": "f", "issue_type": "feature"}
    assert alr._normalise_recommendation(rec)["priority"] == "medium"
    assert alr._normalise_recommendation({"title": "c", "issue_type": "chore"})["priority"] == "low"


def test_normalise_recommendation_tolerates_non_dict():
    norm = alr._normalise_recommendation(None)
    assert norm["title"] == ""
    assert norm["impact_scope"] == "unknown"
    norm2 = alr._normalise_recommendation("plain string")
    assert norm2["title"] == "plain string"


def test_report_renders_impact_scope():
    analysis = _analysis_fixture()
    analysis["classification"]["recommendations"] = [
        {"title": "Audit guard", "issue_type": "feature", "target_skill": "audit",
         "impact_scope": "global", "priority": "high",
         "rationale": "Costly reruns.", "evidence_sessions": ["a.jsonl"]},
    ]
    md = alr.render_report(analysis, fmt="markdown")
    assert "impact_scope: global" in md
    parsed = json.loads(alr.render_report(analysis, fmt="json"))
    assert parsed["recommendations"][0]["impact_scope"] == "global"


def test_filing_description_and_priority_carry_scope():
    stub = _WlStub()
    rec = dict(RECS[0], impact_scope="global", priority="high")
    result = alr.file_recommendations([rec], PARENT, wl_runner=stub, worklog_flags=[])
    assert result["created"][0]["title"] == rec["title"]
    cmd = next(c for c in stub.commands if "create" in c)
    joined = " ".join(cmd)
    assert "impact_scope: global" in joined
    assert cmd[cmd.index("--priority") + 1] == "high"
    assert "priority_hint: high" in joined
