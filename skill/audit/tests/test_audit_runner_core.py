from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from skill.audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests.

    ``_call_pi`` acquires the real cross-process audit semaphore before
    launching the (mocked) subprocess. Under concurrent audit load the
    semaphore can saturate, making these timing-path unit tests flaky (see
    SA-0MSCDC4750019G9Y, SA-0MSCDC76A007JCJK). Replace it with a
    null-context so the mocked return paths are exercised directly.

    The real semaphore behavior is covered separately by
    ``test_audit_runner_concurrency.py``.
    """
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield

class TestDetectProjectRoot:
    """Tests for _detect_project_root() auto-detection logic (AC1, AC2)."""

    def test_detects_git_root(self):
        """AC1: TARGET_PROJECT_ROOT resolves to git root when CWD is inside a git repo.

        Mocks ``subprocess.run`` to return a successful git toplevel response,
        then asserts the returned Path matches the expected git root.
        """
        with mock.patch.object(audit_runner.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "rev-parse", "--show-toplevel"],
                returncode=0,
                stdout="/home/user/project\n",
                stderr="",
            )
            result = audit_runner._detect_project_root()

            assert result == Path("/home/user/project")
            mock_run.assert_called_once_with(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )

    def test_fallback_to_cwd(self):
        """AC2: TARGET_PROJECT_ROOT falls back to Path.cwd() when CWD is outside a git repo.

        Mocks ``subprocess.run`` to raise ``CalledProcessError`` (git not in a
        repo), then asserts the returned Path equals ``Path.cwd()``.
        """
        with mock.patch.object(audit_runner.subprocess, "run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=128,
                cmd=["git", "rev-parse", "--show-toplevel"],
            )
            result = audit_runner._detect_project_root()

            assert result == Path.cwd()
            mock_run.assert_called_once_with(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )

    def test_fallback_on_oserror(self):
        """TARGET_PROJECT_ROOT falls back to Path.cwd() when subprocess raises OSError."""
        with mock.patch.object(audit_runner.subprocess, "run") as mock_run:
            mock_run.side_effect = OSError("git not available")
            result = audit_runner._detect_project_root()

            assert result == Path.cwd()
            mock_run.assert_called_once_with(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )

class TestDefaultDebugLogPath:
    """Tests for _default_debug_log_path() (SA-0MSBSOAEM0078LAO)."""

    def test_uses_target_project_root(self):
        """Debug log path is outside .worklog/ and outside the repo tree.

        Calls ``_default_debug_log_path`` and asserts the returned path is
        rooted under ``~/.audit_debug/<project>/audit_debug_<id>...`` — never
        under ``TARGET_PROJECT_ROOT/.worklog`` (the 9.5 GB scan trap).
        """
        log_path = audit_runner._default_debug_log_path("TEST-123", "parent")

        assert log_path.name == "audit_debug_TEST-123.jsonl"
        assert ".worklog" not in log_path.parts
        assert str(audit_runner.TARGET_PROJECT_ROOT) not in str(log_path)

    def test_uses_context_in_filename(self):
        """Verify the context parameter is used when constructing the file name."""
        log_path = audit_runner._default_debug_log_path("CHILD-456", "child")
        assert log_path.name == "audit_debug_CHILD-456.jsonl"
        assert ".worklog" not in log_path.parts

    def test_returns_path_object(self):
        """_default_debug_log_path returns a Path instance."""
        result = audit_runner._default_debug_log_path("TEST-1", "parent")
        assert isinstance(result, Path)

class TestCodeQualityUsesTargetProjectRoot:
    """Tests for code quality invocation using TARGET_PROJECT_ROOT (AC4)."""

    def _make_mock_runner(self):
        """Build a mock runner that handles all ``wl`` commands needed by ``cmd_issue``.

        The call sequence expected by ``cmd_issue("TEST-1", ...)`` with
        ``force=True``, ``persist=False``, and empty children is:

        1. ``StatusLifecycle.show`` → ``wl show TEST-1 --json``
        2. ``StatusLifecycle.update_status`` → ``wl update ... --status in_progress ...``
        3. ``_run_wl`` → ``wl show TEST-1 --children --json``
        4. ``StatusLifecycle.update_status`` (in ``finally``) → ``wl update ...``
        """
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)

            # StatusLifecycle.show → wl show <id> --json
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )

            # StatusLifecycle.update_status → wl update <id> --status ...
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )

            # _run_wl → wl show <id> --children --json
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "",
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )

            # Fallback for any unexpected commands
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def test_code_quality_passed_target_project_root(self):
        """AC4: Code quality invocation passes TARGET_PROJECT_ROOT as project_root.

        Patches ``run_code_quality`` and calls ``cmd_issue`` with a mock runner.
        Asserts that ``run_code_quality`` was called with ``project_root``
        equal to the module's ``TARGET_PROJECT_ROOT`` constant.
        """
        # Mock run_code_quality to capture its arguments
        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

        mock_runner = self._make_mock_runner()

        with mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality", mock_cq
        ):
            audit_runner.cmd_issue(
                "TEST-1",
                persist=False,
                force=True,  # Skip freshness gate
                runner=mock_runner,
            )

        mock_cq.assert_called_once()
        _args, kwargs = mock_cq.call_args
        assert kwargs["project_root"] == audit_runner.TARGET_PROJECT_ROOT

    def test_code_quality_not_passed_repo_root(self):
        """AC4 (guard): Ensure project_root is NOT the same as REPO_ROOT when they differ.

        When TARGET_PROJECT_ROOT differs from REPO_ROOT (i.e. when the
        audit runner's framework root is not the CWD), the code quality
        call must receive TARGET_PROJECT_ROOT, not REPO_ROOT.

        In the test environment both may be equal (running inside the
        SorraAgents repo), but we verify they are passed correctly.
        """
        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

        mock_runner = self._make_mock_runner()

        with mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality", mock_cq
        ):
            audit_runner.cmd_issue(
                "TEST-1",
                persist=False,
                force=True,
                runner=mock_runner,
            )

        mock_cq.assert_called_once()
        _args, kwargs = mock_cq.call_args
        assert kwargs["project_root"] is audit_runner.TARGET_PROJECT_ROOT

class TestCallPiEnableTools:
    """Tests for _call_pi() enable_tools parameter (AC1-AC5)."""

    def _make_mock_popen(self, stdout_text: str = "{\"text\": \"test\"}"):
        """Create a mock Popen that returns a process-like object."""
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (stdout_text, "")
        mock_process.returncode = 0
        return mock_process

    def test_command_includes_tools_when_enable_tools_true(self):
        """AC1: _call_pi() adds --tools flags when enable_tools=True.

        Mocks ``subprocess.Popen`` and asserts the constructed command
        includes ``--tools read,bash,grep,find,ls --exclude-tools ask_question``
        plus the context-reduction flags ``--no-context-files --no-skills``.
        """
        mock_process = self._make_mock_popen()

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process) as mock_popen:
            audit_runner._call_pi("test prompt", model="test-model", enable_tools=True)

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]  # The command list
        assert "--tools" in args
        tools_idx = args.index("--tools")
        assert args[tools_idx + 1] == "read,bash,grep,find,ls"
        assert "--exclude-tools" in args
        exclude_idx = args.index("--exclude-tools")
        assert args[exclude_idx + 1] == "ask_question"
        # Context reduction flags are present in the tool-enabled path
        assert "--no-context-files" in args
        assert "--no-skills" in args

    def test_command_unchanged_when_enable_tools_false(self):
        """AC2: _call_pi() does NOT add --tools when enable_tools=False (default)."""
        mock_process = self._make_mock_popen()

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process) as mock_popen:
            audit_runner._call_pi("test prompt", model="test-model")

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "--tools" not in args
        assert "--exclude-tools" not in args
        # Standard flags are present
        assert args[0] == "pi"
        assert args[1] == "-p"
        assert args[2] == "--mode"
        assert args[3] == "json"
        # Context reduction flags are present in the no-tools path
        assert "--no-context-files" in args
        assert "--no-skills" in args

    def test_default_enable_tools_is_false(self):
        """AC3: Default value of enable_tools is False (backward compatible)."""
        mock_process = self._make_mock_popen()

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process) as mock_popen:
            audit_runner._call_pi("test prompt", model="test-model")

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "--tools" not in args

    def test_existing_callers_unchanged(self):
        """AC4: Existing callers work without modification (default enable_tools=False).

        Verifies the default command structure matches current behavior,
        including the context-reduction flags every call now carries.
        """
        mock_process = self._make_mock_popen()

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process) as mock_popen:
            # Call with the same signature as existing callers use
            audit_runner._call_pi("test prompt", model="test-model", pi_bin="pi")

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args == [
            "pi", "-p", "--mode", "json", "--model", "test-model",
            "test prompt", "--no-context-files", "--no-skills",
        ]

    def test_context_reduction_flags_present_in_both_tool_modes(self):
        """SA-0MSISKM8F004NW1U AC1: --no-context-files --no-skills in both modes.

        Asserts the context-reduction flags are part of the constructed pi
        command for both enable_tools=True and enable_tools=False paths.
        """
        mock_process = self._make_mock_popen()

        for enable_tools in (False, True):
            with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process) as mock_popen:
                audit_runner._call_pi(
                    "test prompt", model="test-model", enable_tools=enable_tools
                )
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert "--no-context-files" in args
            assert "--no-skills" in args
            # Flags come after the prompt, before/around the tools block
            assert args.index("--no-context-files") < args.index("--no-skills")
            if enable_tools:
                assert "--tools" in args
            else:
                assert "--tools" not in args

    def test_timeout_handling_unchanged(self):
        """AC5: Timeout handling remains unchanged when enable_tools=True."""
        mock_process = mock.MagicMock()
        # First communicate() raises TimeoutExpired, second returns normally
        timeout_error = subprocess.TimeoutExpired(cmd="pi", timeout=10, output="", stderr="")
        mock_process.communicate.side_effect = [timeout_error, ("", "")]

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process):
            result = audit_runner._call_pi("test prompt", model="test-model", enable_tools=True)

        assert result.get("_timeout") is True
        assert result.get("verdict") == "unmet"
        assert "timed out" in result.get("evidence", "")

class TestCallPiAndMaybeLogEnableTools:
    """Tests for _call_pi_and_maybe_log() forwarding enable_tools (AC1-AC3)."""

    def _make_mock_popen(self, stdout_text: str = "{\"text\": \"test\"}"):
        """Create a mock Popen that returns a process-like object."""
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (stdout_text, "")
        mock_process.returncode = 0
        return mock_process

    def test_forwards_enable_tools_true_to_call_pi(self):
        """AC1: enable_tools=True is forwarded to _call_pi().

        Mocks _call_pi and asserts it was called with enable_tools=True.
        """
        with mock.patch.object(audit_runner, "_call_pi") as mock_call_pi:
            mock_call_pi.return_value = {"verdict": "met", "evidence": "ok"}

            audit_runner._call_pi_and_maybe_log(
                "TEST-1", "phase2", "test prompt",
                model="test-model", enable_tools=True,
            )

        mock_call_pi.assert_called_once()
        _args, kwargs = mock_call_pi.call_args
        assert kwargs.get("enable_tools") is True

    def test_forwards_enable_tools_false_to_call_pi(self):
        """AC2: enable_tools=False is forwarded to _call_pi()."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call_pi:
            mock_call_pi.return_value = {"verdict": "met", "evidence": "ok"}

            audit_runner._call_pi_and_maybe_log(
                "TEST-1", "phase2", "test prompt",
                model="test-model", enable_tools=False,
            )

        mock_call_pi.assert_called_once()
        _args, kwargs = mock_call_pi.call_args
        assert kwargs.get("enable_tools") is False

    def test_default_enable_tools_is_false(self):
        """AC3: Default enable_tools is False (backward compatible).

        Existing callers that don't pass enable_tools should get the default.
        """
        with mock.patch.object(audit_runner, "_call_pi") as mock_call_pi:
            mock_call_pi.return_value = {"verdict": "met", "evidence": "ok"}

            # Call with same signature as existing callers
            audit_runner._call_pi_and_maybe_log(
                "TEST-1", "phase2", "test prompt",
                model="test-model",
            )

        mock_call_pi.assert_called_once()
        _args, kwargs = mock_call_pi.call_args
        assert kwargs.get("enable_tools") is False

    def test_existing_callers_unchanged(self):
        """AC3 (guard): Existing callers work without modification."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call_pi:
            mock_call_pi.return_value = {"verdict": "met", "evidence": "ok"}

            # Call with same signature as current callers use
            audit_runner._call_pi_and_maybe_log(
                "TEST-1", "phase2", "test prompt",
                model="test-model", pi_bin="pi",
            )

        mock_call_pi.assert_called_once()
        _args, kwargs = mock_call_pi.call_args
        # ensure enable_tools is False by default
        assert kwargs.get("enable_tools") is False
        # Original parameters are still forwarded
        assert kwargs.get("model") == "test-model"
        assert kwargs.get("pi_bin") == "pi"

class TestEffectiveTimeoutResolution:
    """Tests for effective timeout resolution (--timeout flag vs env var)."""

    def test_cli_flag_wins_over_env_var(self):
        """The --timeout CLI flag takes precedence over AUDIT_PI_TIMEOUT."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PI_TIMEOUT_ENV: "3600"},
            clear=False,
        ):
            assert audit_runner._resolve_effective_timeout(900) == 900

    def test_env_var_used_when_no_cli_flag(self):
        """AUDIT_PI_TIMEOUT is used when --timeout is not passed."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PI_TIMEOUT_ENV: "3600"},
            clear=False,
        ):
            assert audit_runner._resolve_effective_timeout(None) == 3600

    def test_none_when_no_flag_and_no_env(self):
        """Returns None (CALL_PI_TIMEOUT default) when neither is set."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {},
            clear=True,
        ):
            assert audit_runner._resolve_effective_timeout(None) is None

    def test_invalid_env_value_falls_back_to_none(self):
        """An invalid AUDIT_PI_TIMEOUT value falls back to the default."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PI_TIMEOUT_ENV: "not-a-number"},
            clear=False,
        ):
            assert audit_runner._resolve_effective_timeout(None) is None

    def test_env_var_constant_defined(self):
        """The AUDIT_PI_TIMEOUT env var constant is defined."""
        assert audit_runner.AUDIT_PI_TIMEOUT_ENV == "AUDIT_PI_TIMEOUT"

    def test_main_resolves_env_var_timeout(self):
        """main() resolves the effective timeout from env var and passes it through."""
        with (
            mock.patch.object(audit_runner, "cmd_issue") as mock_cmd,
            mock.patch.object(audit_runner, "cmd_project") as mock_project,
            mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"),
            mock.patch.dict(
                audit_runner.os.environ,
                {audit_runner.AUDIT_PI_TIMEOUT_ENV: "3600"},
                clear=False,
            ),
        ):
            rc = audit_runner.main(["issue", "SA-123", "--do-not-persist"])
            assert rc == mock_cmd.return_value
            _args, kwargs = mock_cmd.call_args
            assert kwargs["timeout"] == 3600

            rc = audit_runner.main(["project"])
            assert rc == mock_project.return_value
            _args, kwargs = mock_project.call_args
            assert kwargs["timeout"] == 3600

    def test_main_uses_cli_flag_over_env_var(self):
        """main() prefers the CLI --timeout flag over the env var."""
        with (
            mock.patch.object(audit_runner, "cmd_issue") as mock_cmd,
            mock.patch.object(audit_runner, "cmd_project") as mock_project,
            mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"),
            mock.patch.dict(
                audit_runner.os.environ,
                {audit_runner.AUDIT_PI_TIMEOUT_ENV: "3600"},
                clear=False,
            ),
        ):
            audit_runner.main(["issue", "SA-123", "--do-not-persist", "--timeout", "7200"])
            _args, kwargs = mock_cmd.call_args
            assert kwargs["timeout"] == 7200

            audit_runner.main(["project", "--timeout", "7200"])
            _args, kwargs = mock_project.call_args
            assert kwargs["timeout"] == 7200

class TestCallPiTimingInstrumentation:
    """Tests for elapsed-time instrumentation in _call_pi (AC1).

    Every return path of ``_call_pi`` (success, timeout, provider error,
    empty output) must attach a non-negative ``elapsed_seconds`` value
    measured with ``time.monotonic``.
    """

    def _make_mock_popen(self, stdout_text: str = '{"text": "test"}'):
        """Create a mock Popen that returns a process-like object."""
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (stdout_text, "")
        mock_process.returncode = 0
        return mock_process

    def test_elapsed_seconds_attached_on_success(self):
        """AC1: _call_pi attaches elapsed_seconds on a successful call."""
        mock_process = self._make_mock_popen()

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process):
            result = audit_runner._call_pi("test prompt", model="test-model")

        assert "elapsed_seconds" in result
        assert isinstance(result["elapsed_seconds"], float)
        assert result["elapsed_seconds"] >= 0

    def test_elapsed_seconds_attached_on_timeout(self):
        """AC1: elapsed_seconds is attached on the timeout return path."""
        mock_process = mock.MagicMock()
        timeout_error = subprocess.TimeoutExpired(cmd="pi", timeout=10, output="", stderr="")
        mock_process.communicate.side_effect = [timeout_error, ("", "")]

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process):
            result = audit_runner._call_pi("test prompt", model="test-model")

        assert result.get("_timeout") is True
        assert "elapsed_seconds" in result
        assert result["elapsed_seconds"] >= 0

    def test_elapsed_seconds_attached_on_provider_error(self):
        """AC1: elapsed_seconds is attached on the provider-error return path."""
        provider_error_stream = json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant", "stopReason": "error", "errorMessage": "boom"}],
        })
        mock_process = self._make_mock_popen(stdout_text=provider_error_stream)

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process):
            result = audit_runner._call_pi("test prompt", model="test-model")

        assert result.get("_provider_error") is True
        assert "elapsed_seconds" in result
        assert result["elapsed_seconds"] >= 0

    def test_elapsed_seconds_attached_on_empty_output(self):
        """AC1: elapsed_seconds is attached even when the output is empty."""
        mock_process = self._make_mock_popen(stdout_text="")

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process):
            result = audit_runner._call_pi("test prompt", model="test-model")

        assert "elapsed_seconds" in result
        assert result["elapsed_seconds"] >= 0

class TestCallPiAndMaybeLogTiming:
    """Tests for per-call timing emission in _call_pi_and_maybe_log (AC2/AC3)."""

    def test_timing_line_emitted_to_stderr(self, capsys):
        """AC2: a timing line with issue id, context, and elapsed seconds goes to stderr."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {
                "verdict": "met", "evidence": "ok", "elapsed_seconds": 12.34,
            }
            audit_runner._call_pi_and_maybe_log("SA-123", "phase2_deep", "prompt")

        captured = capsys.readouterr()
        assert "SA-123" in captured.err
        assert "phase2_deep" in captured.err
        assert "12.34" in captured.err

    def test_timing_line_emitted_without_debug_log(self, capsys):
        """AC2: the timing line is emitted even when no debug_log is configured."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {
                "verdict": "met", "evidence": "ok", "elapsed_seconds": 1.25,
            }
            audit_runner._call_pi_and_maybe_log("SA-123", "parent", "prompt")

        captured = capsys.readouterr()
        assert "timing" in captured.err.lower()
        assert "parent" in captured.err
        assert "1.25" in captured.err

    def test_debug_log_entry_includes_elapsed(self, tmp_path):
        """AC3: debug-log entry includes elapsed_seconds alongside issue_id and context."""
        log = tmp_path / "debug.jsonl"
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {
                "verdict": "unmet",
                "evidence": "",
                "raw_stdout": "raw",
                "raw_stderr": "",
                "elapsed_seconds": 5.5,
            }
            audit_runner._call_pi_and_maybe_log(
                "SA-123", "phase2_child:0", "prompt", debug_log=str(log),
            )

        lines = log.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["issue_id"] == "SA-123"
        assert entry["context"] == "phase2_child:0"
        assert entry["elapsed_seconds"] == 5.5

class TestInputTokensCapture:
    """Tests for input-token capture (SA-0MSISKM8F004NW1U AC2).

    Every successful ``_call_pi`` return attaches ``input_tokens`` extracted
    from the ``agent_end`` message's usage block, so the per-call timing line
    can verify the context-reduction bound (<10K initial input tokens per
    audit session) without a debug log.
    """

    def _make_mock_popen(self, stdout_text: str):
        """Create a mock Popen that returns a process-like object."""
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (stdout_text, "")
        mock_process.returncode = 0
        return mock_process

    def _agent_end_stream(self, usage: dict | None, text: str = '{"verdict": "met", "evidence": "ok"}') -> str:
        """Build a pi --mode json stream with an agent_end carrying usage."""
        assistant = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        if usage is not None:
            assistant["usage"] = usage
        return json.dumps({"type": "agent_end", "messages": [assistant]})

    def test_extract_input_tokens_helper(self):
        """The helper reads input from the agent_end assistant usage block."""
        stream = self._agent_end_stream({"input": 769, "output": 10, "totalTokens": 779})
        assert audit_runner._extract_input_tokens(stream) == 769
        # No usage block -> None
        assert audit_runner._extract_input_tokens(self._agent_end_stream(None)) is None
        # No agent_end event -> None
        assert audit_runner._extract_input_tokens('{"type": "turn_end"}') is None
        assert audit_runner._extract_input_tokens("") is None

    def test_input_tokens_attached_on_success(self):
        """_call_pi attaches input_tokens from the agent_end usage block."""
        mock_process = self._make_mock_popen(self._agent_end_stream({"input": 769}))
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process):
            result = audit_runner._call_pi("test prompt", model="test-model")
        assert result["verdict"] == "met"
        assert result["input_tokens"] == 769

    def test_input_tokens_none_when_no_usage(self):
        """input_tokens is None when the stream carries no usage data."""
        mock_process = self._make_mock_popen(self._agent_end_stream(None))
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process):
            result = audit_runner._call_pi("test prompt", model="test-model")
        assert result["verdict"] == "met"
        assert result["input_tokens"] is None

    def test_input_tokens_attached_on_free_form_text(self):
        """input_tokens is attached even when pi returns free-form text."""
        stream = self._agent_end_stream({"input": 500}, text="plain response")
        mock_process = self._make_mock_popen(stream)
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process):
            result = audit_runner._call_pi("test prompt", model="test-model")
        assert result["verdict"] == "met"
        assert result["input_tokens"] == 500

    def test_timing_line_includes_input_tokens(self, capsys):
        """The per-call timing line appends input_tokens when captured."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {
                "verdict": "met", "evidence": "ok",
                "elapsed_seconds": 3.0, "input_tokens": 769,
            }
            audit_runner._call_pi_and_maybe_log("SA-123", "parent", "prompt")
        captured = capsys.readouterr()
        assert "input_tokens=769" in captured.err

    def test_timing_line_omits_input_tokens_when_absent(self, capsys):
        """The timing line keeps the legacy format when input_tokens is None."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {
                "verdict": "met", "evidence": "ok", "elapsed_seconds": 3.0,
            }
            audit_runner._call_pi_and_maybe_log("SA-123", "parent", "prompt")
        captured = capsys.readouterr()
        assert "input_tokens" not in captured.err
        assert "elapsed_seconds=3.00" in captured.err

class TestParseReadyToClose:
    """Unit tests for _parse_ready_to_close()."""

    def test_parses_yes(self):
        """Ready to close: Yes is parsed as 'yes'."""
        assert audit_runner._parse_ready_to_close(
            "Ready to close: Yes\n\n## Summary"
        ) == "yes"

    def test_parses_no(self):
        """Ready to close: No is parsed as 'no'."""
        assert audit_runner._parse_ready_to_close(
            "Ready to close: No\n\n## Summary"
        ) == "no"

    def test_case_insensitive(self):
        """Verdict matching is case-insensitive."""
        assert audit_runner._parse_ready_to_close(
            "Ready to close: yEs"
        ) == "yes"

    def test_missing_verdict_returns_none(self):
        """A report with no Ready to close line yields None (unparseable)."""
        assert audit_runner._parse_ready_to_close(
            "## Summary\nNo verdict present"
        ) is None

    def test_wrapped_report_still_parses(self):
        """FailureNotice-wrapped reports (with a leading ==== header) still parse."""
        report = (
            "════════════════════════════════════════\n"
            "Failure: something went wrong\n"
            "Ready to close: Yes\n"
            "## Summary"
        )
        assert audit_runner._parse_ready_to_close(report) == "yes"

class TestParentTimeoutResolution:
    """Tests for parent-timeout resolution (--parent-timeout flag vs env var).

    The cumulative elapsed-time guard skips remaining child audits when the
    parent run approaches the parent bash-tool timeout. SA-0MSABZO2T004B95X
    made that threshold configurable via ``--parent-timeout`` /
    ``AUDIT_PARENT_TIMEOUT``. Since SA-0MSF4AFXF000M5DN the default guard
    scales with the number of active children
    (``PARENT_TIMEOUT_DEFAULT + N x PARENT_TIMEOUT_PER_CHILD``) so
    multi-child parents get a realistic default budget; explicit overrides
    keep their exact semantics.
    """

    def test_default_guard_base_and_scaling_constants(self):
        """AC1: The default guard is a base term plus a per-child budget."""
        assert audit_runner.PARENT_TIMEOUT_DEFAULT == 110
        assert audit_runner.PARENT_TIMEOUT_PER_CHILD == 600

    def test_default_guard_scales_with_child_count(self):
        """AC1: The computed default guard scales with child count (>110s for N>1)."""
        assert audit_runner._default_parent_timeout(0) == 110
        assert audit_runner._default_parent_timeout(1) == 710
        assert audit_runner._default_parent_timeout(2) == 1310
        assert audit_runner._default_parent_timeout(10) == 6110
        assert audit_runner._default_parent_timeout(10) > 110

    def test_env_var_constant_defined(self):
        """AC1: The AUDIT_PARENT_TIMEOUT env var constant is defined."""
        assert audit_runner.AUDIT_PARENT_TIMEOUT_ENV == "AUDIT_PARENT_TIMEOUT"

    def test_cli_flag_wins_over_env_var(self):
        """AC1: The --parent-timeout CLI flag takes precedence over the env var."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARENT_TIMEOUT_ENV: "600"},
            clear=False,
        ):
            assert audit_runner._resolve_parent_timeout(900) == 900

    def test_env_var_used_when_no_cli_flag(self):
        """AC1: AUDIT_PARENT_TIMEOUT is used when --parent-timeout is not passed."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARENT_TIMEOUT_ENV: "600"},
            clear=False,
        ):
            assert audit_runner._resolve_parent_timeout(None) == 600

    def test_no_override_when_nothing_set(self):
        """AC1: No override (None) when neither flag nor env is set — cmd_issue
        computes the scaled default from the child count."""
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            assert audit_runner._resolve_parent_timeout(None) is None

    def test_invalid_env_value_ignored(self):
        """AC1: An invalid AUDIT_PARENT_TIMEOUT value is ignored — no override,
        so cmd_issue computes the scaled default."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARENT_TIMEOUT_ENV: "not-a-number"},
            clear=False,
        ):
            assert audit_runner._resolve_parent_timeout(None) is None

    def test_main_resolves_env_var_parent_timeout(self):
        """AC1: main() resolves the parent timeout from env var and passes it through."""
        with (
            mock.patch.object(audit_runner, "cmd_issue") as mock_cmd,
            mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"),
            mock.patch.dict(
                audit_runner.os.environ,
                {audit_runner.AUDIT_PARENT_TIMEOUT_ENV: "600"},
                clear=False,
            ),
        ):
            rc = audit_runner.main(["issue", "SA-123", "--do-not-persist"])
            assert rc == mock_cmd.return_value
            _args, kwargs = mock_cmd.call_args
            assert kwargs["parent_timeout"] == 600

    def test_main_uses_cli_flag_over_env_var(self):
        """AC1: main() prefers the CLI --parent-timeout flag over the env var."""
        with (
            mock.patch.object(audit_runner, "cmd_issue") as mock_cmd,
            mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"),
            mock.patch.dict(
                audit_runner.os.environ,
                {audit_runner.AUDIT_PARENT_TIMEOUT_ENV: "600"},
                clear=False,
            ),
        ):
            audit_runner.main(
                ["issue", "SA-123", "--do-not-persist", "--parent-timeout", "900"]
            )
            _args, kwargs = mock_cmd.call_args
            assert kwargs["parent_timeout"] == 900

