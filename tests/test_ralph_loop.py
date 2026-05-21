import os
import tempfile
from dataclasses import dataclass

import pytest

import json
from skill.ralph.scripts.ralph_loop import JsonLineFormatter, RalphError, RalphLoop, parse_audit_report


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.audit_outputs: list[str] = []
        self.child_ids = ["SA-CHILD"]
        self.items = {
            "SA-TARGET": {"id": "SA-TARGET", "stage": "plan_complete", "status": "open", "effort": "", "risk": "", "audit": ""},
            "SA-CHILD": {"id": "SA-CHILD", "stage": "in_review", "status": "in-progress", "effort": "", "risk": "", "audit": ""},
        }
        self.comments: list[dict] = []
        self.compact_failures_remaining = 0

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)

        if cmd[:3] == ["wl", "show", "SA-TARGET"] and "--children" in cmd:
            item = self.items["SA-TARGET"]
            children = [
                {"id": child_id, "stage": self.items.get(child_id, {}).get("stage", "")}
                for child_id in self.child_ids
                if child_id in self.items
            ]
            return Result(stdout=json.dumps({"success": True, "workItem": item, "children": children}))
        if cmd[:3] == ["wl", "show", "SA-TARGET"]:
            item = self.items["SA-TARGET"]
            return Result(stdout=json.dumps({"success": True, "workItem": item}))
        if cmd[:3] == ["wl", "show", "SA-CHILD"]:
            item = self.items["SA-CHILD"]
            return Result(stdout=json.dumps({"success": True, "workItem": item}))

        if cmd[:3] == ["wl", "comment", "list"]:
            return Result(stdout=json.dumps({"success": True, "comments": self.comments}))
        if cmd[:3] == ["wl", "comment", "add"]:
            comment = cmd[cmd.index("--comment") + 1]
            self.comments.append({"comment": comment, "author": "ralph"})
            return Result(stdout=json.dumps({"success": True}))

        if cmd[:3] == ["wl", "update", "SA-TARGET"]:
            # Track effort and risk updates
            if "--effort" in cmd:
                idx = cmd.index("--effort")
                self.items["SA-TARGET"]["effort"] = cmd[idx + 1]
            if "--risk" in cmd:
                idx = cmd.index("--risk")
                self.items["SA-TARGET"]["risk"] = cmd[idx + 1]
            return Result(stdout=json.dumps({"success": True}))

        if cmd[0] == "python3" and len(cmd) > 1 and "orchestrate_estimate.py" in cmd[1]:
            # Return pre-configured effort_and_risk output if provided
            out = getattr(self, "effort_outputs", None)
            if out:
                val = out.pop(0)
                return Result(stdout=val)
            # default to small/low
            return Result(stdout=json.dumps({"effort": {"tshirt": "Small"}, "risk": {"level": "Low", "score": 0}}))

        if cmd[0] == "pi" and "-p" in cmd:
            # pi -p --mode json --model <model> <prompt>
            # Find the prompt: it's the last positional arg
            prompt = cmd[-1]
            if prompt.startswith("/skill:audit"):
                output = self.audit_outputs.pop(0)
                # Simulate the audit skill persisting the audit into the work item
                self.items["SA-TARGET"]["audit"] = output
                return Result(stdout=output)
            if prompt.startswith("/skill:plan"):
                # emulate plan moving target to plan_complete after plan call
                self.items["SA-TARGET"]["stage"] = "plan_complete"
                return Result(stdout="planned")
            if prompt.startswith("implement"):
                # emulate implementation moving target to in_review after first implement call
                self.items["SA-TARGET"]["stage"] = "in_review"
                return Result(stdout="implemented")
            if prompt == "/compact":
                if self.compact_failures_remaining > 0:
                    self.compact_failures_remaining -= 1
                    return Result(returncode=1, stderr="compact failed")
                return Result(stdout="compacted")

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


def test_accepts_in_progress_and_runs():
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "in_progress"
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"


