import os
import tempfile
from dataclasses import dataclass

import pytest

import json
from skill.ralph.scripts.ralph_loop import RalphError, RalphLoop, _comment_hash, parse_audit_report


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.audit_outputs: list[str] = []
        self.items = {
            "SA-TARGET": {"id": "SA-TARGET", "stage": "plan_complete", "status": "open"},
            "SA-CHILD": {"id": "SA-CHILD", "stage": "in_review", "status": "in-progress"},
        }
        self.comments: list[dict] = []

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)

        if cmd[:3] == ["wl", "show", "SA-TARGET"] and "--children" in cmd:
            return Result(stdout='{"success":true,"workItem":{"id":"SA-TARGET","stage":"plan_complete","status":"open"},"children":[{"id":"SA-CHILD"}]}')
        if cmd[:3] == ["wl", "show", "SA-TARGET"]:
            item = self.items["SA-TARGET"]
            return Result(stdout=f'{{"success":true,"workItem":{item}}}'.replace("'", '"'))
        if cmd[:3] == ["wl", "show", "SA-CHILD"]:
            item = self.items["SA-CHILD"]
            return Result(stdout=f'{{"success":true,"workItem":{item}}}'.replace("'", '"'))

        if cmd[:4] == ["wl", "comment", "list", "SA-TARGET"]:
            return Result(stdout=f'{{"success":true,"comments":{self.comments}}}'.replace("'", '"'))
        if cmd[:4] == ["wl", "comment", "add", "SA-TARGET"]:
            comment = cmd[cmd.index("--comment") + 1]
            self.comments.append({"comment": comment})
            return Result(stdout='{"success":true}')

        if cmd[:3] == ["wl", "update", "SA-TARGET"]:
            return Result(stdout='{"success":true}')

        if cmd[0] == "python3" and len(cmd) > 1 and "orchestrate_estimate.py" in cmd[1]:
            # Return pre-configured effort_and_risk output if provided
            out = getattr(self, "effort_outputs", None)
            if out:
                val = out.pop(0)
                return Result(stdout=val)
            # default to small/low
            return Result(stdout=json.dumps({"effort": {"tshirt": "Small"}, "risk": {"level": "Low"}}))

        if cmd[0] == "pi" and "-p" in cmd:
            # pi -p --mode json --model <model> <prompt>
            # Find the prompt: it's the last positional arg
            prompt = cmd[-1]
            if prompt.startswith("/audit"):
                output = self.audit_outputs.pop(0)
                return Result(stdout=output)
            if prompt.startswith("implement"):
                # emulate implementation moving target to in_review after first implement call
                self.items["SA-TARGET"]["stage"] = "in_review"
                return Result(stdout="implemented")

        if cmd[:2] == ["bash", "-lc"]:
            return Result(stdout="ok")

        if cmd and cmd[0] == "git":
            return Result(stdout="ok")

        raise AssertionError(f"Unexpected command: {cmd}")


AUDIT_FAIL = """Ready to close: No

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | stage gate | met | file:1 |
| 2 | retry loop | unmet | file:2 |
"""

AUDIT_PASS = """Ready to close: Yes

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | stage gate | met | file:1 |
| 2 | retry loop | met | file:2 |
"""


def test_parse_audit_report_extracts_readiness_and_rows():
    parsed = parse_audit_report(AUDIT_FAIL)
    assert parsed.ready_to_close is False
    assert len(parsed.criteria) == 2
    assert parsed.unmet_or_partial[0].text == "retry loop"


def test_precondition_requires_plan_complete_or_in_review():
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "in_progress"
    loop = RalphLoop(runner=runner, stream=False)

    with pytest.raises(RalphError, match="plan_complete or in_review"):
        loop.run("SA-TARGET")


def test_happy_path_success_with_merge_offer_not_executed_without_confirm():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, check_cmds=["pytest -q"], max_attempts=2, confirm_merge=False)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    assert result["merge_offered"] is True
    assert result["merge_executed"] is False
    assert not any(call[:2] == ["git", "push"] for call in runner.calls)


def test_retry_path_uses_remediation_in_next_implement_prompt():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_FAIL, AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, max_attempts=3)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    implement_prompts = [c[-1] for c in runner.calls if c[0] == "pi" and "-p" in c and c[-1].startswith("implement")]
    assert len(implement_prompts) == 2
    assert "Address all the gaps identified in the audit" in implement_prompts[1]