class TestMaxChildAuditsResolution:
    """Tests for the per-run child-audit cap resolution (AC3).

    The recursive child-audit cascade is bounded by a per-run cap resolved as
    ``--max-child-audits`` CLI flag > ``AUDIT_MAX_CHILD_AUDITS`` env > default
    (SA-0MSKB6V5Q007YDHE).
    """

    def test_env_constant_defined(self):
        """AC3: The AUDIT_MAX_CHILD_AUDITS env var constant is defined."""
        assert audit_runner.AUDIT_MAX_CHILD_AUDITS_ENV == "AUDIT_MAX_CHILD_AUDITS"

    def test_default_cap(self):
        """AC3: The default cap is used when no flag/env is set."""
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            assert audit_runner._resolve_max_child_audits(None) == (
                audit_runner._DEFAULT_MAX_CHILD_AUDITS
            )

    def test_cli_flag_wins_over_env(self):
        """AC3: The --max-child-audits CLI flag takes precedence over env."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_MAX_CHILD_AUDITS_ENV: "7"},
            clear=False,
        ):
            assert audit_runner._resolve_max_child_audits(2) == 2

    def test_env_var_used_when_no_flag(self):
        """AC3: AUDIT_MAX_CHILD_AUDITS is honored when no flag is passed."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_MAX_CHILD_AUDITS_ENV: "7"},
            clear=False,
        ):
            assert audit_runner._resolve_max_child_audits(None) == 7

    def test_invalid_env_falls_back_to_default(self):
        """AC3: An invalid env value falls back to the default cap with a warning."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_MAX_CHILD_AUDITS_ENV: "bogus"},
            clear=False,
        ):
            assert audit_runner._resolve_max_child_audits(None) == (
                audit_runner._DEFAULT_MAX_CHILD_AUDITS
            )

    def test_invalid_cli_falls_back_to_default(self):
        """AC3: A non-positive --max-child-audits falls back to the default."""
        assert audit_runner._resolve_max_child_audits(0) == (
            audit_runner._DEFAULT_MAX_CHILD_AUDITS
        )

    def test_main_resolves_env_var_max_child_audits(self):
        """AC3: main() resolves the cap from env var and passes it through."""
        with (
            mock.patch.object(audit_runner, "cmd_issue") as mock_cmd,
            mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"),
            mock.patch.dict(
                audit_runner.os.environ,
                {audit_runner.AUDIT_MAX_CHILD_AUDITS_ENV: "4"},
                clear=False,
            ),
        ):
            rc = audit_runner.main(["issue", "SA-123", "--do-not-persist"])
            assert rc == mock_cmd.return_value
            _args, kwargs = mock_cmd.call_args
            assert kwargs["max_child_audits"] == 4

    def test_main_resolves_audit_children_flag(self):
        """AC2: main() passes the --audit-children flag through to cmd_issue."""
        with mock.patch.object(audit_runner, "cmd_issue") as mock_cmd, \
                mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"):
            audit_runner.main(
                ["issue", "SA-123", "--do-not-persist", "--audit-children"]
            )
            _args, kwargs = mock_cmd.call_args
            assert kwargs["audit_children"] is True

    def test_main_defaults_audit_children_off(self):
        """AC1: main() defaults --audit-children to off (no cascade)."""
        with mock.patch.object(audit_runner, "cmd_issue") as mock_cmd, \
                mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"):
            audit_runner.main(["issue", "SA-123", "--do-not-persist"])
            _args, kwargs = mock_cmd.call_args
            assert kwargs["audit_children"] is False

class TestMaxCitationsPerACResolution:
    """Tests for the Phase-2 evidence citation-cap resolution (F1-AC1/AC5).

    The max-citations-per-AC cap resolves as ``--max-citations-per-ac`` CLI
    flag > ``audit.max_citations_per_ac`` CWD config key > hardcoded default
    5, with invalid values failing closed to the default with a warning
    (LP-0MSQ32WM5000NCB7 AC1).
    """

    def test_default_constant_is_five(self):
        """AC1: The hardcoded default cap is 5 file:line refs per AC."""
        assert audit_runner._DEFAULT_MAX_CITATIONS_PER_AC == 5

    def test_resolver_returns_default_with_no_config(self):
        """AC1: No CLI flag or config key -> default 5."""
        with mock.patch.object(audit_runner, "_load_config", return_value={}):
            assert audit_runner._resolve_max_citations_per_ac(None) == 5

    def test_config_dotted_key_overrides_default(self):
        """AC1: the audit.max_citations_per_ac CWD config key overrides the default."""
        with mock.patch.object(
            audit_runner, "_load_config",
            return_value={"audit.max_citations_per_ac": 3},
        ):
            assert audit_runner._resolve_max_citations_per_ac(None) == 3

    def test_config_nested_key_overrides_default(self):
        """AC1: the nested audit: {max_citations_per_ac} form is also honored."""
        with mock.patch.object(
            audit_runner, "_load_config",
            return_value={"audit": {"max_citations_per_ac": 4}},
        ):
            assert audit_runner._resolve_max_citations_per_ac(None) == 4

    def test_cli_flag_overrides_config(self):
        """AC1: --max-citations-per-ac CLI flag beats the config key."""
        with mock.patch.object(
            audit_runner, "_load_config",
            return_value={"audit.max_citations_per_ac": 3},
        ):
            assert audit_runner._resolve_max_citations_per_ac(2) == 2

    def test_invalid_cli_fails_closed_with_warning(self, capsys):
        """AC5: a non-positive --max-citations-per-ac falls back to the default."""
        with mock.patch.object(audit_runner, "_load_config", return_value={}):
            assert audit_runner._resolve_max_citations_per_ac(0) == 5
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert str(audit_runner._DEFAULT_MAX_CITATIONS_PER_AC) in captured.err

    def test_invalid_config_value_fails_closed_with_warning(self, capsys):
        """AC5: a non-int audit.max_citations_per_ac value falls back to the default."""
        with mock.patch.object(
            audit_runner, "_load_config",
            return_value={"audit.max_citations_per_ac": "bogus"},
        ):
            assert audit_runner._resolve_max_citations_per_ac(None) == 5
        assert "Warning" in capsys.readouterr().err

    def test_negative_config_fails_closed_with_warning(self, capsys):
        """AC5: a negative config value falls back to the default."""
        with mock.patch.object(
            audit_runner, "_load_config",
            return_value={"audit.max_citations_per_ac": -3},
        ):
            assert audit_runner._resolve_max_citations_per_ac(None) == 5
        assert "Warning" in capsys.readouterr().err

    def test_main_passes_cli_cap_to_cmd_issue(self):
        """AC1: main() resolves --max-citations-per-ac and passes it to cmd_issue."""
        with (
            mock.patch.object(audit_runner, "cmd_issue") as mock_cmd,
            mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"),
            mock.patch.object(audit_runner, "_load_config", return_value={}),
        ):
            audit_runner.main(
                ["issue", "SA-123", "--do-not-persist", "--max-citations-per-ac", "4"]
            )
            _args, kwargs = mock_cmd.call_args
            assert kwargs["max_citations_per_ac"] == 4

    def test_main_defaults_cap_for_cmd_issue(self):
        """AC1: main() resolves the default cap when no flag/config is present."""
        with (
            mock.patch.object(audit_runner, "cmd_issue") as mock_cmd,
            mock.patch.object(audit_runner, "_apply_proxy_mode_serialization"),
            mock.patch.object(audit_runner, "_load_config", return_value={}),
        ):
            audit_runner.main(["issue", "SA-123", "--do-not-persist"])
            _args, kwargs = mock_cmd.call_args
            assert kwargs["max_citations_per_ac"] == (
                audit_runner._DEFAULT_MAX_CITATIONS_PER_AC
            )

class TestParentTimeoutGuardBehavior:
    """AC3: The elapsed-time guard skips children only in pathological runs;
    the scaled default gives multi-child parents a realistic budget and an
    explicit --parent-timeout override preserves exact override semantics."""

    def _make_runner(self, child_stage="in_review", parent_audit_show=False):
        """Build a mock runner returning a parent with one child.

        *parent_audit_show* makes ``wl audit-show TEST-1`` return a stored
        audit (used by persist=True tests for the readback verification).
        """
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)

            # Parent readback verification (persist=True only): return a stored
            # audit so the readback guard passes.
            if "audit-show" in cmd_str and parent_audit_show and "TEST-1" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "audit": {
                            "rawOutput": "TEST-1 audit report",
                            "auditedAt": "2026-01-01T00:00:00.000Z",
                        },
                    }),
                    stderr="",
                )

            # StatusLifecycle.show -> wl show <id> --json
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )

            # StatusLifecycle.update_status -> wl update <id> --status ...
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )

            # _run_wl -> wl show <id> --children --json
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n- AC1: parent criterion",
                            "status": "in_progress",
                        },
                        "children": [{
                            "id": "CHILD-1",
                            "title": "Child Issue",
                            "status": "completed",
                            "stage": child_stage,
                            "description": "## Acceptance Criteria\n- CAC1: child criterion",
                        }],
                    }),
                    stderr="",
                )

            # Fallback (audit-show etc.) -> no audit data
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _run(self, parent_timeout=None, elapsed=120.0):
        """Run cmd_issue with a fake clock reporting `elapsed` seconds since
        the guard start marker; pi and code quality are mocked.

        Returns (rc, json_payload).
        """
        clock = {"n": 0, "t0": 1000.0}

        def _fake_monotonic():
            clock["n"] += 1
            # Call #1 is the guard start marker (_audit_start); every call
            # after that is `elapsed` seconds in.
            if clock["n"] == 1:
                return clock["t0"]
            return clock["t0"] + elapsed

        pi_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "mocked"}]',
        }

        def _passthrough_phase2(work_item, ac_results, child_results, **kwargs):
            return (ac_results, child_results, True)

        with (
            mock.patch.object(
                audit_runner.time, "monotonic", side_effect=_fake_monotonic
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", return_value=pi_result
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis",
                side_effect=_passthrough_phase2,
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True,
                runner=self._make_runner(),
                json_mode=True,
                parent_timeout=parent_timeout,
                audit_children=True,  # exercises the full per-child flow
            )
        return rc

    def test_default_guard_skips_child_in_pathological_run(self, capsys):
        """AC5/AC3: The scaled default guard (710s for a 1-child parent) still
        trips for a pathological elapsed time (800s), and the skip diagnostic
        names the computed budget and the override."""
        rc = self._run(parent_timeout=None, elapsed=800.0)
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        child = payload["children"][0]
        ac = child["ac_results"][0]
        assert ac["verdict"] == "unmet"
        assert ac["text"] == "Skipped due to audit timeout. Manual audit required."
        assert "(710s budget" in ac["evidence"]
        assert "--parent-timeout" in ac["evidence"]
        assert "AUDIT_PARENT_TIMEOUT" in ac["evidence"]

    def test_override_audits_child_previously_skipped(self, capsys):
        """AC3: With --parent-timeout 600, the same run audits the child."""
        rc = self._run(parent_timeout=600, elapsed=120.0)
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        child = payload["children"][0]
        ac = child["ac_results"][0]
        assert ac["verdict"] == "met"
        assert ac["text"] == "CAC1: child criterion"

    def test_default_guard_attempts_child_auto_audit(self, capsys):
        """AC2 (SA-0MSKB6V5Q007YDHE): With defaults (no override), a run that
        finishes the parent Phase 1 call in a normal time no longer trips the
        guard — with --audit-children the child auto-audit is attempted instead
        of being skipped."""
        clock = {"n": 0, "t0": 1000.0}

        def _fake_monotonic():
            clock["n"] += 1
            if clock["n"] == 1:
                return clock["t0"]
            return clock["t0"] + 30.0

        pi_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "mocked"}]',
        }

        def _passthrough_phase2(work_item, ac_results, child_results, **kwargs):
            return (ac_results, child_results, True)

        triggered: list[str] = []

        def _fake_subprocess_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "audit_runner.py" in cmd_str and "issue" in cmd_str:
                triggered.append(cmd_str)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with (
            mock.patch.object(
                audit_runner.time, "monotonic", side_effect=_fake_monotonic
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", return_value=pi_result
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis",
                side_effect=_passthrough_phase2,
            ),
            mock.patch.object(
                audit_runner.subprocess, "run", side_effect=_fake_subprocess_run
            ),
            mock.patch.object(audit_runner, "persist_audit", return_value=0),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True,
                runner=self._make_runner(parent_audit_show=True),
                json_mode=True,
                parent_timeout=None,
                audit_children=True,
            )

        err = capsys.readouterr().err
        assert rc == 0
        assert "Approaching parent timeout" not in err, (
            "the scaled default guard must not trip right after the parent Phase 1 call"
        )
        assert any("CHILD-1" in c for c in triggered), (
            "the child auto-audit should be attempted under the default guard "
            "when --audit-children is set"
        )

    def test_default_no_cascade_without_audit_children(self, capsys):
        """AC1 (SA-0MSKB6V5Q007YDHE): without --audit-children, a child with
        no fresh audit is NOT auto-triggered — the cascade is opt-in."""
        clock = {"n": 0, "t0": 1000.0}

        def _fake_monotonic():
            clock["n"] += 1
            if clock["n"] == 1:
                return clock["t0"]
            return clock["t0"] + 30.0

        pi_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "mocked"}]',
        }

        def _passthrough_phase2(work_item, ac_results, child_results, **kwargs):
            return (ac_results, child_results, True)

        triggered: list[str] = []

        def _fake_subprocess_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "audit_runner.py" in cmd_str and "issue" in cmd_str:
                triggered.append(cmd_str)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with (
            mock.patch.object(
                audit_runner.time, "monotonic", side_effect=_fake_monotonic
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", return_value=pi_result
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis",
                side_effect=_passthrough_phase2,
            ),
            mock.patch.object(
                audit_runner.subprocess, "run", side_effect=_fake_subprocess_run
            ),
            mock.patch.object(audit_runner, "persist_audit", return_value=0),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=True, force=True,
                runner=self._make_runner(parent_audit_show=True),
                json_mode=True,
                parent_timeout=None,
                # audit_children defaults to False — no cascade
            )

        err = capsys.readouterr().err
        assert rc == 0
        assert any("CHILD-1" in c for c in triggered) is False, (
            "without --audit-children no child audit subprocess is spawned"
        )
        # Parent-first default: the parent passed with no gaps, so the child
        # inherits passed — no cascade and no "not ready" diagnostic.
        assert "Inherited from parent pass" not in err  # stderr has no such marker
        assert "parent-first" in err

class TestNormalizeVerdict:
    """AC1: _normalize_verdict maps model synonyms to the runner vocabulary."""

    def test_pass_maps_to_met(self):
        assert audit_runner._normalize_verdict("pass") == audit_runner.VERDICT_MET

    def test_met_unchanged_and_case_insensitive(self):
        assert audit_runner._normalize_verdict("met") == audit_runner.VERDICT_MET
        assert audit_runner._normalize_verdict("MET") == audit_runner.VERDICT_MET
        assert audit_runner._normalize_verdict("Pass") == audit_runner.VERDICT_MET

    def test_fail_maps_to_unmet(self):
        assert audit_runner._normalize_verdict("fail") == audit_runner.VERDICT_UNMET
        assert audit_runner._normalize_verdict("failed") == audit_runner.VERDICT_UNMET

    def test_unknown_verdict_passes_through(self):
        assert audit_runner._normalize_verdict("weird") == "weird"

    def test_empty_and_none(self):
        assert audit_runner._normalize_verdict("") == ""
        assert audit_runner._normalize_verdict(None) == ""

class TestCallPiParseNormalizesVerdict:
    """AC4: _call_pi's JSON parse normalizes the verdict field."""

    def _make_mock_popen(self, stdout_text: str):
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (stdout_text, "")
        mock_process.returncode = 0
        return mock_process

    def test_json_parse_normalizes_pass(self):
        """A single-object 'pass' verdict is normalized to 'met'."""
        mock_process = self._make_mock_popen(
            '{"verdict": "pass", "evidence": "ok"}'
        )
        with mock.patch.object(
            audit_runner.subprocess, "Popen", return_value=mock_process
        ), mock.patch.object(
            audit_runner, "extract_pi_text",
            return_value='{"verdict": "pass", "evidence": "ok"}',
        ):
            result = audit_runner._call_pi("test prompt", model="test-model")
        assert result["verdict"] == audit_runner.VERDICT_MET

    def test_json_parse_keeps_met(self):
        mock_process = self._make_mock_popen(
            '{"verdict": "met", "evidence": "ok"}'
        )
        with mock.patch.object(
            audit_runner.subprocess, "Popen", return_value=mock_process
        ), mock.patch.object(
            audit_runner, "extract_pi_text",
            return_value='{"verdict": "met", "evidence": "ok"}',
        ):
            result = audit_runner._call_pi("test prompt", model="test-model")
        assert result["verdict"] == audit_runner.VERDICT_MET