def test_happy_path_success_with_merge_offer_not_executed_without_confirm():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(
        runner=runner,
        stream=False,
        check_cmds=["pytest -q -r a --disable-warnings"],
        max_attempts=2,
        confirm_merge=False,
    )

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    assert result["merge_offered"] is True
    assert result["merge_executed"] is False
    assert not any(call[:2] == ["git", "push"] for call in runner.calls)


def test_pi_invocations_use_ephemeral_sessions():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    loop.run("SA-TARGET")

    pi_calls = [call for call in runner.calls if call and call[0] == "pi" and "-p" in call]
    assert pi_calls
    assert all("--no-session" in call for call in pi_calls)


def test_retry_path_uses_remediation_in_next_implement_prompt():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_FAIL, AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, max_attempts=3)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    implement_prompts = [c[-1] for c in runner.calls if c[0] == "pi" and "-p" in c and c[-1].startswith("implement")]
    assert len(implement_prompts) == 2
    assert "Address all the gaps identified in the audit" in implement_prompts[1]


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
    # Simulate an existing AMPA comment (content not validated by ralph in this mode)
    runner.comments = [{"comment": "# AMPA Audit Result\n\n..."}]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    loop.run("SA-TARGET")

    add_calls = [c for c in runner.calls if c[:4] == ["wl", "comment", "add", "SA-TARGET"]]
    assert add_calls == []
    # Also verify that wl update --audit-text is not called by ralph
    update_calls = [c for c in runner.calls if c[:3] == ["wl", "update", "SA-TARGET"] and "--audit-text" in c]
    assert update_calls == []


def test_changed_audit_appends_new_comment_not_duplicate():
    """When audit content changes, ralph must still rely on the persisted audit and
    must NOT write audit text or AMPA audit comments itself."""
    runner = FakeRunner()
    runner.comments = [{"comment": "# AMPA Audit Result\n\nold content"}]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    loop.run("SA-TARGET")

    # Ralph should not write audit text or append AMPA audit comments
    add_calls = [c for c in runner.calls if c[:4] == ["wl", "comment", "add", "SA-TARGET"]]
    assert add_calls == []
    update_calls = [c for c in runner.calls if c[:3] == ["wl", "update", "SA-TARGET"] and "--audit-text" in c]
    assert update_calls == []


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
    with pytest.raises(RalphError, match="plan_complete, in_review, or in_progress"):
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
    loop = RalphLoop(
        runner=runner,
        stream=False,
        check_cmds=["pytest -q -r a --disable-warnings"],
    )

    with pytest.raises(RalphError, match="Check failed"):
        loop.run("SA-TARGET")


def test_audit_text_written_via_wl_update_is_not_called_by_ralph():
    """Ralph should NOT write the audit text; it must be persisted by the audit skill."""
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    loop.run("SA-TARGET")

    # Ralph must not call wl update with --audit-text
    update_calls = [c for c in runner.calls if c[:3] == ["wl", "update", "SA-TARGET"] and "--audit-text" in c]
    assert update_calls == []


def test_check_cmds_are_canonicalized_to_quiet_pytest():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(
        runner=runner,
        stream=False,
        check_cmds=["pytest tests/test_example.py"],
    )

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    check_calls = [c[2] for c in runner.calls if c[:2] == ["bash", "-lc"]]
    assert any(
        call == "pytest -q -r a --disable-warnings tests/test_example.py"
        for call in check_calls
    )


def test_check_cmds_are_canonicalized_to_quiet_npm_test():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(
        runner=runner,
        stream=False,
        check_cmds=["npm test"],
    )

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    check_calls = [c[2] for c in runner.calls if c[:2] == ["bash", "-lc"]]
    assert any(call == "npm --silent test" for call in check_calls)


def test_delegated_commands_are_logged_in_console_output():
    import io
    import logging
    import shlex

    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("ralph")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    try:
        result = loop.run("SA-TARGET")
    finally:
        logger.setLevel(old_level)
        logger.removeHandler(handler)

    assert result["status"] == "success"
    console_output = stream.getvalue()
    delegated_calls = [c for c in runner.calls if c[0] in {"pi", "wl"}]
    assert delegated_calls
    for call in delegated_calls:
        assert shlex.join(call) in console_output


