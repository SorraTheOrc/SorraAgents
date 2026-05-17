import os
import tempfile
from dataclasses import dataclass

import pytest

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

        if cmd[:2] == ["pi", "run"]:
            prompt = cmd[2]
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


def test_precondition_requires_plan_complete():
    runner = FakeRunner()
    runner.items["SA-TARGET"]["stage"] = "in_progress"
    loop = RalphLoop(runner=runner)

    with pytest.raises(RalphError):
        loop.run("SA-TARGET")


def test_happy_path_success_with_merge_offer_not_executed_without_confirm():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, check_cmds=["pytest -q"], max_attempts=2, confirm_merge=False)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    assert result["merge_offered"] is True
    assert result["merge_executed"] is False
    assert not any(call[:2] == ["git", "push"] for call in runner.calls)


def test_retry_path_uses_remediation_in_next_implement_prompt():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_FAIL, AUDIT_PASS]
    loop = RalphLoop(runner=runner, max_attempts=3)

    result = loop.run("SA-TARGET")

    assert result["status"] == "success"
    implement_prompts = [c[2] for c in runner.calls if c[:2] == ["pi", "run"] and c[2].startswith("implement")]
    assert len(implement_prompts) == 2
    assert "retry loop" in implement_prompts[1]


def test_cancel_file_stops_loop():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    with tempfile.TemporaryDirectory() as td:
        cancel_file = os.path.join(td, "cancel")
        open(cancel_file, "w", encoding="utf-8").close()
        loop = RalphLoop(runner=runner, cancel_file=cancel_file)
        result = loop.run("SA-TARGET")

    assert result["status"] == "cancelled"


def test_max_attempts_returns_max_attempts_status():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_FAIL, AUDIT_FAIL]
    loop = RalphLoop(runner=runner, max_attempts=2)

    result = loop.run("SA-TARGET")

    assert result["status"] == "max_attempts"


def test_confirm_merge_executes_git_steps():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, confirm_merge=True)

    result = loop.run("SA-TARGET")

    assert result["merge_executed"] is True
    assert any(call[:3] == ["git", "fetch", "origin"] for call in runner.calls)
    assert any(call[:2] == ["git", "push"] for call in runner.calls)


def test_idempotent_audit_comment_append_skips_duplicate_hash():
    runner = FakeRunner()
    digest = _comment_hash(AUDIT_PASS)
    runner.comments = [{"comment": f"# AMPA Audit Result\naudit-hash:{digest}\n\n..."}]
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner)

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
    loop = RalphLoop(runner=runner)

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
    loop = RalphLoop(runner=runner)

    # Direct API call gives RalphError
    with pytest.raises(RalphError, match="plan_complete"):
        loop.run("SA-TARGET")


class FakeRunnerWithPushFailure(FakeRunner):
    def __call__(self, cmd):
        if cmd and cmd[:2] == ["git", "push"]:
            return Result(returncode=1, stderr="remote: Permission denied")
        return super().__call__(cmd)


def test_merge_permission_failure_raises_ralph_error():
    runner = FakeRunnerWithPushFailure()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner, confirm_merge=True)

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
    loop = RalphLoop(runner=runner, check_cmds=["pytest -q"])

    with pytest.raises(RalphError, match="Check failed"):
        loop.run("SA-TARGET")


def test_audit_text_written_via_wl_update():
    runner = FakeRunner()
    runner.audit_outputs = [AUDIT_PASS]
    loop = RalphLoop(runner=runner)

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
        loop = RalphLoop(runner=runner, verbose=True)
        loop.run("SA-TARGET")

        debug_msgs = [r for r in records if r.levelno == logging.DEBUG]
        pi_debug_msgs = [r for r in debug_msgs if "ralph.cmd.pi.run" in r.getMessage()]
        # Should have at least implement and audit pi.run debug logs
        assert len(pi_debug_msgs) >= 2
        # Check that stdout_start is logged (the implement output)
        stdout_msgs = [r for r in pi_debug_msgs if "stdout_start" in r.getMessage()]
        assert len(stdout_msgs) >= 1
    finally:
        logger.setLevel(old_level)
        logger.removeHandler(handler)