class TestExtractAcs:
    """Direct unit tests for _extract_acs().

    Locks the wrapped-bullet extraction behaviour required by
    SA-0MSIDPN1N001BEQO: all wrapped-bullet ACs are extracted with their
    indented continuation lines folded into the bullet and checkbox markers
    stripped, with no regression to single-line / numbered / heading /
    blank-line behaviour.
    """

    # OSL-0MSCCFH10001E59N-shaped fixture: 6 ``- [ ]`` bullets with indented
    # continuation lines and no blank lines between bullets.
    _OSL_SHAPED_AC_BLOCK = (
        "## Acceptance Criteria\n\n"
        "- [ ] Opening the podcast editor via the replacement workflow (ContextHub\n"
        "      `worklog-selection-list` plugin pane, e.g. `herdr plugin pane open\n"
        "      --plugin worklog-selection-list --entrypoint worklist --placement tab\n"
        "      --focus`) creates a new tab whose label is exactly `Podcast Editing`\n"
        "      (verified via `herdr tab list`).\n"
        "- [ ] The open-and-rename behaviour is implemented in a repo-tracked shell script\n"
        "      under `packages/ContextHub/packages/herdr/scripts/` (following the\n"
        "      `open.sh` pattern) that parses the `tab_id` from `herdr plugin pane open`\n"
        "      JSON output and calls `herdr tab rename <tab-id> \"Podcast Editing\"`.\n"
        "- [ ] The script fails gracefully (clear error, non-zero exit) when the herdr CLI\n"
        "      is unavailable or the `tab_id` cannot be parsed — a missing rename is never\n"
        "      silently skipped.\n"
        "- [ ] A shell unit test (mock herdr CLI, mirroring the ContextHub\n"
        "      `scripts/tests/test_run_in_pane.sh` pattern) covers the parse-and-rename\n"
        "      wiring and passes.\n"
        "- [ ] The herdr config binding that opens the podcast editor (currently\n"
        "      `prefix+l` → `worklog-selection-list.open-worklist`) is updated to invoke\n"
        "      the new script (or a plugin action wrapping it), and the plugin README\n"
        "      documents the keybinding and resulting tab name.\n"
        "- [ ] All related documentation is updated to reflect the changes (README), and\n"
        "      the full project test suite passes with the new changes.\n"
    )

    def test_osl_shaped_wrapped_bullets_all_extracted(self):
        """All 6 OSL-shaped wrapped-bullet ACs are extracted in full."""
        acs = audit_runner._extract_acs(self._OSL_SHAPED_AC_BLOCK)
        assert len(acs) == 6
        assert acs[0] == (
            "Opening the podcast editor via the replacement workflow (ContextHub "
            "`worklog-selection-list` plugin pane, e.g. `herdr plugin pane open "
            "--plugin worklog-selection-list --entrypoint worklist --placement tab "
            "--focus`) creates a new tab whose label is exactly `Podcast Editing` "
            "(verified via `herdr tab list`)."
        )
        assert acs[1] == (
            "The open-and-rename behaviour is implemented in a repo-tracked shell script "
            "under `packages/ContextHub/packages/herdr/scripts/` (following the "
            "`open.sh` pattern) that parses the `tab_id` from `herdr plugin pane open` "
            "JSON output and calls `herdr tab rename <tab-id> \"Podcast Editing\"`."
        )
        assert acs[2] == (
            "The script fails gracefully (clear error, non-zero exit) when the herdr CLI "
            "is unavailable or the `tab_id` cannot be parsed — a missing rename is never "
            "silently skipped."
        )
        assert acs[3] == (
            "A shell unit test (mock herdr CLI, mirroring the ContextHub "
            "`scripts/tests/test_run_in_pane.sh` pattern) covers the parse-and-rename "
            "wiring and passes."
        )
        assert acs[4] == (
            "The herdr config binding that opens the podcast editor (currently "
            "`prefix+l` → `worklog-selection-list.open-worklist`) is updated to invoke "
            "the new script (or a plugin action wrapping it), and the plugin README "
            "documents the keybinding and resulting tab name."
        )
        assert acs[5] == (
            "All related documentation is updated to reflect the changes (README), and "
            "the full project test suite passes with the new changes."
        )

    def test_wrapped_bullet_continuation_folds_into_bullet(self):
        """An indented continuation line is folded into the current bullet."""
        desc = (
            "## Acceptance Criteria\n"
            "- Criterion one wraps onto\n"
            "      a continuation line.\n"
            "- Criterion two.\n"
        )
        assert audit_runner._extract_acs(desc) == [
            "Criterion one wraps onto a continuation line.",
            "Criterion two.",
        ]

    def test_checkbox_markers_stripped_from_bullets(self):
        """Checkbox markers are stripped from extracted bullet ACs."""
        desc = (
            "## Acceptance Criteria\n"
            "- [ ] Unchecked bullet\n"
            "- [x] Lowercase checked\n"
            "- [X] Uppercase checked\n"
            "- [~] In-progress\n"
            "- [-] Closed\n"
            "- Plain bullet\n"
        )
        assert audit_runner._extract_acs(desc) == [
            "Unchecked bullet",
            "Lowercase checked",
            "Uppercase checked",
            "In-progress",
            "Closed",
            "Plain bullet",
        ]

    def test_single_line_bullets_unchanged(self):
        """Single-line bullets still extract (no regression)."""
        desc = (
            "## Acceptance Criteria\n"
            "- Criterion one\n"
            "- Criterion two\n"
        )
        assert audit_runner._extract_acs(desc) == ["Criterion one", "Criterion two"]

    def test_numbered_acs_untouched(self):
        """Numbered ACs keep their text; checkbox stripping never applies."""
        desc = (
            "## Acceptance Criteria\n"
            "1. Criterion one\n"
            "2. Criterion two\n"
        )
        assert audit_runner._extract_acs(desc) == ["Criterion one", "Criterion two"]

    def test_success_criteria_heading(self):
        """Success Criteria headings are extracted exactly like Acceptance Criteria."""
        desc = (
            "## Success Criteria\n"
            "- [ ] Must succeed\n"
            "- [ ] And keep working\n"
        )
        assert audit_runner._extract_acs(desc) == [
            "Must succeed",
            "And keep working",
        ]

    def test_heading_terminates_extraction(self):
        """A later heading terminates extraction (e.g. ## References)."""
        desc = (
            "## Acceptance Criteria\n"
            "- Criterion one\n"
            "- Criterion two\n"
            "\n"
            "## References\n"
            "- something else\n"
        )
        assert audit_runner._extract_acs(desc) == ["Criterion one", "Criterion two"]

    def test_blank_lines_between_bullets_skipped(self):
        """Blank lines between bullets are skipped, not treated as terminators."""
        desc = (
            "## Acceptance Criteria\n"
            "- Criterion one\n"
            "\n"
            "- Criterion two\n"
            "\n"
            "- Criterion three\n"
        )
        assert audit_runner._extract_acs(desc) == [
            "Criterion one",
            "Criterion two",
            "Criterion three",
        ]

    def test_non_indented_prose_terminates_extraction(self):
        """Trailing non-indented prose after the list is not folded in."""
        desc = (
            "## Acceptance Criteria\n"
            "- Criterion one\n"
            "- Criterion two\n"
            "These notes are not part of the criteria list.\n"
        )
        assert audit_runner._extract_acs(desc) == ["Criterion one", "Criterion two"]

    def test_no_acceptance_criteria_fallback(self):
        """Missing criteria section yields the documented fallback string."""
        assert audit_runner._extract_acs(
            "## Summary\nJust prose.\n"
        ) == ["No acceptance criteria defined."]
        assert audit_runner._extract_acs("") == ["No acceptance criteria defined."]

    # ------------------------------------------------------------------
    # Heading variant coverage (SA-0MSJLC8XA00178YD)
    # ------------------------------------------------------------------

    def test_parenthetical_heading(self):
        """Headings with parenthetical suffixes are matched (AC1)."""
        desc = (
            "## Acceptance criteria (testable)\n"
            "\n"
            "1. First criterion\n"
            "2. Second criterion\n"
        )
        acs = audit_runner._extract_acs(desc)
        assert acs == ["First criterion", "Second criterion"]

    def test_no_suffix_no_regression(self):
        """Standard Acceptance Criteria headings still work (AC2)."""
        assert audit_runner._extract_acs(
            "## Acceptance Criteria\n"
            "- [ ] Criterion one\n"
        ) == ["Criterion one"]
        assert audit_runner._extract_acs(
            "## Acceptance Criteria:\n"
            "- [ ] Criterion one\n"
        ) == ["Criterion one"]

    def test_bold_heading(self):
        """Bold-formatted Acceptance Criteria headings are matched (AC4)."""
        desc = (
            "**Acceptance criteria:**\n"
            "\n"
            "- [ ] First criterion\n"
            "- [ ] Second criterion\n"
        )
        acs = audit_runner._extract_acs(desc)
        assert acs == ["First criterion", "Second criterion"]

    def test_angle_bracket_heading(self):
        """Angle-bracket convention headings are matched (AC4)."""
        desc = (
            "<<Acceptance>> <<criteria>>\n"
            "\n"
            "1. First criterion\n"
            "2. Second criterion\n"
        )
        acs = audit_runner._extract_acs(desc)
        assert acs == ["First criterion", "Second criterion"]