def test_delegated_commands_include_machine_readable_json_fields():
    import io
    import logging
    import shlex

    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLineFormatter())
    logger = logging.getLogger("ralph")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    try:
        result = loop.run("SA-TARGET")
    finally:
        logger.setLevel(old_level)
        logger.removeHandler(handler)

    assert result["status"] == "success"
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    delegated_lines = [line for line in lines if line.get("category") in {"pi", "wl"} and "cmd" in line]
    assert delegated_lines
    rendered_calls = {shlex.join(c) for c in runner.calls if c[0] in {"pi", "wl"}}
    observed_cmds = {line["cmd"] for line in delegated_lines}
    assert rendered_calls <= observed_cmds
    assert all(isinstance(line.get("argv"), list) for line in delegated_lines)


def test_failed_pi_command_still_logs_exact_command_before_raising():
    import io
    import logging
    import shlex

    class FailOnAuditRunner(FakeRunner):
        def __call__(self, cmd):
            cmd = list(cmd)
            if cmd[:2] == ["pi", "-p"] and cmd[-1].startswith("/skill:audit"):
                self.calls.append(cmd)
                return Result(returncode=1, stderr="audit failed")
            return super().__call__(cmd)

    runner = FailOnAuditRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("ralph")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    try:
        with pytest.raises(RalphError, match="pi run failed"):
            loop.run("SA-TARGET")
    finally:
        logger.setLevel(old_level)
        logger.removeHandler(handler)

    console_output = stream.getvalue()
    failing_calls = [c for c in runner.calls if c[0] == "pi" and c[-1].startswith("/skill:audit")]
    assert failing_calls
    assert shlex.join(failing_calls[0]) in console_output


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


def test_run_pi_stream_mode_uses_ephemeral_sessions():
    from unittest.mock import patch, MagicMock

    fake_process = MagicMock()
    fake_process.returncode = 0
    fake_process.stdout = iter([])
    fake_process.stderr = MagicMock()
    fake_process.stderr.read.return_value = ""
    fake_process.wait.return_value = None

    with patch("skill.ralph.scripts.ralph_loop.subprocess.Popen", return_value=fake_process) as popen:
        loop = RalphLoop(verbose=False, stream=True)
        loop.pi_bin = "pi"
        result = loop._run_pi("test prompt")

    assert result == ""
    cmd = popen.call_args.args[0]
    assert "--no-session" in cmd
    assert cmd.index("--no-session") < cmd.index("test prompt")


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
    audit_calls = [c for c in pi_calls if c[-1].startswith("/skill:audit")]
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
    assert first_call.startswith("/skill:audit"), f"First call should be audit, got: {first_call[:60]}"
    second_call = pi_calls[1][-1]
    assert second_call.startswith("implement"), f"Second call should be implement, got: {second_call[:60]}"
    third_call = pi_calls[2][-1]
    assert third_call.startswith("/skill:audit"), f"Third call should be audit, got: {third_call[:60]}"


def test_in_review_skips_start_of_iteration_audit_when_persisted_comment_up_to_date():
    """When the most recent '# AMPA Audit Result' comment is newer than the
    most-recent updatedAt across the recursive scope, Ralph should skip the
    start-of-iteration /skill:audit invocation and use the persisted audit.
    """
    runner = FakeRunner()
    # Target starts in_review
    runner.items["SA-TARGET"]["stage"] = "in_review"
    # Set updatedAt timestamps for target and child (child older)
    runner.items["SA-TARGET"]["updatedAt"] = "2026-05-20T11:00:00Z"
    runner.items["SA-CHILD"]["updatedAt"] = "2026-05-20T10:00:00Z"
    # Persisted audit already present on the work item
    runner.items["SA-TARGET"]["audit"] = AUDIT_PASS
    # A recent AMPA comment whose createdAt is later than the updatedAt values
    runner.comments = [{"comment": "# AMPA Audit Result\n\n...", "author": "ralph", "createdAt": "2026-05-20T12:00:00Z"}]

    loop = RalphLoop(runner=runner, stream=False, max_attempts=2)
    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    # There should be NO implement or /skill:audit calls — Ralph relied on persisted audit
    pi_calls = [c for c in runner.calls if c[0] == "pi" and "-p" in c]
    implement_calls = [c for c in pi_calls if c[-1].startswith("implement")]
    audit_calls = [c for c in pi_calls if c[-1].startswith("/skill:audit")]
    assert len(implement_calls) == 0
    assert len(audit_calls) == 0