def test_autoplan_skips_plan_for_small_low():
    """When stage is intake_complete and effort/risk are Small/Low, do not invoke /plan and proceed to implement."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    # effort_and_risk returns Small / Low
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Small"}, "risk": {"level": "Low"}})]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # ensure no plan invocation (opencode run /plan)
    assert not any(c[0] == "opencode" and "/plan" in " ".join(c) for c in runner.calls)
    # ensure implement was invoked (pi with implement prompt)
    assert any(c[0] == "pi" and c[-1].startswith("implement") for c in runner.calls)


def test_autoplan_invokes_plan_for_medium_high():
    """When stage is intake_complete and effort/risk are Medium/High, invoke /plan then implement."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    # effort_and_risk returns Medium / High
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Medium"}, "risk": {"level": "High"}})]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # ensure plan invocation occurred
    assert any(c[0] == "opencode" and "/plan" in " ".join(c) for c in runner.calls)
    # ensure implement was invoked after plan
    plan_indices = [i for i,c in enumerate(runner.calls) if c[0] == "opencode" and "/plan" in " ".join(c)]
    impl_indices = [i for i,c in enumerate(runner.calls) if c[0] == "pi" and c[-1].startswith("implement")]
    assert plan_indices and impl_indices and min(impl_indices) > min(plan_indices)


def test_cancel_file_stops_loop():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    with tempfile.TemporaryDirectory() as td:
        cancel_file = os.path.join(td, "cancel")
        open(cancel_file, "w", encoding="utf-8").close()
        loop = RalphLoop(runner=runner, stream=False, cancel_file=cancel_file)
        result = loop.run("SA-TARGET")

    assert result["status"] == "cancelled"


def test_max_attempts_returns_max_attempts_status():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_FAIL, AUDIT_FAIL]
    loop = RalphLoop(runner=runner, stream=False, max_attempts=2)

    result = loop.run("SA-TARGET")

    assert result["status"] == "max_attempts"


def test_confirm_merge_executes_git_steps():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, confirm_merge=True)

    result = loop.run("SA-TARGET")

    assert result["merge_executed"] is True
    assert any(call[:3] == ["git", "fetch", "origin"] for call in runner.calls)
    assert any(call[:2] == ["git", "push"] for call in runner.calls)


def test_idempotent_audit_comment_append_skips_duplicate_hash():
    runner = FakeRunner()
    digest = _comment_hash(AUDIT_PASS)
    runner.comments = [{"comment": f"# AMPA Audit Result\naudit-hash:{digest}\n\n..."}]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    loop.run("SA-TARGET")

    add_calls = [c for c in runner.calls if c[:4] == ["wl", "comment", "add", "SA-TARGET"]]
    assert add_calls == []


def test_changed_audit_appends_new_comment_not_duplicate():
    """When audit content changes, a new comment is appended (not skipped)."""
    runner = FakeRunner()
    # Existing comment has hash of AUDIT_FAIL
    old_digest = _comment_hash(AUDIT_FAIL)
    runner.comments = [{"comment": f"# AMPA Audit Result\naudit-hash:{old_digest}\n\nold content"}]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    loop.run("SA-TARGET")

    # Should add a new comment because the hash differs
    add_calls = [c for c in runner.calls if c[:4] == ["wl", "comment", "add", "SA-TARGET"]]
    assert len(add_calls) == 1
    new_digest = _comment_hash(AUDIT_PASS)
    assert f"audit-hash:{new_digest}" in add_calls[0][add_calls[0].index("--comment") + 1]


AUDIT_PARTIAL = """Ready to close: No

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | stage gate | met | file:1 |
| 2 | retry loop | partial | file:2 |
"""


def test_partial_verdict_counts_as_unmet():
    parsed = parse_audit_report(AUDIT_PARTIAL)
    assert len(parsed.unmet_or_partial) == 1
    assert parsed.unmet_or_partial[0].verdict == "partial"
    assert parsed.unmet_or_partial[0].text == "retry loop"


def test_empty_audit_report_is_handled_gracefully():
    parsed = parse_audit_report("")
    assert parsed.ready_to_close is False
    assert parsed.criteria == []
    assert parsed.unmet_or_partial == []


def test_malformed_audit_table_rows_are_skipped():
    text = """Ready to close: Yes

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | stage gate | met | file:1 |
| not | a | proper | row |
"""
    parsed = parse_audit_report(text)
    assert len(parsed.criteria) == 1


def test_cli_parser_missing_id_exits_with_error():
    from skill.ralph.scripts.ralph_loop import main

    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