class TestCmdIssuePhases:
    """SA-0MSL1ZB5J005ENLI: the decomposed cmd_issue phases are
    independently callable module-level functions operating on a shared
    :class:`_AuditContext` — each is testable in isolation.
    """

    @staticmethod
    def _make_ctx(runner, **overrides):
        defaults = {
            "issue_id": "TEST-1", "persist": False, "timeout": None,
            "parent_timeout": None, "pi_bin": "pi", "model": None,
            "model_source": "default", "runner": runner,
            "json_mode": False, "debug_log": None, "force": False,
            "worklog_dir": None, "batch_phase2": False, "green_run": None,
            "audit_children": False, "max_child_audits": None,
            "run_tests": False,
        }
        defaults.update(overrides)
        return audit_runner._AuditContext(**defaults)

    def test_fetch_phase_syncs_context(self):
        """_phase_fetch_and_cq fills ctx.work_item/children/acs from a single
        --children fetch (no extra fetches)."""
        calls = []

        def _runner(cmd):
            calls.append(list(cmd))
            cs = " ".join(cmd)
            if "--children" in cs:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n- AC1: x",
                        },
                        "children": [{"id": "CHILD-1", "title": "C"}],
                    }),
                    stderr="",
                )
            if "git" in cs:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        ctx = self._make_ctx(_runner)
        with mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [], "fixes_applied": 0},
        ):
            rc = audit_runner._phase_fetch_and_cq(ctx)
        assert rc is None
        assert ctx.work_item["id"] == "TEST-1"
        assert ctx.children[0]["id"] == "CHILD-1"
        assert ctx.acs == ["AC1: x"]
        children_shows = [c for c in calls if "show" in c and "CHILD-1" in c]
        assert children_shows == []

    def test_terminal_lifecycle_restores_on_incomplete_audit(self):
        """_apply_terminal_lifecycle restores the pre-audit state when the
        audit did not complete (never leaves the item in_progress)."""
        updates = []

        def _runner(cmd):
            cs = " ".join(cmd)
            if "update" in cs:
                updates.append(list(cmd))
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        ctx = self._make_ctx(_runner)
        ctx.audit_completed = False
        audit_runner._apply_terminal_lifecycle(ctx)
        assert updates, "expected a terminal wl update"
        last = updates[-1]
        assert "TEST-1" in last
        assert "--status" in last and "open" in last
        assert "--stage" in last and "plan_complete" in last

    # ------------------------------------------------------------------
    # Fallback-tainted 'Yes' verdicts (WL-0MSN7XAUS008WOPQ)
    #
    # A completed 'Ready to close: Yes' run must advance to
    # completed/in_review even when AC evidence was fallback-tainted
    # (read-only test skip, diagnostic 'partial' fallback, etc.). The
    # fallback taint only forces the restore branch for a 'No' verdict
    # (an infra-fallback 'No' is not an explicit model assessment).
    # Genuine infrastructure failures still restore, and a restore of a
    # completed 'Yes' run always prints a visible warning.
    # ------------------------------------------------------------------

    def _run_terminal_lifecycle(self, *, fallback_tainted=False, **ctx_overrides):
        """Run _apply_terminal_lifecycle on a ctx with an update-recording runner.

        *fallback_tainted* sets the ctx's ``ac_fallback_used`` event so the
        infra-fallback provenance flag is visible to the lifecycle.
        """
        updates = []

        def _runner(cmd):
            cs = " ".join(cmd)
            if "update" in cs:
                updates.append(list(cmd))
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        ctx = self._make_ctx(_runner)
        if fallback_tainted:
            ctx.ac_fallback_used.set()
        for key, value in ctx_overrides.items():
            setattr(ctx, key, value)
        audit_runner._apply_terminal_lifecycle(ctx)
        return updates

    def test_terminal_lifecycle_yes_advances_when_fallback_tainted(self):
        """A completed 'Yes' run with the fallback flag set still advances to
        completed/in_review (AC1) — fallback-tainted AC evidence must NOT
        block the advance when the overall verdict is 'Yes'."""
        updates = self._run_terminal_lifecycle(
            fallback_tainted=True,
            audit_verdict="yes",
            audit_completed=True,
            original_status="in_progress",
            original_stage="in_progress",
        )
        assert updates, "expected a terminal wl update"
        last = updates[-1]
        assert "TEST-1" in last
        assert "--status" in last and "completed" in last
        assert "--stage" in last and "in_review" in last

    def test_terminal_lifecycle_yes_fallback_tainted_keeps_done(self):
        """A completed 'Yes' run with the fallback flag set on a terminal
        'done' item keeps the done stage (no stage transition)."""
        updates = self._run_terminal_lifecycle(
            fallback_tainted=True,
            audit_verdict="yes",
            audit_completed=True,
            original_status="completed",
            original_stage="done",
        )
        assert updates, "expected a terminal wl update"
        last = updates[-1]
        assert "--status" in last and "completed" in last
        assert "--stage" not in last  # stage stays 'done'

    def test_terminal_lifecycle_script_failure_restores_pre_audit_state(self):
        """A genuine infrastructure failure (script failure) still restores
        the pre-audit state — unchanged behaviour (AC2)."""
        updates = self._run_terminal_lifecycle(
            audit_verdict="yes",
            audit_completed=True,
            script_failure={
                "script_name": "pi (parent AC review)",
                "reason": "model timeout",
            },
            original_status="in_progress",
            original_stage="in_progress",
        )
        assert updates, "expected a terminal wl update"
        last = updates[-1]
        assert "--status" in last and "in_progress" in last
        assert "--stage" in last and "in_progress" in last
        assert "--assignee" in last and "" in last

    def test_terminal_lifecycle_no_still_demotes(self):
        """A 'No' verdict still demotes to open/plan_complete (AC3)."""
        updates = self._run_terminal_lifecycle(
            audit_verdict="no",
            audit_completed=True,
            original_status="completed",
            original_stage="in_review",
        )
        assert updates, "expected a terminal wl update"
        last = updates[-1]
        assert "--status" in last and "open" in last
        assert "--stage" in last and "plan_complete" in last

    def test_terminal_lifecycle_restore_of_completed_yes_warns(self, capsys):
        """Any restore of a completed 'Yes' run prints a visible warning
        (AC4) — never a silent divergence between the persisted report's
        'Ready to close: Yes' and the item's restored pre-audit state."""
        updates = self._run_terminal_lifecycle(
            audit_verdict="yes",
            audit_completed=True,
            script_failure={
                "script_name": "pi (Phase 2 deep analysis)",
                "reason": "provider error",
            },
            original_status="in_progress",
            original_stage="in_progress",
        )
        assert updates, "expected a terminal wl update"
        last = updates[-1]
        assert "--status" in last and "in_progress" in last
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "Yes" in captured.err