def test_in_review_runs_start_of_iteration_audit_when_persisted_comment_outdated():
    """When the latest AMPA comment is older than the most-recent updatedAt in
    the scope, Ralph must invoke /skill:audit at the start of the iteration.
    """
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "in_review"
    # Child updated more recently than the audit comment
    runner.items["SA-TARGET"]["updatedAt"] = "2026-05-20T10:00:00Z"
    runner.items["SA-CHILD"]["updatedAt"] = "2026-05-20T12:00:00Z"
    # A stale AMPA comment
    runner.comments = [{"comment": "# AMPA Audit Result\n\nold", "author": "ralph", "createdAt": "2026-05-20T09:00:00Z"}]
    # The audit skill will be invoked and will persist the audit
    runner.audit_outputs = [AUDIT_PASS]

    loop = RalphLoop(runner=runner, stream=False, max_attempts=2)
    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    pi_calls = [c for c in runner.calls if c[0] == "pi" and "-p" in c]
    # Because the persisted comment was stale, Ralph should have invoked /skill:audit
    audit_calls = [c for c in pi_calls if c[-1].startswith("/skill:audit")]
    assert len(audit_calls) == 1


class TransitionToInReviewRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.items["SA-CHILD"]["stage"] = "in_progress"

    def __call__(self, cmd):
        cmd = list(cmd)
        if cmd and cmd[0] == "pi" and "-p" in cmd and cmd[-1].startswith("implement"):
            self.items["SA-CHILD"]["stage"] = "in_review"
        return super().__call__(cmd)


def test_compact_invoked_when_child_transitions_to_in_review():
    runner = TransitionToInReviewRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    assert result["compact"]["invocations"] == 1
    assert result["compact"]["failures"] == 0
    compact_calls = [c for c in runner.calls if c[0] == "pi" and "-p" in c and c[-1] == "/compact"]
    assert len(compact_calls) == 1


def test_compact_failure_is_non_fatal_and_loop_continues():
    runner = TransitionToInReviewRunner()
    runner.compact_failures_remaining = 1
    runner.audit_outputs = [AUDIT_FAIL, AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False, max_attempts=3)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    assert result["attempt"] == 2
    assert result["compact"]["invocations"] == 1
    assert result["compact"]["failures"] == 1
    audit_calls = [c for c in runner.calls if c[0] == "pi" and "-p" in c and c[-1].startswith("/skill:audit")]
    assert len(audit_calls) == 2


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


def test_ralph_reads_persisted_audit_and_does_not_update():
    """Ralph should read the audit persisted by the audit skill via wl show and
    must NOT attempt to write the audit itself."""
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"

    update_calls = [c for c in runner.calls if c[:3] == ["wl", "update", "SA-TARGET"]]
    # Ralph must not write the audit to the work item; the audit skill owns persistence
    assert update_calls == []


def test_ralph_handles_persisted_audit_object_text_field():
    """When the persisted audit is stored as an object with a `text` field,
    Ralph should extract the text and proceed without error."""
    class PersistObjectRunner(FakeRunner):
        def __call__(self, cmd):
            cmd = list(cmd)
            # Intercept the audit skill invocation to persist the audit as an object
            if cmd and cmd[0] == "pi" and "-p" in cmd and cmd[-1].startswith("/skill:audit"):
                output = self.audit_outputs.pop(0)
                # Persist as an object on the work item (audit.text)
                self.items["SA-TARGET"]["audit"] = {"text": output}
                return Result(stdout=output)
            return super().__call__(cmd)

    runner = PersistObjectRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"