def test_cli_parser_accepts_all_flags():
    from skill.ralph.scripts.ralph_loop import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "SA-X1", "--max-attempts", "5",
        "--check-cmd", "pytest",
        "--confirm-merge",
        "--cancel-file", "/tmp/cancel",
        "--quiet",
        "--pi-bin", "/usr/local/bin/pi",
        "--wl-bin", "/usr/local/bin/wl",
    ])
    assert args.work_item_id == "SA-X1"
    assert args.max_attempts == 5
    assert args.check_cmd == ["pytest"]
    assert args.confirm_merge is True
    assert args.cancel_file == "/tmp/cancel"
    assert args.quiet is True
    assert args.pi_bin == "/usr/local/bin/pi"
    assert args.wl_bin == "/usr/local/bin/wl"
    assert args.verbose is False


def test_cli_parser_verbose_flag():
    from skill.ralph.scripts.ralph_loop import build_parser

    parser = build_parser()
    args = parser.parse_args(["SA-X2", "--verbose"])
    assert args.verbose is True


def test_main_returns_error_on_precondition_failure():
    from skill.ralph.scripts.ralph_loop import main

    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "idea"
    loop = RalphLoop(runner=runner, stream=False)

    # Direct API call gives RalphError
    with pytest.raises(RalphError, match="plan_complete or in_review"):
        loop.run("SA-TARGET")


class FakeRunnerWithPushFailure(FakeRunner):
    def __call__(self, cmd):
        if cmd and cmd[:2] == ["git", "push"]:
            return Result(returncode=1, stderr="remote: Permission denied")
        return super().__call__(cmd)


def test_merge_permission_failure_raises_ralph_error():
    runner = FakeRunnerWithPushFailure()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, confirm_merge=True)

    with pytest.raises(RalphError, match="Merge step failed"):
        loop.run("SA-TARGET")


class FakeRunnerWithCheckFailure(FakeRunner):
    def __call__(self, cmd):
        if cmd and cmd[:2] == ["bash", "-lc"] and "pytest" in cmd[2]:
            return Result(returncode=1, stderr="1 test failed")
        return super().__call__(cmd)


def test_check_cmd_failure_raises_ralph_error():
    runner = FakeRunnerWithCheckFailure()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, check_cmds=["pytest -q"])

    with pytest.raises(RalphError, match="Check failed"):
        loop.run("SA-TARGET")


def test_audit_text_written_via_wl_update():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    loop.run("SA-TARGET")

    update_calls = [c for c in runner.calls if c[:3] == ["wl", "update", "SA-TARGET"]]
    assert len(update_calls) >= 1
    audit_text_idx = update_calls[0].index("--audit-text")
    assert update_calls[0][audit_text_idx + 1] == AUDIT_PASS


def test_verbose_mode_logs_pi_output_start():
    """Verbose=True causes DEBUG-level logs for pi run stdout."""
    import logging

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("ralph")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)

    try:
        runner = FakeRunner()
        runner.audit_outputs = [AUDIT_PASS]
        loop = RalphLoop(runner=runner, stream=False, verbose=True)
        loop.run("SA-TARGET")

        debug_msgs = [r for r in records if r.levelno == logging.DEBUG]
        pi_debug_msgs = [r for r in debug_msgs if "ralph.cmd.pi.run" in r.getMessage()]
        # Should have at least implement and audit pi.run debug logs
        assert len(pi_debug_msgs) >= 2
        # Verbose mode logs the full prompt (not truncated)
        prompt_msgs = [r for r in pi_debug_msgs if "prompt_full" in r.getMessage()]
        assert len(prompt_msgs) >= 1
        # The prompt should contain the implement instruction in full
        assert "Continue until the work item and all dependencies are completed, but do not merge." in prompt_msgs[0].getMessage()
        # text_start should be logged (the extracted text content)
        text_msgs = [r for r in pi_debug_msgs if "text_start" in r.getMessage()]
        assert len(text_msgs) >= 1
    finally:
        logger.setLevel(old_level)
        logger.removeHandler(handler)