class TestWlShowDedup:
    """SA-0MSL1Z7E9005TLBA: no duplicate ``wl show`` of the same item when
    the data is already in hand.

    cmd_issue used to fetch the parent up to twice without ``--children``
    (once inside the freshness gate's fingerprint computation, once for the
    pre-audit status/stage capture) and re-fetch each child inside
    ``_get_child_audit_verdict`` even though ``wl show --children`` already
    returned the child dict (description + updatedAt). With already-fetched
    data passed through (``_check_audit_freshness`` accepts ``work_item``;
    ``_get_child_audit_verdict`` accepts ``child``), a single fetch is
    reused across the freshness gate and the status capture, and per-child
    freshness reuses the in-hand child dict.
    """

    # Never matches a real fingerprint (sha256 hex) → parent audit is stale
    # by the content gate, so the pipeline runs.
    _STALE_FP = "0" * 64
    _HEAD = "a" * 40

    def _make_runner(self, calls, child_audit=None):
        """Recording runner: appends every invocation to *calls*.

        Serves a stale-fingerprint parent audit (content gate says stale →
        the audit pipeline runs), the ``wl show`` parent fetch, a
        ``--children`` fetch with one open child, optional per-child audits,
        and canned git responses for fingerprint/scope computation.
        """
        def _side_effect(cmd):
            calls.append(list(cmd))
            cmd_str = " ".join(cmd)

            if "audit-show" in cmd_str:
                if "CHILD-1" in cmd_str:
                    audit = child_audit or None
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True, "audit": audit}),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "audit": {
                            "auditedAt": "2026-08-01T00:00:00.000Z",
                            "rawOutput": (
                                f"Ready to close: No\n\n"
                                f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}"
                                f"{self._STALE_FP}\n\n## Summary\nstale"
                            ),
                        },
                    }),
                    stderr="",
                )

            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )

            if "show" in cmd_str and "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n- AC1: parent criterion",
                            "status": "open",
                            "stage": "plan_complete",
                            "updatedAt": "2026-08-01T00:00:00.000Z",
                        },
                        "children": [{
                            "id": "CHILD-1",
                            "title": "Child 1",
                            "status": "open",
                            "stage": "plan_complete",
                            "updatedAt": "2026-08-01T00:00:00.000Z",
                            "description": "## Acceptance Criteria\n- CAC1: child criterion",
                        }],
                    }),
                    stderr="",
                )

            if "show" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n- AC1: parent criterion",
                            "status": "open",
                            "stage": "plan_complete",
                            "updatedAt": "2026-08-01T00:00:00.000Z",
                        },
                    }),
                    stderr="",
                )

            # git fingerprint / scope computation (best-effort, empty OK)
            if cmd_str.startswith("git rev-parse"):
                return SimpleNamespace(
                    returncode=0, stdout=self._HEAD + "\n", stderr="",
                )
            if "git status --porcelain" in cmd_str or "git diff --name-only" in cmd_str:
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        return _side_effect

    def _run_single_child_audit(self, calls, child_audit=None):
        """Run a full cmd_issue for a single-child item; return its rc."""
        runner = self._make_runner(calls, child_audit=child_audit)
        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                return_value={
                    "extracted_text": (
                        '[{"index": 0, "verdict": "met", "evidence": "ok"}]'
                    ),
                    "verdict": "met",
                    "evidence": "ok",
                },
            ),
            mock.patch.object(
                audit_runner, "_assemble_issue_report",
                return_value="Ready to close: Yes\n\n## Summary\nall met",
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=runner,
            )

    def test_parent_show_fetched_once_despite_stale_fingerprint(self):
        """AC1: the parent's ``wl show`` is fetched exactly once even when
        the stale-fingerprint freshness gate must compute the current
        fingerprint (previously a second fetch) and the status capture must
        run (previously a third, when the time gate also fetched)."""
        calls = []
        rc = self._run_single_child_audit(calls)
        assert rc == 0
        parent_shows = [
            c for c in calls if "show" in c and "TEST-1" in c
            and "--children" not in c
        ]
        assert len(parent_shows) == 1, \
            f"expected exactly 1 parent wl show, got {len(parent_shows)}: {parent_shows}"

    def test_child_verdict_reuses_in_hand_child_data(self):
        """AC1: a legacy (fingerprint-less) child audit forces the time
        gate, which used to fetch ``wl show CHILD-1`` for updatedAt; with
        the child dict passed through, no per-child ``wl show`` is issued.
        """
        child_audit = {
            "auditedAt": "2026-08-02T00:00:00.000Z",  # 1 day after updatedAt
            "rawOutput": "Ready to close: Yes\n\n## Summary\nchild audited",
        }
        calls = []
        rc = self._run_single_child_audit(calls, child_audit=child_audit)
        assert rc == 0
        child_shows = [
            c for c in calls if "show" in c and "CHILD-1" in c
        ]
        assert child_shows == [], \
            f"unexpected per-child wl show: {child_shows}"