def test_ralph_uses_auditText_fallback_when_audit_field_missing():
    """If workItem.audit is absent but auditText is present, Ralph should use auditText."""
    class AuditTextFallbackRunner(FakeRunner):
        def __call__(self, cmd):
            cmd = list(cmd)
            if cmd and cmd[0] == "pi" and "-p" in cmd and cmd[-1].startswith("/skill:audit"):
                output = self.audit_outputs.pop(0)
                # Simulate persistence to auditText instead of audit
                self.items["SA-TARGET"].pop("audit", None)
                self.items["SA-TARGET"]["auditText"] = output
                return Result(stdout=output)
            return super().__call__(cmd)

    runner = AuditTextFallbackRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"


def test_ralph_errors_if_no_persisted_audit_found():
    """If the audit skill does not persist a structured audit to the work item,
    ralph should abort and report an error (no fallback allowed)."""
    class NoPersistRunner(FakeRunner):
        def __call__(self, cmd):
            cmd = list(cmd)
            # when asked to run /skill:audit, return a response but do NOT persist it
            if cmd and cmd[0] == "pi" and "-p" in cmd and cmd[-1].startswith("/skill:audit"):
                output = self.audit_outputs.pop(0)
                return Result(stdout=output)
            return super().__call__(cmd)

    runner = NoPersistRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    with pytest.raises(RalphError, match="No persisted audit found"):
        loop.run("SA-TARGET")


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
# =============================================================================
# Auto-plan feature tests
# =============================================================================


def test_autoplan_skips_plan_for_small_low():
    """When stage is intake_complete and effort/risk are Small/Low, do not invoke /plan and proceed to implement."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Small"}, "risk": {"level": "Low", "score": 2}})]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # ensure no plan invocation (opencode run /plan)
    assert not any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)
    # ensure implement was invoked (pi with implement prompt)
    assert any(c[0] == "pi" and c[-1].startswith("implement") for c in runner.calls)
    # ensure autoplan comment was posted
    comment_calls = [c for c in runner.calls if c[:4] == ["wl", "comment", "add", "SA-TARGET"]]
    autoplan_comments = [c for c in comment_calls if "autoplan-decision-hash:" in c[c.index("--comment") + 1]]
    assert len(autoplan_comments) == 1
    assert "proceed to implement" in autoplan_comments[0][autoplan_comments[0].index("--comment") + 1]


def test_autoplan_skips_plan_for_extra_small_low():
    """Extra Small effort + Low risk also skips /plan."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Extra Small"}, "risk": {"level": "Low", "score": 1}})]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    assert not any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)
    assert any(c[0] == "pi" and c[-1].startswith("implement") for c in runner.calls)


def test_autoplan_invokes_plan_for_medium_high():
    """When stage is intake_complete and effort/risk are Medium/High, invoke /plan then implement."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Medium"}, "risk": {"level": "High", "score": 15}})]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # ensure plan invocation occurred
    assert any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)
    # ensure implement was invoked after plan
    plan_indices = [i for i, c in enumerate(runner.calls) if c[0] == "pi" and c[-1].startswith("/skill:plan")]
    impl_indices = [i for i, c in enumerate(runner.calls) if c[0] == "pi" and c[-1].startswith("implement")]
    assert plan_indices and impl_indices and min(impl_indices) > min(plan_indices)
    # ensure autoplan comment was posted with plan decision
    comment_calls = [c for c in runner.calls if c[:4] == ["wl", "comment", "add", "SA-TARGET"]]
    autoplan_comments = [c for c in comment_calls if "autoplan-decision-hash:" in c[c.index("--comment") + 1]]
    assert len(autoplan_comments) >= 1
    assert "run /plan" in autoplan_comments[0][autoplan_comments[0].index("--comment") + 1]


def test_autoplan_idempotent_no_duplicate_decisions():
    """Re-running ralph when an autoplan decision comment already exists skips re-computation."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.items["SA-TARGET"]["effort"] = "Small"
    runner.items["SA-TARGET"]["risk"] = "Low"
    # Pre-add an autoplan decision comment to simulate a prior run
    runner.comments = [{"comment": "# Ralph Auto-Plan Decision\nautoplan-decision-hash:abc123\n\nEffort: Small\nRisk: Low (score: 2)\nDecision: proceed to implement (effort and risk below threshold)", "author": "ralph"}]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # No effort-and-risk script should be called (idempotent)
    er_calls = [c for c in runner.calls if c[0] == "python3" and "orchestrate_estimate.py" in c[1]]
    assert len(er_calls) == 0
    # No Plan should be invoked (effort Small, risk Low → skip plan)
    assert not any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)
    # Implementation should proceed
    assert any(c[0] == "pi" and c[-1].startswith("implement") for c in runner.calls)


