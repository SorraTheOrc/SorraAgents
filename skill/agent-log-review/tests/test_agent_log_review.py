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
        "session_count", "tool_distribution", "thinking_signals",
        "tool_errors", "retry_patterns",
    }
    assert sig["tool_distribution"]["total_calls"] == 1
    assert sig["tool_errors"]["total"] == 1
    assert sig["thinking_signals"]["total_hits"] >= 2