class TestExtractJsonObject:
    """Tests for _extract_json_object() used by the project-level audit.

    Covers the parsing helper that wires Pi's summary/recommendation JSON
    object into cmd_project (SA-0MSL1YWOG005QAH8).
    """

    def test_parses_bare_json_object(self):
        """A standalone JSON object is parsed and returned."""
        result = audit_runner._extract_json_object('{"summary": "s", "recommendation": "r"}')
        assert result == {"summary": "s", "recommendation": "r"}

    def test_parses_object_after_analysis_text(self):
        """Analysis text before the JSON object does not block parsing."""
        text = (
            "Here is my analysis of the project state.\n"
            '{"summary": "Model summary", "recommendation": "Model rec"}'
        )
        result = audit_runner._extract_json_object(text)
        assert result == {"summary": "Model summary", "recommendation": "Model rec"}

    def test_parses_object_with_markdown_fence(self):
        """A ```json ... ``` fenced object is parsed (trailing fence ignored)."""
        text = '```json\n{"summary": "s", "recommendation": "r"}\n```'
        result = audit_runner._extract_json_object(text)
        assert result == {"summary": "s", "recommendation": "r"}

    def test_parses_nested_object_with_required_keys(self):
        """With required keys, the outer object wins over nested fragments."""
        text = '{"summary": "s", "meta": {"nested": true}, "recommendation": "r"}'
        result = audit_runner._extract_json_object(
            text, required_keys=("summary", "recommendation")
        )
        assert result["recommendation"] == "r"
        assert result["meta"] == {"nested": True}

    def test_required_keys_skip_incidental_fragment(self):
        """A fragment object before the real response is skipped when keys
        are required (the real object appears later / last)."""
        text = (
            '{"fragment": true} then the response '
            '{"summary": "s", "recommendation": "r"}'
        )
        result = audit_runner._extract_json_object(
            text, required_keys=("summary", "recommendation")
        )
        assert result == {"summary": "s", "recommendation": "r"}

    def test_empty_and_none_return_none(self):
        """Empty or None input yields None (no crash)."""
        assert audit_runner._extract_json_object("") is None
        assert audit_runner._extract_json_object(None) is None

    def test_non_json_text_returns_none(self):
        """Free-form text without a JSON object yields None."""
        assert audit_runner._extract_json_object("no json here at all") is None

    def test_array_without_object_returns_none(self):
        """A JSON array without an object inside yields None."""
        assert audit_runner._extract_json_object("[1, 2, 3]") is None
        assert audit_runner._extract_json_object('"just a string"') is None