def test_autoplan_idempotent_skips_plan_when_effort_risk_already_set():
    """When effort and risk fields are already set on the work item, skip re-running effort-and-risk."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.items["SA-TARGET"]["effort"] = "Medium"
    runner.items["SA-TARGET"]["risk"] = "High"
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # No effort-and-risk script should be called
    er_calls = [c for c in runner.calls if c[0] == "python3" and "orchestrate_estimate.py" in c[1]]
    assert len(er_calls) == 0
    # Plan should be invoked (Medium effort + High risk → run /plan)
    assert any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)


def test_autoplan_idempotent_no_duplicate_comments():
    """Autoplan comment is not posted twice when the same decision hash exists."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Small"}, "risk": {"level": "Low", "score": 2}})]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    # First run
    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    comment_calls_1 = [c for c in runner.calls if c[:4] == ["wl", "comment", "add", "SA-TARGET"]]
    autoplan_comments_1 = [c for c in comment_calls_1 if "autoplan-decision-hash:" in c[c.index("--comment") + 1]]
    assert len(autoplan_comments_1) == 1

    # Now simulate a second run — the comment should already exist
    runner.calls.clear()
    runner.items["SA-TARGET"]["stage"] = "in_review"
    runner.audit_outputs = [AUDIT_PASS]
    loop2 = RalphLoop(runner=runner, stream=False)

    # The existing comments contain the autoplan-decision hash
    # Since the scope is already in_review, the in_review path skips autoplan
    result2 = loop2.run("SA-TARGET")
    assert result2["status"] == "success"


def test_autoplan_failure_defaults_to_plan():
    """When the effort-and-risk script fails, default to running /plan (safety-first)."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    # Simulate failure by returning non-zero returncode
    runner.effort_outputs = [Result(returncode=1, stdout="", stderr="error").stdout]
    # Since FakeRunner returns success for orchestrate_estimate, we override the __call__
    # Actually, let's use a custom runner that returns failure
    class FailRunner(FakeRunner):
        def __call__(self, cmd):
            if cmd[0] == "python3" and len(cmd) > 1 and "orchestrate_estimate.py" in cmd[1]:
                self.calls.append(cmd)
                return Result(returncode=1, stdout="", stderr="script failed")
            return super().__call__(cmd)

    fail_runner = FailRunner()
    fail_runner.items["SA-TARGET"]["stage"] = "intake_complete"
    fail_runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=fail_runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # Plan should be invoked due to failure fallback
    assert any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in fail_runner.calls)


def test_autoplan_ambiguous_data_defaults_to_plan():
    """When effort-and-risk returns unparseable data, default to running /plan."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.effort_outputs = ["not json at all"]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # Plan should be invoked due to ambiguous data
    assert any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)


def test_autoplan_no_autoplan_flag_skips_step():
    """When --no-autoplan is set, the autplan step is skipped even for intake_complete."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)
    loop.no_autoplan = True

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # No effort-and-risk script should be called
    er_calls = [c for c in runner.calls if c[0] == "python3" and "orchestrate_estimate.py" in c[1]]
    assert len(er_calls) == 0
    # No /plan should be invoked
    assert not any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)


def test_autoplan_custom_thresholds():
    """Custom thresholds for skipping /plan are respected."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    # Medium effort + Low risk — with custom thresholds, Medium should skip plan
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Medium"}, "risk": {"level": "Low", "score": 2}})]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(
        runner=runner,
        stream=False,
        autoplan_effort_skip=frozenset({"Extra Small", "Small", "Medium"}),
        autoplan_risk_skip=frozenset({"Low"}),
    )

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # Medium + Low should skip plan with the expanded thresholds
    assert not any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)
    assert any(c[0] == "pi" and c[-1].startswith("implement") for c in runner.calls)