def test_stream_pi_captures_and_returns_output():
    """When stream=True, _stream_pi parses JSON and streams only text_delta content."""
    import subprocess
    from unittest.mock import patch, MagicMock

    # Simulate a pi subprocess producing pi JSON protocol events
    fake_process = MagicMock()
    fake_process.returncode = 0
    fake_process.stdout = iter([
        '{"type":"agent_start"}\n',
        '{"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","delta":"thinking"}}\n',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":"Hello "}}\n',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":"World"}}\n',
        '{"type":"agent_end"}\n',
    ])
    fake_process.stderr = MagicMock()
    fake_process.stderr.read.return_value = ""
    fake_process.wait.return_value = None

    with patch("skill.ralph.scripts.ralph_loop.subprocess.Popen", return_value=fake_process):
        loop = RalphLoop(verbose=False, stream=True)
        loop.pi_bin = "echo"
        result = loop._stream_pi(["echo", "test"], "test prompt")

    # Only text_delta content is returned — thinking and metadata are suppressed
    assert result == "Hello World"


def test_stream_pi_verbose_logs_raw_json(capsys):
    """When verbose=True, raw JSON lines are logged at DEBUG level."""
    import logging
    from unittest.mock import patch, MagicMock

    fake_process = MagicMock()
    fake_process.returncode = 0
    fake_process.stdout = iter([
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":"Hello"}}\n',
        '{"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","delta":"internal"}}\n',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":" World"}}\n',
    ])
    fake_process.stderr = MagicMock()
    fake_process.stderr.read.return_value = ""
    fake_process.wait.return_value = None

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("ralph")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)

    try:
        with patch("skill.ralph.scripts.ralph_loop.subprocess.Popen", return_value=fake_process):
            loop = RalphLoop(verbose=True, stream=True)
            loop.pi_bin = "echo"
            result = loop._stream_pi(["echo", "test"], "test prompt")

        assert result == "Hello World"
        # Verbose mode should log raw JSON lines
        json_logs = [r for r in records if r.levelno == logging.DEBUG and "json_line" in r.getMessage()]
        assert len(json_logs) >= 1  # all JSON lines logged at DEBUG
        # Console should show only text_delta content — thinking suppressed
        captured = capsys.readouterr()
        assert "Hello" in captured.out
        assert "World" in captured.out
        assert "internal" not in captured.out  # thinking not shown
        assert "thinking_delta" not in captured.out
    finally:
        logger.setLevel(old_level)
        logger.removeHandler(handler)


def test_in_review_skips_first_implement():
    """When target is already in_review, ralph skips the first implement and audits directly."""
    runner = FakeRunner()
    # Target starts in_review (not plan_complete)
    runner.items["SA-TARGET"]["stage"] = "in_review"
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, max_attempts=3)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    # There should be NO implement calls, only an audit call
    pi_calls = [c for c in runner.calls if c[0] == "pi" and "-p" in c]
    implement_calls = [c for c in pi_calls if c[-1].startswith("implement")]
    audit_calls = [c for c in pi_calls if c[-1].startswith("/audit")]
    assert len(implement_calls) == 0, f"Expected no implement calls, got {implement_calls}"
    assert len(audit_calls) == 1