class TestFormatScriptFailure:
    """Unit tests for the shared _format_script_failure helper (SA-0MSL1Z67Z001ZO87).

    Both ``cmd_issue`` and ``cmd_project`` delegate their nested
    ``_record_script_failure`` closures to this single module-level
    function, so the reason mapping lives in exactly one place.
    """

    def test_generic_exception_uses_str_reason(self):
        """A generic exception keeps its str() as the reason."""
        result = audit_runner._format_script_failure("pi", RuntimeError("boom"))
        assert result == {
            "script_name": "pi",
            "reason": "boom",
            "stderr": "boom",
        }

    def test_timeout_maps_to_readable_reason(self):
        """TimeoutExpired maps to a readable 'Timeout after Ns' reason."""
        exc = subprocess.TimeoutExpired(cmd=["pi"], timeout=120)
        result = audit_runner._format_script_failure("pi", exc)
        assert result["script_name"] == "pi"
        assert result["reason"] == "Timeout after 120s"
        assert "timed out after 120 seconds" in result["stderr"]

    def test_file_not_found_maps_to_filename(self):
        """FileNotFoundError maps to the missing executable filename."""
        exc = FileNotFoundError(2, "No such file or directory", "pi")
        result = audit_runner._format_script_failure("wl", exc)
        assert result["reason"] == "File not found: pi"

class TestCmdProjectPiOutputWiring:
    """cmd_project uses Pi output when parseable and falls back to local values.

    Acceptance criteria (SA-0MSL1YWOG005QAH8):
    1. No Pi model call is made whose output is discarded.
    2. Existing project-audit output format is preserved when the model path
       is unavailable/fails.
    3. Unit tests cover both the wired path and the fallback path.
    """

    _WORK_ITEMS: ClassVar[list[dict[str, str]]] = [
        {"id": "SA-1", "status": "in_progress"},
        {"id": "SA-2", "status": "blocked"},
        {"id": "SA-3", "status": "completed"},
    ]

    _LOCAL_SUMMARY = (
        "Project-level audit: 1 items in progress, 1 blocked, 1 completed."
    )
    _LOCAL_RECOMMENDATION = "Review blocked items SA-2 to unblock progress."

    def _run_project_capture(self, pi_result, json_mode=True, capsys=None):
        with (
            mock.patch.object(audit_runner, "_load_config", return_value={}),
            mock.patch.object(
                audit_runner, "_resolve_model_for_phase", return_value="test-model"
            ),
            mock.patch.object(
                audit_runner,
                "_run_wl",
                return_value={"success": True, "workItems": self._WORK_ITEMS},
            ),
            mock.patch.object(
                audit_runner, "_run_wl_projected", return_value=1
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", return_value=pi_result
            ),
        ):
            rc = audit_runner.cmd_project(
                model="test-model", runner=mock.MagicMock(), json_mode=json_mode
            )
        assert rc == 0
        return capsys.readouterr().out

    def test_wired_path_uses_model_summary_and_recommendation(self, capsys):
        """A parseable Pi JSON object drives the project report (wired path)."""
        pi_result = {
            "verdict": "met",
            "evidence": '{"summary": "Model summary", "recommendation": "Model rec"}',
            "extracted_text": '{"summary": "Model summary", "recommendation": "Model rec"}',
        }
        out = self._run_project_capture(pi_result, json_mode=True, capsys=capsys)
        payload = json.loads(out)
        assert payload["summary"] == "Model summary"
        assert payload["recommendation"] == "Model rec"
        assert payload["ready_to_close"] is False

    def test_wired_path_text_report_contains_model_output(self, capsys):
        """Text-mode report embeds the model summary and recommendation."""
        pi_result = {
            "extracted_text": (
                '{"summary": "Model summary", "recommendation": "Model rec"}'
            ),
        }
        out = self._run_project_capture(pi_result, json_mode=False, capsys=capsys)
        assert "Model summary" in out
        assert "Model rec" in out
        assert "Ready to close: No" in out

    def test_fallback_on_pi_runtime_error(self, capsys):
        """Pi failure (RuntimeError) preserves locally computed values."""
        with (
            mock.patch.object(audit_runner, "_load_config", return_value={}),
            mock.patch.object(
                audit_runner, "_resolve_model_for_phase", return_value="test-model"
            ),
            mock.patch.object(
                audit_runner,
                "_run_wl",
                return_value={"success": True, "workItems": self._WORK_ITEMS},
            ),
            mock.patch.object(
                audit_runner, "_run_wl_projected", return_value=1
            ),
            mock.patch.object(
                audit_runner,
                "_call_pi_and_maybe_log",
                side_effect=RuntimeError("pi binary not found"),
            ),
        ):
            rc = audit_runner.cmd_project(
                model="test-model", runner=mock.MagicMock(), json_mode=True
            )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"] == self._LOCAL_SUMMARY
        assert payload["recommendation"] == self._LOCAL_RECOMMENDATION
        # The recorded failure is surfaced with the mapped reason (AC2).
        assert payload["script_failure"]["script_name"] == "pi (project-level summary)"
        assert payload["script_failure"]["reason"] == "pi binary not found"

    def test_fallback_on_unparseable_pi_output(self, capsys):
        """Unparseable model output preserves locally computed values."""
        pi_result = {"extracted_text": "This is not JSON at all."}
        out = self._run_project_capture(pi_result, json_mode=True, capsys=capsys)
        payload = json.loads(out)
        assert payload["summary"] == self._LOCAL_SUMMARY
        assert payload["recommendation"] == self._LOCAL_RECOMMENDATION

    def test_fallback_on_empty_pi_output(self, capsys):
        """Empty model output (timeout/provider error) preserves local values."""
        pi_result = {"verdict": "unmet", "evidence": "", "extracted_text": ""}
        out = self._run_project_capture(pi_result, json_mode=True, capsys=capsys)
        payload = json.loads(out)
        assert payload["summary"] == self._LOCAL_SUMMARY
        assert payload["recommendation"] == self._LOCAL_RECOMMENDATION

    def test_fallback_on_missing_json_keys(self, capsys):
        """Model JSON missing required keys preserves local values."""
        pi_result = {"extracted_text": '{"summary": "Only a summary here"}'}
        out = self._run_project_capture(pi_result, json_mode=True, capsys=capsys)
        payload = json.loads(out)
        assert payload["summary"] == self._LOCAL_SUMMARY
        assert payload["recommendation"] == self._LOCAL_RECOMMENDATION

class _StallingProcess:
    """Fake Popen whose stdout/stderr are real pipes that never emit output.

    Used by the stall-abort tests (AC4a): a process whose output streams
    stay completely silent while ``poll()`` reports it still running. The
    in-process stall detector must kill it well before the full 1800s
    budget and return a ``_timeout`` verdict.
    """

    def __init__(self):
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        self.stdout = os.fdopen(r1, "rb", buffering=0)
        self.stderr = os.fdopen(r2, "rb", buffering=0)
        self._w1, self._w2 = w1, w2
        self.returncode = None

    def poll(self):
        return None  # never exits

    def kill(self):
        try:
            os.close(self._w1)
            os.close(self._w2)
        except OSError:
            pass

    def communicate(self, timeout=None):
        return "", ""

    def close(self):
        self.stdout.close()
        self.stderr.close()
        for fd in (self._w1, self._w2):
            try:
                os.close(fd)
            except OSError:
                pass

class TestStallAbort:
    """AC4a: a stalled Pi call aborts in-process well before the 1800s budget."""

    def _call_pi_with_stall(self, stall_env: str, effective_timeout: int | None = None) -> tuple[dict, float]:
        proc = _StallingProcess()
        try:
            with mock.patch.object(audit_runner.subprocess, "Popen", return_value=proc), \
                 mock.patch.dict(audit_runner.os.environ, {audit_runner.AUDIT_STALL_TIMEOUT_ENV: stall_env}, clear=False):
                start = time.monotonic()
                kwargs = {"model": "test-model"} if effective_timeout is None else {"model": "test-model", "timeout": effective_timeout}
                result = audit_runner._call_pi("test prompt", **kwargs)
                elapsed = time.monotonic() - start
        finally:
            proc.close()
        return result, elapsed

    def test_stalled_call_aborts_well_before_full_budget(self):
        """A call with no output for the stall threshold aborts in-process.

        With ``AUDIT_STALL_TIMEOUT=1`` the call must return a ``_timeout``
        verdict after ~1s, not after the full 1800s budget.
        """
        result, elapsed = self._call_pi_with_stall("1")
        assert result.get("_timeout") is True
        assert result.get("verdict") == "unmet"
        assert elapsed < 60, f"stalled call took {elapsed:.1f}s; expected abort well before 1800s"
        assert result.get("elapsed_seconds", 0) < 60
        assert "stall" in result.get("evidence", "").lower()

    def test_stall_abort_sets_timeout_marker(self):
        """The stall abort returns the existing ``_timeout`` marker (AC4a)."""
        result, _ = self._call_pi_with_stall("1")
        assert result.get("_timeout") is True
        assert result.get("raw_stdout") is not None

    def test_stall_timeout_env_resolution(self):
        """AUDIT_STALL_TIMEOUT env var is honored; invalid values warn + default."""
        with mock.patch.dict(audit_runner.os.environ, {audit_runner.AUDIT_STALL_TIMEOUT_ENV: "120"}, clear=False):
            assert audit_runner._resolve_stall_timeout() == 120
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            assert audit_runner._resolve_stall_timeout() == 600
        with mock.patch.dict(audit_runner.os.environ, {audit_runner.AUDIT_STALL_TIMEOUT_ENV: "abc"}, clear=False), \
             mock.patch("sys.stderr") as mock_err:
            assert audit_runner._resolve_stall_timeout() == 600
            assert "invalid" in mock_err.write.call_args_list[0][0][0].lower()

class TestChildScreenShortBudget:
    """AC4b: child Phase-1 screens get a short budget; Phase 2 keeps 1800."""

    def _timeout_mock_process(self):
        """Mock Popen whose communicate() raises TimeoutExpired then drains."""
        mock_process = mock.MagicMock()
        timeout_error = subprocess.TimeoutExpired(cmd="pi", timeout=10, output="", stderr="")
        mock_process.communicate.side_effect = [timeout_error, ("", "")]
        return mock_process

    def test_child_screen_uses_short_budget(self):
        """A child Phase-1 screen exceeding its short budget returns a clean timeout verdict.

        The effective per-call budget for a child screen defaults to
        ``_CHILD_SCREEN_TIMEOUT_DEFAULT`` (600s), never the full 1800s.
        """
        mock_process = self._timeout_mock_process()
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process), \
             mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            result = audit_runner._call_pi("test prompt", model="test-model", child_screen=True)
        assert result.get("_timeout") is True
        assert result.get("verdict") == "unmet"
        assert str(audit_runner._CHILD_SCREEN_TIMEOUT_DEFAULT) in result.get("evidence", "")
        assert "1800" not in result.get("evidence", "")

    def test_child_screen_timeout_env_override(self):
        """AUDIT_CHILD_SCREEN_TIMEOUT overrides the default short budget."""
        mock_process = self._timeout_mock_process()
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process), \
             mock.patch.dict(audit_runner.os.environ, {audit_runner.AUDIT_CHILD_SCREEN_TIMEOUT_ENV: "300"}, clear=False):
            result = audit_runner._call_pi("test prompt", model="test-model", child_screen=True)
        assert result.get("_timeout") is True
        assert "300" in result.get("evidence", "")

    def test_phase2_retains_1800_budget(self):
        """Phase 2 (non-child) calls keep the existing 1800s budget."""
        mock_process = self._timeout_mock_process()
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process), \
             mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            result = audit_runner._call_pi("test prompt", model="test-model")
        assert result.get("_timeout") is True
        assert str(audit_runner.CALL_PI_TIMEOUT) in result.get("evidence", "")

    def test_parent_phase1_retains_1800_budget(self):
        """Parent Phase-1 screens (child_screen=False) keep 1800s."""
        mock_process = self._timeout_mock_process()
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process), \
             mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            result = audit_runner._call_pi("test prompt", model="test-model", child_screen=False)
        assert str(audit_runner.CALL_PI_TIMEOUT) in result.get("evidence", "")

    def test_explicit_timeout_arg_wins_over_short_budget(self):
        """An explicit timeout arg still wins over the child-screen default."""
        mock_process = self._timeout_mock_process()
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process), \
             mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            result = audit_runner._call_pi("test prompt", model="test-model", child_screen=True, timeout=3600)
        assert "3600" in result.get("evidence", "")

    def test_child_screen_timeout_constants_defined(self):
        assert audit_runner.AUDIT_CHILD_SCREEN_TIMEOUT_ENV == "AUDIT_CHILD_SCREEN_TIMEOUT"
        assert audit_runner._CHILD_SCREEN_TIMEOUT_DEFAULT == 600

    def test_phase1_review_child_acs_forwards_child_screen(self):
        """_phase1_review_child_acs passes child_screen=True to _call_pi."""
        child = {
            "id": "CHILD-1",
            "title": "Child Issue",
            "description": "## Acceptance Criteria\n1. AC one\n2. AC two",
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}, {"index": 1, "verdict": "met", "evidence": "ok"}]'},
        ) as mock_call, mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"):
            audit_runner._phase1_review_child_acs(
                0, child, "test-model", "pi", None, None,
                mock.MagicMock(), lambda *a, **k: None,
            )
        _args, kwargs = mock_call.call_args
        assert kwargs.get("child_screen") is True