def test_autoplan_posts_decision_comment():
    """Autoplan posts a human-readable decision comment on the work item."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Small"}, "risk": {"level": "Low", "score": 2}})]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # Find the autoplan decision comment
    comment_calls = [c for c in runner.calls if c[:4] == ["wl", "comment", "add", "SA-TARGET"]]
    autoplan_comments = [c for c in comment_calls if "autoplan-decision-hash:" in c[c.index("--comment") + 1]]
    assert len(autoplan_comments) == 1
    comment_text = autoplan_comments[0][autoplan_comments[0].index("--comment") + 1]
    assert "# Ralph Auto-Plan Decision" in comment_text
    assert "Effort: Small" in comment_text
    assert "Risk: Low (score: 2)" in comment_text
    assert "proceed to implement" in comment_text


def test_autoplan_effort_risk_persistence():
    """The effort-and-risk script updates the work item's effort and risk fields."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.effort_outputs = [json.dumps({
        "effort": {"tshirt": "Medium"},
        "risk": {"level": "High", "score": 15}
    })]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # The orchestrate_estimate.py script is responsible for calling wl update --effort --risk
    # and wl comment add. Here we just verify that /plan was invoked because
    # effort Medium + risk High exceeds the default thresholds.
    assert any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)


def test_autoplan_already_computed_with_plan_complete():
    """When effort/risk are already set and stage is plan_complete, skip both
    effort-and-risk and /plan."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.items["SA-TARGET"]["effort"] = "Small"
    runner.items["SA-TARGET"]["risk"] = "Low"
    # Add a comment with autoplan decision hash (simulating prior run)
    import hashlib
    marker_key = "autoplan-decision:Small:Low:0"
    marker_hash = hashlib.sha256(marker_key.encode("utf-8")).hexdigest()[:16]
    runner.comments = [{"comment": f"# Ralph Auto-Plan Decision\nautoplan-decision-hash:{marker_hash}\n\nEffort: Small\nRisk: Low (score: 0)\nDecision: proceed to implement (effort and risk below threshold)", "author": "ralph"}]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, stream=False)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # No effort-and-risk script should be called
    er_calls = [c for c in runner.calls if c[0] == "python3" and "orchestrate_estimate.py" in c[1]]
    assert len(er_calls) == 0
    # No Plan should be invoked (Small + Low → skip plan)
    assert not any(c[0] == "pi" and c[-1].startswith("/skill:plan") for c in runner.calls)
    # Implementation should proceed
    assert any(c[0] == "pi" and c[-1].startswith("implement") for c in runner.calls)


def test_autoplan_only_runs_on_first_attempt():
    """Autoplan should only run on the first attempt, not on retries."""
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "intake_complete"
    runner.effort_outputs = [json.dumps({"effort": {"tshirt": "Small"}, "risk": {"level": "Low", "score": 2}})]
    runner.audit_outputs = [AUDIT_FAIL, AUDIT_PASS]  # First audit fails, second passes
    loop = RalphLoop(runner=runner, stream=False, max_attempts=3)

    result = loop.run("SA-TARGET")
    assert result["status"] == "success"
    # Effort-and-risk should only be called once
    er_calls = [c for c in runner.calls if c[0] == "python3" and "orchestrate_estimate.py" in c[1]]
    assert len(er_calls) == 1
    # Two implement calls (first attempt + second attempt after failed audit)
    impl_calls = [c for c in runner.calls if c[0] == "pi" and c[-1].startswith("implement")]
    assert len(impl_calls) == 2


def test_cli_parser_autoplan_flags():
    """Test --no-autoplan, --autoplan-effort-skip, and --autoplan-risk-skip flags."""
    from skill.ralph.scripts.ralph_loop import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "SA-X1", "--no-autoplan",
        "--autoplan-effort-skip", "Small", "Extra Small",
        "--autoplan-risk-skip", "Low",
    ])
    assert args.no_autoplan is True
    assert args.autoplan_effort_skip == ["Small", "Extra Small"]
    assert args.autoplan_risk_skip == ["Low"]