def test_in_review_implement_after_failed_audit():
    """When target is in_review but audit fails, ralph implements then re-audits."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "in_review"
    runner.audit_outputs = [AUDIT_FAIL, AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, max_attempts=3)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    assert result["attempt"] == 2
    # First attempt: audit only (skip implement)
    # Second attempt: implement + audit
    pi_calls = [c for c in runner.calls if c[0] == "pi" and "-p" in c]
    first_call = pi_calls[0][-1]
    assert first_call.startswith("/audit"), f"First call should be audit, got: {first_call[:60]}"
    second_call = pi_calls[1][-1]
    assert second_call.startswith("implement"), f"Second call should be implement, got: {second_call[:60]}"
    third_call = pi_calls[2][-1]
    assert third_call.startswith("/audit"), f"Third call should be audit, got: {third_call[:60]}"


def test_model_passed_to_pi_commands():
    """The --model flag is passed through to pi commands."""
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, model="opencode-go/glm-5.1")
    loop.run("SA-TARGET")

    pi_calls = [c for c in runner.calls if c[0] == "pi" and "-p" in c]
    assert len(pi_calls) >= 1
    # Command should include --model <model_value> and --mode json
    for call in pi_calls:
        assert "--model" in call, f"Expected --model in pi call: {call[:6]}"
        model_idx = call.index("--model")
        assert call[model_idx + 1] == "opencode-go/glm-5.1"
        assert "--mode" in call, f"Expected --mode in pi call: {call[:6]}"
        mode_idx = call.index("--mode")
        assert call[mode_idx + 1] == "json"


def test_default_model_is_used_when_none_specified():
    """When no model is specified, the default model is used."""
    from skill.ralph.scripts.ralph_loop import DEFAULT_MODEL
    loop = RalphLoop(runner=FakeRunner(), stream=False)
    assert loop.model == DEFAULT_MODEL
    assert DEFAULT_MODEL == "opencode-go/glm-5.1"


def test_config_file_model_resolved():
    """Config file model is used when CLI model is not specified."""
    from skill.ralph.scripts.ralph_loop import _resolve_model
    # CLI takes precedence
    assert _resolve_model("cli-model", "config-model") == "cli-model"
    # Config file is used when CLI is None
    assert _resolve_model(None, "config-model") == "config-model"
    # Default is used when both are None
    assert _resolve_model(None, None) == "opencode-go/glm-5.1"


def test_load_config_from_json_file(tmp_path):
    """Config is loaded from .ralph.json in the current directory."""
    from skill.ralph.scripts.ralph_loop import _load_config
    import json

    config_file = tmp_path / ".ralph.json"
    config_file.write_text(json.dumps({"model": "test-model", "max_attempts": 5}))

    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        config = _load_config()
        assert config["model"] == "test-model"
        assert config["max_attempts"] == 5
    finally:
        os.chdir(original_cwd)


def test_parse_pi_json_line_thinking_suppressed():
    """Thinking events are suppressed — only text_delta content is shown."""
    from skill.ralph.scripts.ralph_loop import _parse_pi_json_line

    # thinking_delta: suppressed (not user-facing)
    stream_text, should_print, complete_text = _parse_pi_json_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","delta":"The user is asking me"}}'
    )
    assert stream_text == "" and should_print is False

    # thinking_start: suppressed
    stream_text, should_print, complete_text = _parse_pi_json_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"thinking_start","contentIndex":0}}'
    )
    assert stream_text == "" and should_print is False

    # thinking_end: suppressed
    stream_text, should_print, complete_text = _parse_pi_json_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"thinking_end","contentIndex":0}}'
    )
    assert stream_text == "" and should_print is False


def test_parse_pi_json_line_text_delta_shown():
    """text_delta events are shown — they contain user-facing text."""
    from skill.ralph.scripts.ralph_loop import _parse_pi_json_line

    # text_delta: additive, user-facing — should be printed
    stream_text, should_print, complete_text = _parse_pi_json_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":"hello"}}'
    )
    assert stream_text == "hello"
    assert should_print is True

    # text_delta with longer content
    stream_text, should_print, complete_text = _parse_pi_json_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":" world"}}'
    )
    assert stream_text == " world"
    assert should_print is True

    # text_delta with empty string: suppress
    stream_text, should_print, complete_text = _parse_pi_json_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":""}}'
    )
    assert stream_text == "" and should_print is False


def test_parse_pi_json_line_metadata_suppressed():
    """Structural metadata events are suppressed."""
    from skill.ralph.scripts.ralph_loop import _parse_pi_json_line

    for event_json in [
        '{"type":"session","version":3}',
        '{"type":"agent_start"}',
        '{"type":"agent_end"}',
        '{"type":"turn_start"}',
        '{"type":"turn_end"}',
        '{"type":"message_start","message":{"role":"user"}}',
        '{"type":"message_end","message":{"role":"user"}}',
    ]:
        stream_text, should_print, complete_text = _parse_pi_json_line(event_json)
        assert stream_text == "" and should_print is False, f"Failed for: {event_json[:60]}"


def test_parse_pi_json_line_text_start_end():
    """text_start is suppressed; text_end returns complete content for return value."""
    from skill.ralph.scripts.ralph_loop import _parse_pi_json_line

    # text_start: structural — suppressed
    stream_text, should_print, complete_text = _parse_pi_json_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_start","contentIndex":1}}'
    )
    assert stream_text == "" and should_print is False
    assert complete_text is None

    # text_end: returns complete content block for return value
    stream_text, should_print, complete_text = _parse_pi_json_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_end","contentIndex":1,"content":"Hello World"}}'
    )
    assert stream_text == "" and should_print is False
    assert complete_text == "Hello World"


def test_parse_pi_json_line_fallback_for_unknown_json():
    """Unknown JSON types fall back to extracting text from content/text/delta fields."""
    from skill.ralph.scripts.ralph_loop import _parse_pi_json_line

    # Fallback: unknown type with content field
    stream_text, should_print, complete_text = _parse_pi_json_line('{"type":"unknown","content":"hello"}')
    assert stream_text == "hello" and should_print is True

    # Fallback: unknown type with no text → suppressed
    stream_text, should_print, complete_text = _parse_pi_json_line('{"type":"unknown","id":"x"}')
    assert stream_text == "" and should_print is False

    # Non-JSON line → (None, False, None) for fallback
    stream_text, should_print, complete_text = _parse_pi_json_line("plain text line")
    assert stream_text is None and should_print is False and complete_text is None


def test_extract_text_from_json_output():
    """_extract_text_from_json_output prefers text_end/agent_end over deltas."""
    from skill.ralph.scripts.ralph_loop import _extract_text_from_json_output

    # Simulate pi JSON output with text_delta events (no text_end or agent_end)
    json_lines = '\n'.join([
        '{"type":"session","version":3}',
        '{"type":"agent_start"}',
        '{"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","delta":"internal thinking"}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":"Hello "}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":"World"}}',
    ])
    # Only text_delta content is extracted, thinking and metadata are suppressed
    assert _extract_text_from_json_output(json_lines) == "Hello World"

    # With text_end, the complete content replaces deltas
    json_lines_with_end = '\n'.join([
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":"Hello"}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":" World"}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_end","contentIndex":1,"content":"Hello World"}}',
    ])
    # text_end provides complete content, preferred over deltas
    assert _extract_text_from_json_output(json_lines_with_end) == "Hello World"

    # With multiple text_end blocks, only the LAST one is used (the final response)
    json_lines_multi = '\n'.join([
        '{"type":"message_update","assistantMessageEvent":{"type":"text_end","contentIndex":1,"content":"I will audit this item."}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_end","contentIndex":1,"content":"Ready to close: No\\n\\n| # | Criterion | Verdict | Evidence |\\n|---|-----------|---------|----------|\\n| 1 | Tests | unmet | No tests |"}}',
    ])
    result = _extract_text_from_json_output(json_lines_multi)
    # Should return only the LAST text_end block (the audit report), not both
    assert "Ready to close: No" in result
    assert "I will audit this item" not in result

    # With agent_end, the final message text is used (most authoritative)
    json_lines_with_final = '\n'.join([
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":1,"delta":"Partial"}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_end","contentIndex":1,"content":"Partial text from single block"}}',
        '{"type":"agent_end","messages":[{"role":"user","content":[{"type":"text","text":"ignored"}]},{"role":"assistant","content":[{"type":"thinking","thinking":"thought"},{"type":"text","text":"Final audit report text"}]}]}',
    ])
    result = _extract_text_from_json_output(json_lines_with_final)
    # agent_end returns the LAST assistant message text
    assert result == "Final audit report text"

    # Plain text passthrough
    plain = "Just a plain text response"
    assert _extract_text_from_json_output(plain) == plain

    # Empty input
    assert _extract_text_from_json_output("") == ""


def test_wl_update_audit_uses_file_for_large_text():
    """When audit text exceeds _MAX_ARG_LEN, ralph uses --audit-file with a temp file."""
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)
    # Create audit text larger than _MAX_ARG_LEN
    large_audit = "Ready to close: Yes\n" + "x" * (RalphLoop._MAX_ARG_LEN + 1)
    loop._wl_update_audit("SA-TARGET", large_audit)
    # The runner should have been called with --audit-file
    calls = [c for c in runner.calls if c[0] == "wl" and "update" in c]
    assert len(calls) == 1
    cmd = calls[0]
    assert "--audit-file" in cmd
    # The temp file should have been cleaned up (no lingering files)


def test_wl_update_audit_uses_inline_for_small_text():
    """When audit text is small, ralph uses --audit-text inline."""
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)
    loop._wl_update_audit("SA-TARGET", "Ready to close: Yes")
    calls = [c for c in runner.calls if c[0] == "wl" and "update" in c]
    assert len(calls) == 1
    cmd = calls[0]
    assert "--audit-text" in cmd
    assert "--audit-file" not in cmd


def test_wl_comment_add_truncates_large_comment():
    """When a comment exceeds _MAX_ARG_LEN, it is truncated with a note."""
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)
    large_comment = "x" * (RalphLoop._MAX_ARG_LEN + 1000)
    loop._wl_comment_add("SA-TARGET", large_comment)
    calls = [c for c in runner.calls if c[0] == "wl" and "comment" in c]
    assert len(calls) == 1
    cmd = calls[0]
    # Find the comment argument (after --comment)
    comment_idx = cmd.index("--comment")
    comment_text = cmd[comment_idx + 1]
    assert len(comment_text) < RalphLoop._MAX_ARG_LEN + 200  # includes truncation note
    assert "truncated" in comment_text