class TestSlotAwareConcurrency:
    """AC4c: dynamic child-call ceiling from proxy slots; fail-open fallback."""

    def _mock_slot_status(self, available, total):
        return mock.patch.object(
            audit_runner, "_query_slot_status", return_value=(available, total),
        )

    def test_dynamic_ceiling_caps_by_available_slots(self):
        """available=2 → ceiling min(2, max); available=1 → 1; available=0 → 1 floor."""
        with self._mock_slot_status(2, 4):
            assert audit_runner._resolve_child_concurrency() == 2
        with self._mock_slot_status(1, 4):
            assert audit_runner._resolve_child_concurrency() == 1
        with self._mock_slot_status(0, 4):
            assert audit_runner._resolve_child_concurrency() == 1  # floor, never 0
        with self._mock_slot_status(8, 8):
            assert audit_runner._resolve_child_concurrency() == 2  # capped by configured max

    def test_fallback_to_static_on_query_failure(self):
        """Query failure (None, None) degrades to the static ceiling (fail-open)."""
        with self._mock_slot_status(None, None):
            assert audit_runner._resolve_child_concurrency() == 2  # AUDIT_PARALLELISM default
        with mock.patch.dict(audit_runner.os.environ, {audit_runner.AUDIT_PARALLELISM_ENV: "3"}, clear=False), \
             self._mock_slot_status(None, None):
            assert audit_runner._resolve_child_concurrency() == 3

    def test_slot_status_query_parses_endpoint_json(self):
        """_query_slot_status parses the proxy /llama/local/status payload."""
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = b'{"available_slots": 2, "total_slots": 4}'
        mock_resp.__enter__.return_value = mock_resp
        with mock.patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            available, total = audit_runner._query_slot_status(url="http://localhost:8000/llama/local/status")
        assert (available, total) == (2, 4)
        mock_open.assert_called_once()
        _args, kwargs = mock_open.call_args
        assert kwargs.get("timeout") == audit_runner.AUDIT_SLOT_STATUS_TIMEOUT

    def test_slot_status_query_fail_open_on_error(self):
        """A failed/erroring slot query returns (None, None) — never raises."""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert audit_runner._query_slot_status(url="http://localhost:8000/llama/local/status") == (None, None)
        with mock.patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert audit_runner._query_slot_status(url="http://x") == (None, None)

    def test_slot_status_constants(self):
        assert audit_runner.AUDIT_SLOT_STATUS_URL_ENV == "AUDIT_SLOT_STATUS_URL"
        assert audit_runner.AUDIT_SLOT_STATUS_URL_DEFAULT.endswith("/llama/local/status")
        assert audit_runner.AUDIT_SLOT_STATUS_TIMEOUT <= 2  # short timeout (1s)

    def test_max_child_concurrency_env(self):
        """AUDIT_MAX_CHILD_CONCURRENCY caps the dynamic ceiling."""
        with mock.patch.dict(audit_runner.os.environ, {audit_runner.AUDIT_MAX_CHILD_CONCURRENCY_ENV: "1"}, clear=False), \
             self._mock_slot_status(4, 4):
            assert audit_runner._resolve_child_concurrency() == 1

class TestProxyModeSerialization:
    """Proxy cheap-mode detection + per-run serialization (SA-0MSN04X2S006ONH0).

    The runner queries ``GET <base>/admin/mode`` at start (fail-open, ~3 s
    timeout, read-only) and, when the mode is exactly ``"cheap"``, forces
    ``AUDIT_PARALLELISM=1`` and ``AUDIT_MAX_CONCURRENCY=1`` for this run.
    """

    @staticmethod
    def _mock_mode_response(body: str, status: int = 200):
        mock_resp = mock.MagicMock()
        mock_resp.status = status
        mock_resp.read.return_value = body.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        return mock.patch("urllib.request.urlopen", return_value=mock_resp)

    def test_proxy_mode_constants(self):
        assert audit_runner.AUDIT_PROXY_BASE_URL_ENV == "AUDIT_PROXY_BASE_URL"
        assert audit_runner.AUDIT_PROXY_BASE_URL_DEFAULT == "http://192.168.0.199:8000"
        assert audit_runner.AUDIT_PROXY_MODE_TIMEOUT <= 3  # short timeout (~3s)

    def test_query_proxy_mode_parses_json(self):
        """GET <base>/admin/mode JSON mode field is parsed."""
        with self._mock_mode_response('{"mode": "fast"}') as mock_open:
            mode = audit_runner._query_proxy_mode("http://proxy:8000")
        assert mode == "fast"
        _args, kwargs = mock_open.call_args
        assert _args[0] == "http://proxy:8000/admin/mode"
        assert kwargs.get("timeout") == audit_runner.AUDIT_PROXY_MODE_TIMEOUT

    def test_query_proxy_mode_cheap(self):
        """Mode 'cheap' is returned as-is."""
        with self._mock_mode_response('{"mode": "cheap"}'):
            assert audit_runner._query_proxy_mode("http://proxy:8000") == "cheap"

    def test_query_proxy_mode_uses_env_base_url(self):
        """AUDIT_PROXY_BASE_URL env overrides the default base URL."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PROXY_BASE_URL_ENV: "http://alt:9000"},
            clear=False,
        ), self._mock_mode_response('{"mode": "fast"}') as mock_open:
            assert audit_runner._query_proxy_mode() == "fast"
        assert mock_open.call_args[0][0] == "http://alt:9000/admin/mode"

    def test_query_proxy_mode_fail_open_on_network_error(self):
        """Unreachable/timeout/error → None (never raises)."""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert audit_runner._query_proxy_mode("http://x") is None
        with mock.patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert audit_runner._query_proxy_mode("http://x") is None

    def test_query_proxy_mode_non_200_returns_none(self):
        """A non-200 response is treated as a failed query (fail-open)."""
        with self._mock_mode_response("oops", status=503):
            assert audit_runner._query_proxy_mode("http://x") is None

    def test_query_proxy_mode_unparseable_returns_none(self):
        """Non-JSON body → None."""
        with self._mock_mode_response("not-json"):
            assert audit_runner._query_proxy_mode("http://x") is None

    def test_query_proxy_mode_missing_mode_field_returns_none(self):
        """JSON without a 'mode' string field → None."""
        with self._mock_mode_response('{"other": 1}'):
            assert audit_runner._query_proxy_mode("http://x") is None
        with self._mock_mode_response('{"mode": 5}'):
            assert audit_runner._query_proxy_mode("http://x") is None

    def test_apply_serialization_cheap_sets_both_env_and_logs(self, capsys):
        """Mode 'cheap' → AUDIT_PARALLELISM=1 and AUDIT_MAX_CONCURRENCY=1 + stderr line."""
        with mock.patch.object(audit_runner, "_query_proxy_mode", return_value="cheap"), \
             mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            audit_runner._apply_proxy_mode_serialization()
            assert audit_runner.os.environ[audit_runner.AUDIT_PARALLELISM_ENV] == "1"
            assert audit_runner.os.environ[audit_runner.ENV_MAX_WORKERS] == "1"
        err = capsys.readouterr().err
        assert "cheap" in err
        assert "AUDIT_PARALLELISM=1" in err

    def test_apply_serialization_fast_leaves_env_unchanged(self, capsys):
        """Mode 'fast' → no env mutation, no log output."""
        with mock.patch.object(audit_runner, "_query_proxy_mode", return_value="fast"), \
             mock.patch.dict(
                 audit_runner.os.environ,
                 {audit_runner.AUDIT_PARALLELISM_ENV: "2", audit_runner.ENV_MAX_WORKERS: "5"},
                 clear=True,
             ):
            audit_runner._apply_proxy_mode_serialization()
            assert audit_runner.os.environ[audit_runner.AUDIT_PARALLELISM_ENV] == "2"
            assert audit_runner.os.environ[audit_runner.ENV_MAX_WORKERS] == "5"
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == ""

    def test_apply_serialization_failure_fail_open_warns(self, capsys):
        """Query failure → no env mutation + warning logged (fail-open)."""
        with mock.patch.object(audit_runner, "_query_proxy_mode", return_value=None), \
             mock.patch.dict(
                 audit_runner.os.environ,
                 {audit_runner.AUDIT_PARALLELISM_ENV: "2", audit_runner.ENV_MAX_WORKERS: "5"},
                 clear=True,
             ):
            audit_runner._apply_proxy_mode_serialization()
            assert audit_runner.os.environ[audit_runner.AUDIT_PARALLELISM_ENV] == "2"
            assert audit_runner.os.environ[audit_runner.ENV_MAX_WORKERS] == "5"
            assert "Warning" in capsys.readouterr().err

class TestStallAndBudgetRegression:
    """AC5: healthy audits complete unchanged with the new knobs."""

    def test_healthy_call_with_default_stall_detection(self):
        """A healthy call producing output promptly is unaffected by stall detection."""
        mock_process = mock.MagicMock()
        inner = json.dumps({"verdict": "met", "evidence": "ok"})
        stdout_text = json.dumps({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "content": inner},
        })
        mock_process.communicate.return_value = (stdout_text, "")
        mock_process.returncode = 0
        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process), \
             mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            result = audit_runner._call_pi("test prompt", model="test-model")
        assert result.get("verdict") == "met"
        assert result.get("_timeout") is None

    def test_full_budget_still_available_for_phase2(self):
        """Phase 2 calls (no child_screen) still resolve to CALL_PI_TIMEOUT=1800."""
        assert audit_runner.CALL_PI_TIMEOUT == 1800
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            assert audit_runner._resolve_call_timeout(None, child_screen=False) == audit_runner.CALL_PI_TIMEOUT
            assert audit_runner._resolve_call_timeout(None, child_screen=True) == audit_runner._CHILD_SCREEN_TIMEOUT_DEFAULT

