#!/usr/bin/env python3
"""Unit tests for TARGET_PROJECT_ROOT auto-detection in audit_runner.py.

Tests cover:
  - _detect_project_root() git-root detection and fallback
  - _default_debug_log_path() using TARGET_PROJECT_ROOT
  - Code quality invocation passing TARGET_PROJECT_ROOT as project_root
"""  # noqa: EXE001
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so the audit_runner module is importable.
# This mirrors the pattern used by tests/conftest.py.
# ---------------------------------------------------------------------------
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

# ===========================================================================
# _detect_project_root unit tests
# ===========================================================================


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


# ===========================================================================
# _default_debug_log_path tests
# ===========================================================================


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


# ===========================================================================
# Code quality invocation tests
# ===========================================================================


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
        # The argument identity check ensures the code uses the module-level
        # constant rather than a copy or other value.


# ===========================================================================
# _call_pi enable_tools parameter tests
# ===========================================================================


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
        includes ``--tools read,bash,grep,find,ls --exclude-tools ask_question``.
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

        Verifies the default command structure matches current behavior.
        """
        mock_process = self._make_mock_popen()

        with mock.patch.object(audit_runner.subprocess, "Popen", return_value=mock_process) as mock_popen:
            # Call with the same signature as existing callers use
            audit_runner._call_pi("test prompt", model="test-model", pi_bin="pi")

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args == ["pi", "-p", "--mode", "json", "--model", "test-model", "test prompt"]

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


# ===========================================================================
# _call_pi_and_maybe_log enable_tools forwarding tests
# ===========================================================================


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


# ===========================================================================
# _run_phase2_deep_analysis enable_tools usage tests
# ===========================================================================


class TestRunPhase2DeepAnalysisEnableTools:
    """Tests for _run_phase2_deep_analysis() using enable_tools=True (AC1-AC5)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1",
                    stage: str = "in_progress",
                    status: str = "open",
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": "Child Issue",
            "stage": stage,
            "status": status,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_parent_deep_analysis_uses_enable_tools_true(self):
        """AC1: Parent deep analysis calls _call_pi_and_maybe_log with enable_tools=True."""
        issue = self._make_issue()
        acs = [self._make_ac(0), self._make_ac(1)]

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}

            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        # Find the parent call (first argument is issue_id)
        parent_calls = [
            call for call in mock_call.call_args_list
            if call[0][0] == "TEST-1" and call[0][1] == "phase2_deep"
        ]
        assert len(parent_calls) >= 1
        _args, kwargs = parent_calls[0]
        assert kwargs.get("enable_tools") is True

    def test_child_deep_analysis_uses_enable_tools_true(self):
        """AC2: Child deep analysis calls _call_pi_and_maybe_log with enable_tools=True."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", ac_count=2)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}

            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        # Find the child call (first argument is child id)
        child_calls = [
            call for call in mock_call.call_args_list
            if call[0][0] == "CHILD-1"
        ]
        assert len(child_calls) >= 1
        _args, kwargs = child_calls[0]
        assert kwargs.get("enable_tools") is True

    def test_phase2_prompt_unchanged(self):
        """AC3: Phase 2 prompt remains appropriate for tools-enabled mode.

        The prompt already asks the model to read files, which is now feasible.
        Verify it still contains the file-reading instructions.
        """
        issue = self._make_issue()
        acs = [self._make_ac(0)]

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}

            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        mock_call.assert_called_once()
        prompt = mock_call.call_args[0][2]  # third positional arg is prompt
        assert "Read the actual implementation files" in prompt
        assert "file:line reference" in prompt

    def test_non_phase2_calls_unchanged(self):
        """AC5: Non-Phase-2 calls (Phase 1, project-level) remain unchanged.

        Verify that _call_pi() and _call_pi_and_maybe_log() default to
        enable_tools=False for non-Phase-2 callers. This is verified by
        the existing TestCallPiEnableTools and TestCallPiAndMaybeLogEnableTools
        tests which confirm the default is False.
        """
        # This is a documentation/coverage test - the actual behavior
        # is verified by the other test classes.
        # Confirm the default is False in both functions.
        with mock.patch.object(audit_runner.subprocess, "Popen") as mock_popen:
            mock_process = mock.MagicMock()
            mock_process.communicate.return_value = ("{}", "")
            mock_popen.return_value = mock_process

            # Phase 1-like call (no enable_tools argument)
            audit_runner._call_pi("test", model="test-model")
            args = mock_popen.call_args[0][0]
            assert "--tools" not in args

            # Phase 1-like call via _call_pi_and_maybe_log
            with mock.patch.object(audit_runner, "_call_pi") as mock_cp:
                mock_cp.return_value = {"verdict": "met", "evidence": ""}
                audit_runner._call_pi_and_maybe_log("PRJ", "project", "test")
                _args, kwargs = mock_cp.call_args
                assert kwargs.get("enable_tools") is False


# ===========================================================================
# Phase 2 graceful timeout handling tests
# ===========================================================================


class TestPhase2TimeoutHandling:
    """Tests for Phase 2 graceful timeout handling (AC1-AC3)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def test_timeout_marks_acs_as_partial(self):
        """AC1: When Phase 2 times out, all ACs are marked 'partial' with timeout evidence."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "AC1"), self._make_ac(1, "AC2")]

        timeout_result = {
            "verdict": "unmet",
            "evidence": "Pi model call timed out after 600s. Manual audit required.",
            "_timeout": True,
            "extracted_text": "",
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=timeout_result,
        ):
            result = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        # The function now returns 3-tuple (acs, children, phase2_completed)
        updated_acs, _, phase2_completed = result

        assert phase2_completed is False
        for ac in updated_acs:
            assert ac["verdict"] == "partial"
            assert "timed out" in ac["evidence"].lower()

    def test_timeout_preserves_metadata(self):
        """AC2: On timeout, AC text is preserved and verdict is 'partial'."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Criterion 1"), self._make_ac(1, "Criterion 2", "unmet")]

        timeout_result = {
            "verdict": "unmet",
            "evidence": "Pi model call timed out after 600s.",
            "_timeout": True,
            "extracted_text": "",
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=timeout_result,
        ):
            updated_acs, _, phase2_completed = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        assert phase2_completed is False
        for i, ac in enumerate(updated_acs):
            assert ac["text"] == acs[i]["text"]  # Original text preserved
            assert ac["verdict"] == "partial"

    def test_successful_return_still_works(self):
        """AC3: Successful Phase 2 still works correctly (backward compat).

        When no timeout occurs, the function should return phase2_completed=True
        and update ACs based on deep analysis results.
        """
        issue = self._make_issue()
        acs = [self._make_ac(0, "AC1")]

        success_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "file.py:10 works"}]',
            "evidence": "",
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=success_result,
        ):
            updated_acs, _, phase2_completed = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[0]["evidence"] == "file.py:10 works"

    def test_child_timeout_handled_gracefully(self):
        """AC4: Child deep analysis timeout marks child ACs as partial."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = {
            "id": "CHILD-1",
            "title": "Child Issue",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC 1", "verdict": "met", "evidence": ""},
            ],
        }

        # First call (parent) succeeds, second call (child) times out
        parent_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "file.py:10 works"}]',
        }

        child_timeout = {
            "_timeout": True,
            "verdict": "unmet",
            "evidence": "timed out",
            "extracted_text": "",
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=[parent_result, child_timeout],
        ):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model",
                )
            )

        # Parent AC should still be updated (from the successful call)
        assert updated_acs[0]["verdict"] == "met"
        # Child AC should be marked partial due to timeout
        assert updated_children[0]["ac_results"][0]["verdict"] == "partial"
        assert "timed out" in updated_children[0]["ac_results"][0]["evidence"].lower()
        # Overall phase2_completed should be False
        assert phase2_completed is False

    def test_call_pi_timeout_constant_increased(self):
        """AC5: CALL_PI_TIMEOUT is increased to accommodate agent-mode Phase 2."""
        assert audit_runner.CALL_PI_TIMEOUT >= 1800, (
            f"CALL_PI_TIMEOUT ({audit_runner.CALL_PI_TIMEOUT}) should be at least 1800s "
            "for agent-mode Phase 2 deep analysis"
        )


# ===========================================================================
# Effective timeout resolution (SA-0MS95HJ0J004IDIW AC2)
# ===========================================================================


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


# ===========================================================================
# Per-call timing instrumentation (SA-0MSAHQZN4004ZFKQ AC1-AC4)
# ===========================================================================


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


class TestPhase2TimingInstrumentation:
    """Tests that Phase 2 parent and child calls emit per-call timings (AC4)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1",
                    stage: str = "in_progress",
                    status: str = "open",
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": "Child Issue",
            "stage": stage,
            "status": status,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_phase2_parent_and_child_emit_timing_lines(self, capsys):
        """AC4: phase2_deep and phase2_child calls emit per-call timing lines to stderr.

        Mocks ``_call_pi`` (so ``_call_pi_and_maybe_log`` runs for real) and
        runs ``_run_phase2_deep_analysis`` with one active child, then asserts
        the stderr output contains per-call timing lines for the parent
        (context ``phase2_deep``) and child (context ``phase2_child:0``) calls.
        """
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", ac_count=1)

        def _fake_call_pi(prompt, model="test-model", pi_bin="pi",
                          enable_tools=False, timeout=None, max_retries=None):
            return {
                "verdict": "met",
                "evidence": "file.py:10 works",
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "file.py:10 works"}]',
                "elapsed_seconds": 3.75,
            }

        with mock.patch.object(audit_runner, "_call_pi", side_effect=_fake_call_pi):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(issue, acs, [child], "test-model")
            )

        captured = capsys.readouterr()
        assert "TEST-1" in captured.err
        assert "phase2_deep" in captured.err
        assert "CHILD-1" in captured.err
        assert "phase2_child:0" in captured.err
        assert "3.75" in captured.err
        # Behavior is unchanged: verdicts still flow through and phase2 completes.
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"


# ===========================================================================
# Phase 2 file-scope manifest (SA-0MSAIXI1E005SZPV AC1-AC4)
# ===========================================================================


class TestPhase2FileScopeManifest:
    """Tests for the Phase 2 file-scope manifest (AC1-AC4).

    The Phase 2 prompt must include a file-scope manifest (Key Files + git
    changed files + repo index) and Phase 1 file:line evidence (P4) so the
    model verifies in-scope files instead of exploring the whole repo.
    """

    def _make_issue(self, issue_id: str = "TEST-1",
                    description: str = "") -> dict:
        return {"id": issue_id, "title": "Test Issue", "description": description}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met", evidence: str = "") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": evidence}

    def _make_git_runner(self, changed: list[str] | None = None,
                         index: list[str] | None = None):
        """Build a mock runner returning canned git outputs."""
        changed = changed or []
        index = index or ["skill/audit/scripts/audit_runner.py",
                          "skill/audit/tests/test_audit_runner.py",
                          "README.md"]
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "--name-only" in cmd_str:
                out = "\n".join(changed)
            elif "--porcelain=v1" in cmd_str:
                out = "\n".join(f" M {f}" for f in changed)
            elif "ls-files" in cmd_str:
                out = "\n".join(index)
            else:
                out = ""
            return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _fake_call(self, captured):
        """Return a side_effect that matches _call_pi_and_maybe_log's signature."""
        def _side_effect(issue_id, context, prompt, model="m", pi_bin="pi",
                         debug_log=None, enable_tools=False, timeout=None, max_retries=None):
            captured["prompt"] = prompt
            return {"extracted_text": "[]"}
        return _side_effect

    def test_prompt_includes_file_scope_manifest(self):
        """AC1/AC2: phase2_deep prompt includes a FILE SCOPE section."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        runner = self._make_git_runner(changed=["skill/audit/SKILL.md"])
        captured = {}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=self._fake_call(captured)):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", runner=runner,
            )

        prompt = captured["prompt"]
        assert "FILE SCOPE" in prompt
        assert "in-scope" in prompt.lower() or "only the files" in prompt.lower()

    def test_prompt_includes_key_files_from_description(self):
        """AC1: Key Files extracted from the work item description appear in the prompt."""
        desc = (
            "## Summary\n\nThing.\n\n"
            "## Key Files (predicted)\n\n"
            "- `skill/audit/scripts/audit_runner.py` — primary\n"
            "- `skill/audit/tests/test_audit_runner.py` — tests\n"
        )
        issue = self._make_issue(description=desc)
        acs = [self._make_ac(0)]
        runner = self._make_git_runner()
        captured = {}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=self._fake_call(captured)):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", runner=runner,
            )

        prompt = captured["prompt"]
        assert "audit_runner.py" in prompt
        assert "test_audit_runner.py" in prompt

    def test_prompt_includes_changed_files_from_git(self):
        """AC1: git changed files appear in the Phase 2 prompt."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        runner = self._make_git_runner(changed=["skill/audit/SKILL.md"])
        captured = {}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=self._fake_call(captured)):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", runner=runner,
            )

        assert "SKILL.md" in captured["prompt"]

    def test_prompt_includes_phase1_evidence_file_lines(self):
        """AC3 (P4): Phase 1 evidence file:line refs are fed forward."""
        issue = self._make_issue()
        acs = [self._make_ac(0, evidence="skill/audit/scripts/audit_runner.py:1608")]
        runner = self._make_git_runner()
        captured = {}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=self._fake_call(captured)):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", runner=runner,
            )

        assert "audit_runner.py:1608" in captured["prompt"]

    def test_manifest_builder_returns_text(self):
        """AC1: _build_file_scope_manifest returns non-empty manifest text."""
        desc = "## Key Files\n\n- `skill/audit/scripts/audit_runner.py`\n"
        issue = self._make_issue(description=desc)
        acs = [self._make_ac(0, evidence="skill/audit/scripts/audit_runner.py:10")]
        runner = self._make_git_runner(changed=["skill/audit/SKILL.md"])

        manifest = audit_runner._build_file_scope_manifest(issue, acs, runner=runner)
        assert "audit_runner.py" in manifest
        assert "SKILL.md" in manifest
        assert "audit_runner.py:10" in manifest

    def test_manifest_builder_graceful_without_git(self):
        """AC4: manifest builder degrades gracefully when git fails."""
        issue = self._make_issue(description="")
        acs = [self._make_ac(0)]
        runner = mock.MagicMock()
        runner.side_effect = RuntimeError("git not available")

        manifest = audit_runner._build_file_scope_manifest(issue, acs, runner=runner)
        assert isinstance(manifest, str)
        assert manifest  # non-empty

    def test_child_prompt_includes_file_scope(self):
        """AC2: child deep-analysis prompts also carry the FILE SCOPE section."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = {
            "id": "CHILD-1",
            "title": "Child Issue",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met",
                 "evidence": "skill/audit/tests/test_audit_runner.py:1"},
            ],
        }
        runner = self._make_git_runner()
        prompts = []

        def _fake_call(issue_id, context, prompt, model="m", pi_bin="pi",
                       debug_log=None, enable_tools=False, timeout=None, max_retries=None):
            prompts.append(prompt)
            return {"extracted_text": "[]"}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_fake_call):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model", runner=runner,
            )

        assert len(prompts) == 2
        assert "FILE SCOPE" in prompts[1]
        assert "test_audit_runner.py" in prompts[1]


# ===========================================================================
# Phase 2 child-verdict reuse (SA-0MSAIXNXF002W7I3 AC1-AC4)
# ===========================================================================


class TestPhase2ChildVerdictReuse:
    """Tests for reusing fresh child audit verdicts in Phase 2 (AC1-AC4).

    When a child's own fresh audit produced a ready verdict
    (``child_audit_ready=True``), the parent Phase 2 must skip the duplicated
    child deep-analysis call and reuse the child's existing verdicts.
    """

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1",
                    child_audit_ready: bool = False,
                    stage: str = "plan_complete",
                    status: str = "open",
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": "Child Issue",
            "stage": stage,
            "status": status,
            "child_audit_ready": child_audit_ready,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_skips_deep_analysis_when_child_audit_ready(self):
        """AC1: no phase2_child call is made for a child_audit_ready=True child."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", child_audit_ready=True)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model",
                )
            )

        # Only the parent phase2_deep call should be made
        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert child_calls == []
        # Parent ACs still processed
        assert updated_acs[0]["verdict"] == "met"
        # Child AC results are preserved (reused), unchanged
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert phase2_completed is True

    def test_runs_deep_analysis_when_child_audit_not_ready(self):
        """AC2: a child_audit_ready=False child still gets parent deep analysis."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", child_audit_ready=False)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert len(child_calls) == 1

    def test_runs_deep_analysis_when_child_audit_ready_missing(self):
        """AC2 (guard): children without child_audit_ready default to analysis.

        Backward compatibility: existing callers that do not populate
        ``child_audit_ready`` must see unchanged behavior.
        """
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1")
        child.pop("child_audit_ready")  # Simulate pre-P2 callers

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert len(child_calls) == 1

    def test_mixed_children_skip_only_ready_ones(self):
        """AC3: only child_audit_ready=True children are skipped in a mixed set."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        ready_child = self._make_child("READY-1", child_audit_ready=True)
        not_ready_child = self._make_child("PENDING-1", child_audit_ready=False)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [ready_child, not_ready_child], "test-model",
            )

        ready_calls = [c for c in mock_call.call_args_list if c[0][0] == "READY-1"]
        pending_calls = [c for c in mock_call.call_args_list if c[0][0] == "PENDING-1"]
        assert ready_calls == []
        assert len(pending_calls) == 1

    def test_completed_done_child_still_skipped(self):
        """AC3 (guard): completed/done children remain exempt regardless of flag."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("DONE-1", child_audit_ready=False,
                                 stage="done", status="completed")

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        done_calls = [c for c in mock_call.call_args_list if c[0][0] == "DONE-1"]
        assert done_calls == []


# ===========================================================================
# Phase 2 parallel child calls (SA-0MSAIXTMS003REBW AC1-AC4)
# ===========================================================================


class TestPhase2ParallelChildCalls:
    """Tests for bounded-concurrency parallel child deep analysis (AC1-AC4)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str, child_audit_ready: bool = False,
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "plan_complete",
            "status": "open",
            "child_audit_ready": child_audit_ready,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_parallel_children_processed_concurrently(self):
        """AC1: multiple child calls run concurrently (not sequentially).

        Uses a real ThreadPoolExecutor with a mock Pi call that blocks on a
        barrier; if calls were sequential, a 2-child run with a 2-worker pool
        would serialize and take ~2x the per-call time.
        """
        import threading
        import time as _time

        issue = self._make_issue()
        acs = [self._make_ac(0)]
        children = [
            self._make_child("C-1"),
            self._make_child("C-2"),
        ]

        started = threading.Barrier(2)  # both child calls must be in-flight to pass

        def _slow_call(issue_id, context, prompt, model="m", pi_bin="pi",
                       debug_log=None, enable_tools=False, timeout=None, max_retries=None):
            if context.startswith("phase2_child"):
                started.wait(timeout=5)  # raises BrokenBarrierError if not concurrent
            return {"extracted_text": "[]"}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV: "2"},
            clear=False,
        ), mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_slow_call):
            _t0 = _time.monotonic()
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model",
                )
            )
            _elapsed = _time.monotonic() - _t0

        # Both calls completed without deadlock/timeout
        assert phase2_completed is True
        assert len(updated_children) == 2
        # If sequential, elapsed >= 2x barrier overhead; concurrency proves
        # the two calls ran in parallel (barrier would have thrown otherwise).
        assert _elapsed < 10

    def test_sequential_when_parallelism_disabled(self):
        """AC2/fallback: parallelism=1 runs child calls sequentially."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        children = [self._make_child("C-1"), self._make_child("C-2")]
        call_order: list[str] = []

        def _ordered_call(issue_id, context, prompt, model="m", pi_bin="pi",
                          debug_log=None, enable_tools=False, timeout=None, max_retries=None):
            if context.startswith("phase2_child"):
                call_order.append(issue_id)
            return {"extracted_text": "[]"}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV: "1"},
            clear=False,
        ), mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_ordered_call):
            _updated_acs, _updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model",
                )
            )

        assert call_order == ["C-1", "C-2"]  # strictly sequential order
        assert phase2_completed is True

    def test_default_parallelism_cap(self):
        """AC1: a sensible default bounded concurrency cap exists."""
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            cap = audit_runner._resolve_phase2_parallelism()
        assert isinstance(cap, int)
        assert 1 <= cap <= 4

    def test_parallelism_env_var_respected(self):
        """AC1: env var sets the concurrency cap."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV: "3"},
            clear=False,
        ):
            assert audit_runner._resolve_phase2_parallelism() == 3

    def test_invalid_parallelism_env_falls_back(self):
        """AC2: invalid env value falls back to the default cap."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV: "banana"},
            clear=False,
        ):
            cap = audit_runner._resolve_phase2_parallelism()
        assert isinstance(cap, int)
        assert 1 <= cap <= 4

    def test_ready_children_skipped_in_parallel_run(self):
        """AC3: child_audit_ready children are skipped even when parallelism > 1."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        children = [
            self._make_child("READY-1", child_audit_ready=True),
            self._make_child("PENDING-1"),
        ]
        call_ids: list[str] = []

        def _recording_call(issue_id, context, prompt, model="m", pi_bin="pi",
                            debug_log=None, enable_tools=False, timeout=None, max_retries=None):
            call_ids.append(issue_id)
            return {"extracted_text": "[]"}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV: "2"},
            clear=False,
        ), mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_recording_call):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, children, "test-model",
            )

        assert "READY-1" not in call_ids
        assert "PENDING-1" in call_ids

    def test_timeout_child_marks_partial_in_parallel_run(self):
        """AC4: a child timeout still marks partial ACs and phase2_completed=False."""

        issue = self._make_issue()
        acs = [self._make_ac(0)]
        children = [self._make_child("C-1"), self._make_child("C-2")]

        def _call_with_timeout(issue_id, context, prompt, model="m", pi_bin="pi",
                               debug_log=None, enable_tools=False, timeout=None, max_retries=None):
            if issue_id == "C-1":
                return {"_timeout": True, "verdict": "unmet",
                        "evidence": "timed out", "extracted_text": ""}
            return {"extracted_text": "[]"}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV: "2"},
            clear=False,
        ), mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_call_with_timeout):
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model",
                )
            )

        assert phase2_completed is False
        timeout_child = next(c for c in updated_children if c["id"] == "C-1")
        assert timeout_child["ac_results"][0]["verdict"] == "partial"


# ===========================================================================
# Phase 2 retry tuning (SA-0MSAIXZB2007N0F0 AC1-AC4)
# ===========================================================================


class TestPhase2RetryTuning:
    """Tests for bounded provider-error retries on long Phase 2 calls (AC1-AC4).

    Long agent-mode Phase 2 calls (phase2_deep / phase2_child) must NOT
    restart the entire call on provider error beyond a bounded retry cap
    (1, per the performance evaluation). Short Phase 1 bare calls keep the
    existing ``_PI_MAX_RETRIES`` behavior.
    """

    def _make_provider_error_stream(self) -> str:
        return json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant", "stopReason": "error", "errorMessage": "boom"}],
        })

    def _make_valid_stream(self) -> str:
        return json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": '{"verdict": "met", "evidence": "file.py:1"}'}],
        })

    def test_phase2_deep_uses_reduced_retry_cap(self):
        """AC1: phase2_deep calls pass a reduced retry cap (1)."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._call_pi_and_maybe_log(
                "SA-1", "phase2_deep", "prompt",
                model="m", enable_tools=True, max_retries=1,
            )
        _args, kwargs = mock_call.call_args
        assert kwargs.get("max_retries") == 1

    def test_phase2_child_uses_reduced_retry_cap(self):
        """AC1: phase2_child calls pass a reduced retry cap (1)."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._call_pi_and_maybe_log(
                "SA-2", "phase2_child:0", "prompt",
                model="m", enable_tools=True, max_retries=1,
            )
        _args, kwargs = mock_call.call_args
        assert kwargs.get("max_retries") == 1

    def test_default_retries_unchanged_for_phase1(self):
        """AC3: Phase 1 bare calls keep the default _PI_MAX_RETRIES (2)."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._call_pi_and_maybe_log(
                "SA-3", "parent", "prompt", model="m",
            )
        _args, kwargs = mock_call.call_args
        # max_retries not passed → _call_pi uses its default
        assert kwargs.get("max_retries") is None

    def test_call_pi_retries_bounded_by_max_retries(self):
        """AC1/AC2: _call_pi with max_retries=1 makes at most 2 attempts on provider error."""
        provider_stream = self._make_provider_error_stream()
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (provider_stream, "")

        with mock.patch.object(audit_runner.subprocess, "Popen",
                               return_value=mock_process) as mock_popen:
            result = audit_runner._call_pi(
                "prompt", model="m", max_retries=1,
            )

        # 1 initial attempt + 1 retry = 2 attempts, then provider error surfaced
        assert mock_popen.call_count == 2
        assert result.get("_provider_error") is True

    def test_call_pi_default_retries_full_budget(self):
        """AC3: default (no max_retries) keeps _PI_MAX_RETRIES=2 extra attempts."""
        provider_stream = self._make_provider_error_stream()
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (provider_stream, "")

        with mock.patch.object(audit_runner.subprocess, "Popen",
                               return_value=mock_process) as mock_popen:
            result = audit_runner._call_pi("prompt", model="m")

        # 1 initial + 2 retries = 3 attempts
        assert mock_popen.call_count == 3
        assert result.get("_provider_error") is True

    def test_phase2_deep_analysis_forwards_reduced_retries(self):
        """AC1 (integration): _run_phase2_deep_analysis forwards max_retries=1."""
        issue = {"id": "TEST-1", "title": "Test"}
        acs = [{"index": 0, "text": "AC", "verdict": "met", "evidence": ""}]

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        parent_calls = [
            c for c in mock_call.call_args_list
            if c[0][1] == "phase2_deep"
        ]
        assert len(parent_calls) == 1
        _args, kwargs = parent_calls[0]
        assert kwargs.get("max_retries") == 1

    def test_child_provider_error_degrades_to_partial(self):
        """AC2 (child path): a provider error in phase2_child degrades ACs.

        Mirrors the parent phase2_deep path: on _provider_error the child's
        ACs must be marked partial (not left at Phase 1 verdicts), and
        phase2_completed must be False so the audit is not reported as fully
        deep-verified.
        """
        issue = {"id": "TEST-1", "title": "Test"}
        acs = [{"index": 0, "text": "AC", "verdict": "met", "evidence": ""}]
        children = [{
            "id": "CHILD-1",
            "title": "Child",
            "ac_results": [
                {"index": 0, "text": "AC1", "verdict": "met",
                 "evidence": "phase1"}
            ],
        }]

        def _provider_error_call(issue_id, context, prompt, model="m",
                                 pi_bin="pi", debug_log=None,
                                 enable_tools=False, timeout=None,
                                 max_retries=None):
            if context.startswith("phase2_child"):
                return {
                    "verdict": "unmet",
                    "evidence": "Pi provider error: finish_reason: error",
                    "extracted_text": "",
                    "_provider_error": True,
                    "_provider_error_message": "finish_reason: error",
                }
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=_provider_error_call,
        ):
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model",
                )
            )

        assert phase2_completed is False
        child = updated_children[0]
        assert child["id"] == "CHILD-1"
        assert child["ac_results"][0]["verdict"] == "partial"
        assert "provider error" in child["ac_results"][0]["evidence"].lower()


# ===========================================================================
# Verdict-driven status lifecycle tests (SA-0MSAWFTZX003T042)
# ===========================================================================


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


class TestVerdictDrivenStatusLifecycle:
    """Tests for the verdict-driven status transition in cmd_issue's finally.

    The audit runner must leave the work item in a state consistent with its
    audit verdict (SA-0MSAWFTZX003T042):

      - Ready to close: Yes → status=completed, stage=in_review (stage kept
        as 'done' when the item is already in a terminal done stage)
      - Ready to close: No → status=open, stage=plan_complete
      - Failure / unparseable verdict (infrastructure failure) → restore the
        captured pre-audit status/stage + cleared assignee; the item is
        never demoted to open unless the verdict was an explicit No
      - Freshness-gate skip → no lifecycle transitions
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_runner(self, updates, status="open", stage="plan_complete",
                     description="", children=None, fail_children_show=False):
        """Build a mock runner that records every ``wl update`` command.

        Handles the exact ``wl`` command sequence issued by ``cmd_issue``:
        status capture, in_progress claim, children fetch, and the
        verdict-driven terminal transition in the ``finally`` block.
        """
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)

            if "update" in cmd_str:
                updates.append(list(cmd))
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )

            # Original status/stage capture → wl show <id> --json
            if "show" in cmd_str and "--children" not in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": status, "stage": stage},
                    }),
                    stderr="",
                )

            # wl show <id> --children --json (optionally failing)
            if "--children" in cmd_str:
                if fail_children_show:
                    return SimpleNamespace(returncode=1, stdout="", stderr="boom")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": status,
                            "stage": stage,
                        },
                        "children": children or [],
                    }),
                    stderr="",
                )

            # Fallback for any unexpected command
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _run_issue(self, updates, verdict_report, **runner_kwargs):
        """Run cmd_issue with a controlled report verdict and no real subprocesses."""
        mock_runner = self._make_runner(updates, **runner_kwargs)
        with (
            mock.patch.object(
                audit_runner, "_assemble_issue_report",
                return_value=verdict_report,
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

    def _last_update(self, updates):
        """Return the last wl update command recorded (the terminal transition)."""
        assert updates, "expected at least one wl update command"
        return updates[-1]

    # ------------------------------------------------------------------
    # Ready to close: Yes
    # ------------------------------------------------------------------

    def test_ready_yes_sets_completed_in_review(self):
        """AC1: Ready to close: Yes → status=completed, stage=in_review.

        Applies regardless of the pre-audit status (here: in_progress).
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="in_progress", stage="in_progress",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review", "--json",
        ]

    def test_ready_yes_keeps_terminal_done_stage(self):
        """AC1: Ready to close: Yes keeps a pre-existing 'done' stage."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="completed", stage="done",
        )
        last = self._last_update(updates)
        assert "--status" in last and "completed" in last
        assert "--stage" not in last  # stage stays 'done'

    def test_ready_yes_idempotent_on_completed_in_review(self):
        """AC6: Re-auditing a completed/in_review item with Yes stays completed/in_review."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="completed", stage="in_review",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review", "--json",
        ]

    # ------------------------------------------------------------------
    # Ready to close: No
    # ------------------------------------------------------------------

    def test_ready_no_sets_open_plan_complete(self):
        """AC2: Ready to close: No → status=open, stage=plan_complete.

        Applies regardless of the pre-audit status (here: completed/in_review,
        i.e. a failing re-audit demotes the item).
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: No\n\n## Summary\n2 unmet.",
            status="completed", stage="in_review",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "open", "--stage", "plan_complete", "--json",
        ]

    def test_ready_no_moves_open_item_to_plan_complete(self):
        """AC2: No on an already-open item still lands at open/plan_complete."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: No\n\n## Summary\nunmet.",
            status="open", stage="in_progress",
        )
        assert self._last_update(updates) == [
            "wl", "update", "TEST-1",
            "--status", "open", "--stage", "plan_complete", "--json",
        ]

    # ------------------------------------------------------------------
    # Failure / unparseable verdict
    # ------------------------------------------------------------------

    def test_failure_restores_safe_state_and_clears_assignee(self):
        """AC4: On failure the item is never left in_progress; assignee cleared."""
        updates = []
        # wl show --children fails → early exit with script_failure recorded
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes",
            status="open", stage="plan_complete",
            fail_children_show=True,
        )
        last = self._last_update(updates)
        assert "--status" in last and "open" in last
        assert "--stage" in last and "plan_complete" in last
        assert "--assignee" in last and "" in last

    def test_failure_on_in_progress_item_restores_pre_audit_state(self):
        """AC2: An infra failure while the pre-audit status was in_progress
        restores in_progress (assignee cleared) — never demotes to open.
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes",
            status="in_progress", stage="in_progress",
            fail_children_show=True,
        )
        last = self._last_update(updates)
        assert "--status" in last and "in_progress" in last
        assert "--stage" in last and "in_progress" in last
        assert "--assignee" in last and "" in last

    def test_failure_on_in_review_item_keeps_in_review(self):
        """AC2: An infra failure on a completed/in_review item (e.g. a re-audit
        hitting a model timeout) keeps it at completed/in_review so the item is
        not kicked back to the actionable queue.
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes",
            status="completed", stage="in_review",
            fail_children_show=True,
        )
        last = self._last_update(updates)
        assert "--status" in last and "completed" in last
        assert "--stage" in last and "in_review" in last
        assert "--assignee" in last and "" in last

    def test_failure_takes_precedence_over_parseable_yes_report(self):
        """AC7: An infra failure combined with an otherwise-parseable Yes report
        must NOT advance the item — the failure means the audit did not complete
        cleanly, so the verdict cannot be trusted. The item stays at its
        pre-audit state (here in_progress), never completed/in_review.
        """
        updates = []
        self._run_issue(
            updates,
            verdict_report="Ready to close: Yes\n\n## Summary\nAll met.",
            status="in_progress", stage="in_progress",
            fail_children_show=True,
        )
        last = self._last_update(updates)
        assert "--status" in last and "in_progress" in last
        assert "--stage" in last and "in_progress" in last
        assert "--assignee" in last and "" in last
        # The item must NOT advance to completed — failure takes precedence.
        assert "completed" not in last

    def test_unparseable_verdict_falls_back_to_safe_state(self):
        """AC4: An unparseable verdict must not blindly set completed/open."""
        updates = []
        self._run_issue(
            updates,
            verdict_report="## Summary\nNo verdict line present",
            status="completed", stage="in_review",
        )
        last = self._last_update(updates)
        # Restored to the captured pre-audit state, assignee cleared
        assert "--status" in last and "completed" in last
        assert "--stage" in last and "in_review" in last
        assert "--assignee" in last and "" in last

    # ------------------------------------------------------------------
    # Freshness gate skip
    # ------------------------------------------------------------------

    def test_freshness_skip_performs_no_transitions(self):
        """AC5: A fresh audit skips with zero status/stage transitions."""
        updates = []
        mock_runner = self._make_runner(updates)
        with mock.patch.object(
            audit_runner, "_check_audit_freshness",
            return_value="Skipping: audit still fresh\n<existing report>",
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=mock_runner,
            )
        assert rc == 0
        assert updates == []


# ===========================================================================
# Phase 2 prompt scanning-guidance tests (SA-0MSBR0E8Y0022Z4V)
#
# These tests encode the prompt-guidance wiring delivered by
# SA-0MSBR0SRK0035HB1: Phase 2 prompts must reference the bounded scan.py
# helper and forbid unbounded recursive grep / repo-root scans.
# ===========================================================================


class TestPhase2ScanningGuidance:
    """Phase 2 prompts contain scanning guidance (scan.py + no unbounded grep)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _capture_prompt(self, children: list[dict] | None = None,
                        ac_count: int = 1) -> str:
        """Run _run_phase2_deep_analysis and return the parent prompt text."""
        issue = self._make_issue()
        acs = [self._make_ac(i) for i in range(ac_count)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._run_phase2_deep_analysis(
                issue, acs, children or [], "test-model",
            )
        parent_call = [
            call for call in mock_call.call_args_list
            if call[0][1] == "phase2_deep"
        ]
        assert parent_call
        return parent_call[0][0][2]  # prompt is the 3rd positional arg

    def test_parent_prompt_references_scan_helper(self) -> None:
        """The parent phase2_deep prompt references scan.py."""
        prompt = self._capture_prompt()
        assert "scan.py" in prompt

    def test_parent_prompt_forbids_unbounded_recursive_grep(self) -> None:
        """The parent prompt forbids unbounded grep -r over repo root."""
        prompt = self._capture_prompt()
        assert "grep -r" in prompt
        assert "unbounded" in prompt

    def test_child_prompt_references_scan_helper(self) -> None:
        """The child phase2_child prompt references scan.py."""
        child = {
            "id": "CHILD-1", "title": "Child", "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": ""}
            ],
        }
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )
        child_call = [
            call for call in mock_call.call_args_list
            if call[0][1].startswith("phase2_child")
        ]
        assert child_call
        prompt = child_call[0][0][2]
        assert "scan.py" in prompt

    def test_child_prompt_forbids_repo_root_scan(self) -> None:
        """The child prompt forbids unbounded repo-root exploration."""
        child = {
            "id": "CHILD-1", "title": "Child", "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": ""}
            ],
        }
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )
        child_call = [
            call for call in mock_call.call_args_list
            if call[0][1].startswith("phase2_child")
        ]
        prompt = child_call[0][0][2]
        assert "grep" in prompt
        assert "unbounded" in prompt or "explore the whole repository" in prompt
# Cumulative elapsed-time guard configuration (SA-0MSABZO2T004B95X)
# ===========================================================================


class TestParentTimeoutResolution:
    """Tests for parent-timeout resolution (--parent-timeout flag vs env var).

    The cumulative elapsed-time guard (hardcoded 110s before this work item)
    skips remaining child audits when the parent run approaches the parent
    bash-tool timeout. SA-0MSABZO2T004B95X makes that threshold configurable
    via ``--parent-timeout`` / ``AUDIT_PARENT_TIMEOUT``.
    """

    def test_default_guard_constant_preserves_behavior(self):
        """AC1/AC5: The default threshold preserves current behavior (110s)."""
        assert audit_runner.PARENT_TIMEOUT_DEFAULT == 110

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

    def test_default_when_nothing_set(self):
        """AC1/AC5: Returns the default (110s) when neither flag nor env is set."""
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            assert audit_runner._resolve_parent_timeout(None) == audit_runner.PARENT_TIMEOUT_DEFAULT

    def test_invalid_env_value_falls_back_to_default(self):
        """AC1: An invalid AUDIT_PARENT_TIMEOUT value falls back to the default."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARENT_TIMEOUT_ENV: "not-a-number"},
            clear=False,
        ):
            assert audit_runner._resolve_parent_timeout(None) == audit_runner.PARENT_TIMEOUT_DEFAULT

    def test_main_resolves_env_var_parent_timeout(self):
        """AC1: main() resolves the parent timeout from env var and passes it through."""
        with (
            mock.patch.object(audit_runner, "cmd_issue") as mock_cmd,
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


class TestParentTimeoutGuardBehavior:
    """AC3: An explicit --parent-timeout override audits children that the
    default 110s elapsed-time guard would skip."""

    def _make_runner(self, child_stage="in_review"):
        """Build a mock runner returning a parent with one child."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)

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
            )
        return rc

    def test_default_guard_skips_child_after_110s(self, capsys):
        """AC5/AC3: With the default threshold, a child is skipped at 120s elapsed."""
        rc = self._run(parent_timeout=None, elapsed=120.0)
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        child = payload["children"][0]
        ac = child["ac_results"][0]
        assert ac["verdict"] == "unmet"
        assert ac["text"] == "Skipped due to audit timeout. Manual audit required."
        assert "(110s budget)" in ac["evidence"]

    def test_override_audits_child_previously_skipped(self, capsys):
        """AC3: With --parent-timeout 600, the same run audits the child."""
        rc = self._run(parent_timeout=600, elapsed=120.0)
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        child = payload["children"][0]
        ac = child["ac_results"][0]
        assert ac["verdict"] == "met"
        assert ac["text"] == "CAC1: child criterion"


# ===========================================================================
# Phase 2 batch deep analysis (SA-0MSAIY59V001KECF / P6)
# ===========================================================================


class TestPhase2BatchResolution:
    """Tests for batch-mode enablement (env var / default)."""

    def test_env_constant_defined(self):
        """AC1: The AUDIT_PHASE2_BATCH env var constant is defined."""
        assert audit_runner.AUDIT_PHASE2_BATCH_ENV == "AUDIT_PHASE2_BATCH"

    def test_default_disabled(self):
        """AC1/AC5: Batching is off by default (existing N+1 path preserved)."""
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            assert audit_runner._phase2_batch_enabled(None) is False

    def test_env_enables(self):
        """AC1: AUDIT_PHASE2_BATCH=1 enables batching."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_BATCH_ENV: "1"},
            clear=False,
        ):
            assert audit_runner._phase2_batch_enabled(None) is True

    def test_cli_flag_wins_over_env(self):
        """AC1: Explicit --batch-phase2 flag overrides a disabled env value."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_BATCH_ENV: "0"},
            clear=False,
        ):
            assert audit_runner._phase2_batch_enabled(True) is True


class TestPhase2BatchRouting:
    """AC1/AC2/AC4: Batch mode folds parent + pending child ACs into one
    indexed call and routes results back to the correct lists."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str, ac_text: str = "Child AC") -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": ac_text, "verdict": "met", "evidence": "phase1"},
            ],
        }

    def test_single_batch_call_covers_parent_and_child(self):
        """AC1: One phase2_batch call replaces the parent + child calls."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1", "Child AC 1")

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "parent file.py:1"},
                {"index": 1, "verdict": "unmet", "evidence": "child file.py:2"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        # Exactly one call, batched
        assert mock_call.call_count == 1
        context = mock_call.call_args.args[1]
        assert context == "phase2_batch"
        prompt = mock_call.call_args.args[2]
        assert "Parent AC" in prompt
        assert "Child AC 1" in prompt

        # Routing: parent AC got its verdict, child AC got its own
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[0]["evidence"] == "parent file.py:1"
        assert updated_children[0]["ac_results"][0]["verdict"] == "unmet"
        assert "Phase 1" in updated_children[0]["ac_results"][0]["evidence"]
        assert "child file.py:2" in updated_children[0]["ac_results"][0]["evidence"]

    def test_index_routing_multiple_children(self):
        """AC2: Results route per-child by index offset."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC"), self._make_ac(1, "Parent AC 2")]
        children = [
            self._make_child("C-1", "C1 AC"),
            self._make_child("C-2", "C2 AC"),
        ]

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "p0"},
                {"index": 1, "verdict": "adjusted", "evidence": "p1"},
                {"index": 2, "verdict": "unmet", "evidence": "c1"},
                {"index": 3, "verdict": "met", "evidence": "c2"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[1]["verdict"] == "adjusted"
        assert updated_children[0]["ac_results"][0]["verdict"] == "unmet"
        assert updated_children[1]["ac_results"][0]["verdict"] == "met"

    def test_batch_skips_done_and_ready_children(self):
        """AC1: completed/done and child_audit_ready children are not batched."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        done_child = self._make_child("DONE-1", "done AC")
        done_child["status"] = "completed"
        done_child["stage"] = "done"
        ready_child = self._make_child("READY-1", "ready AC")
        ready_child["child_audit_ready"] = True
        pending_child = self._make_child("PEND-1", "pending AC")

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "p"},
                {"index": 1, "verdict": "met", "evidence": "c"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ) as mock_call:
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs,
                    [done_child, ready_child, pending_child],
                    "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        prompt = mock_call.call_args.args[2]
        assert "done AC" not in prompt
        assert "ready AC" not in prompt
        assert "pending AC" in prompt
        # Skipped children keep their Phase 1 results unchanged
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert updated_children[1]["ac_results"][0]["verdict"] == "met"
        assert updated_children[2]["ac_results"][0]["verdict"] == "met"

    def test_batch_verdict_semantics_unchanged(self):
        """AC4: Phase 1 met + Phase 2 met -> met; Phase 1 met + Phase 2 disagree -> downgrade."""
        issue = self._make_issue()
        acs = [
            {"index": 0, "text": "AC1", "verdict": "met", "evidence": "p1"},
            {"index": 1, "text": "AC2", "verdict": "met", "evidence": "p1"},
        ]

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "deep ok"},
                {"index": 1, "verdict": "unmet", "evidence": "deep disagree"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ):
            updated_acs, _, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [], "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[0]["evidence"] == "deep ok"
        assert updated_acs[1]["verdict"] == "unmet"
        assert "Phase 1" in updated_acs[1]["evidence"]
        assert "deep disagree" in updated_acs[1]["evidence"]


class TestPhase2BatchFallback:
    """AC3: Batch failure/timeout falls back to per-child calls."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text") -> dict:
        return {"index": index, "text": text, "verdict": "met", "evidence": ""}

    def _make_child(self, child_id: str) -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": "phase1"},
            ],
        }

    def _run_with_side_effects(self, side_effects):
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")
        return audit_runner._run_phase2_deep_analysis(
            issue, acs, [child], "test-model", batch_phase2=True,
        ), audit_runner._call_pi_and_maybe_log

    def test_batch_timeout_falls_back_to_per_child(self):
        """AC3: A batch timeout falls back to the existing per-child path."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")

        timeout_result = {
            "_timeout": True,
            "verdict": "unmet",
            "evidence": "timed out",
            "extracted_text": "",
        }
        success_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        # Batch call (timeout) + fallback parent call + fallback child call
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=[timeout_result, success_result, success_result],
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )

        # Batch call + fallback per-child call both happened
        contexts = [c.args[1] for c in mock_call.call_args_list]
        assert contexts[0] == "phase2_batch"
        assert any(ctx == "phase2_child:0" for ctx in contexts[1:])
        assert updated_acs[0]["verdict"] == "met"
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert phase2_completed is True

    def test_batch_runtime_error_falls_back(self):
        """AC3: A batch RuntimeError falls back to the per-child path."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")

        def _side_effect(issue_id, context, prompt, **kwargs):
            if context == "phase2_batch":
                raise RuntimeError("batch failed")
            return {
                "extracted_text": json.dumps([
                    {"index": 0, "verdict": "met", "evidence": "file.py:1"},
                ]),
            }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_side_effect
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )

        contexts = [c.args[1] for c in mock_call.call_args_list]
        assert contexts[0] == "phase2_batch"
        assert "phase2_deep" in contexts
        assert "phase2_child:0" in contexts
        assert updated_acs[0]["verdict"] == "met"
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert phase2_completed is True

    def test_batch_disabled_uses_existing_path(self):
        """AC5: With batching disabled the existing parent + child calls run."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")

        success = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=success
        ) as mock_call:
            updated_acs, _updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=False,
                )
            )

        contexts = [c.args[1] for c in mock_call.call_args_list]
        assert "phase2_batch" not in contexts
        assert "phase2_deep" in contexts
        assert "phase2_child:0" in contexts
        assert updated_acs[0]["verdict"] == "met"
        assert phase2_completed is True


# ===========================================================================
# Verdict synonym normalization (SA-0MSDOU2SV006J91X)
# ===========================================================================


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


class TestPhase2NormalizesDeepVerdicts:
    """AC3: Phase 2 deep-analysis verdicts are normalized before merge."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text") -> dict:
        return {"index": index, "text": text, "verdict": "met", "evidence": ""}

    def _make_child(self, child_id: str) -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": "phase1"},
            ],
        }

    def test_batch_path_normalizes_pass_to_met(self):
        """A batch deep verdict of 'pass' merges as 'met' (met+pass -> met)."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")
        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "pass", "evidence": "parent file.py:1"},
                {"index": 1, "verdict": "pass", "evidence": "child file.py:2"},
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == audit_runner.VERDICT_MET
        assert updated_children[0]["ac_results"][0]["verdict"] == audit_runner.VERDICT_MET

    def test_per_child_path_normalizes_pass_to_met(self):
        """The per-child deep path (phase2_child) normalizes 'pass' to 'met'."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")
        success = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }
        child_pass = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "pass", "evidence": "child file.py:2"},
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=[success, child_pass],
        ):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=False,
                )
            )
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == audit_runner.VERDICT_MET
        assert updated_children[0]["ac_results"][0]["verdict"] == audit_runner.VERDICT_MET

    def test_batch_pass_downgrade_still_applies(self):
        """AC3: Phase 1 met + deep 'pass' stays met, but deep 'unmet' still downgrades."""
        issue = self._make_issue()
        acs = [
            {"index": 0, "text": "AC1", "verdict": "met", "evidence": "p1"},
            {"index": 1, "text": "AC2", "verdict": "met", "evidence": "p1"},
        ]
        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "pass", "evidence": "deep ok"},
                {"index": 1, "verdict": "unmet", "evidence": "deep fail"},
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ):
            updated_acs, _, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [], "test-model", batch_phase2=True,
                )
            )
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == audit_runner.VERDICT_MET
        assert updated_acs[1]["verdict"] == audit_runner.VERDICT_UNMET


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
            audit_runner, "_extract_pi_text",
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
            audit_runner, "_extract_pi_text",
            return_value='{"verdict": "met", "evidence": "ok"}',
        ):
            result = audit_runner._call_pi("test prompt", model="test-model")
        assert result["verdict"] == audit_runner.VERDICT_MET


class TestPhase1IntakeNormalizesVerdict:
    """AC2: Phase 1 parent AC review records normalized verdicts.

    Drives ``cmd_issue`` end-to-end with a mocked wl runner and a mocked pi
    returning 'pass' verdicts for the parent AC review; the assembled report
    must show the criteria as met and ready-to-close Yes.
    """

    def _make_mock_runner(self, description: str):
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def test_parent_ac_review_normalizes_pass(self, capsys):
        """A Phase 1 'pass' batch produces met verdicts and Ready to close: Yes."""
        description = (
            "# Test\n\n## Acceptance Criteria\n\n"
            "- AC1: The first criterion\n- AC2: The second criterion\n"
        )
        mock_runner = self._make_mock_runner(description)
        pass_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "pass", "evidence": "file.py:1"},
                {"index": 1, "verdict": "pass", "evidence": "file.py:2"},
            ]),
        }

        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=pass_batch
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality", mock_cq
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        report = capsys.readouterr().out
        assert "Ready to close: Yes" in report
        assert "| 1 |" in report

# ===========================================================================
# Phase 1 performance treatment (P7) — file-scope manifest + SCANNING block,
# read-only tools, bounded parallelism, and child_audit_ready reuse in
# Phase 1 AC review (SA-0MSF3RXU8005CFGD).
# ===========================================================================


_PHASE1_READY_RAW = (
    "Audit report for work item CHILD-1\n"
    "Ready to close: Yes\n\n"
    "## Summary\nChild audit passed.\n"
)


def _phase1_parent_desc(key_file: str | None = None) -> str:
    desc = (
        "# Parent\n\n"
        "## Acceptance Criteria\n\n"
        "- AC1: parent criterion\n"
    )
    if key_file:
        desc += f"\n## Key Files\n- {key_file}\n"
    return desc


def _phase1_child(ci: int, child_id: str = "CHILD-1",
                  stage: str = "in_progress",
                  key_file: str | None = None) -> dict:
    desc = f"## Acceptance Criteria\n1. CAC{ci}: child criterion {ci}\n"
    if key_file:
        desc += f"\n## Key Files\n- {key_file}\n"
    return {
        "id": child_id,
        "title": f"Child {child_id}",
        "status": "in_progress",
        "stage": stage,
        "description": desc,
    }


def _make_phase1_runner(children: list[dict],
                        parent_desc: str | None = None,
                        child_audit_raw: dict | None = None):
    """Mock runner driving cmd_issue through Phase 1 with the given children.

    *child_audit_raw* maps child id -> rawOutput returned by ``wl audit-show``.
    Children absent from the map have no persisted audit and go through the
    Phase 1 child AC review path. Returns (runner, audit_show_call_log).
    """
    if parent_desc is None:
        parent_desc = _phase1_parent_desc()
    audit_shows: list[str] = []

    def _side_effect(cmd):
        cmd_list = list(cmd)
        cmd_str = " ".join(cmd_list)
        if "audit-show" in cmd_list:
            child_id = cmd_list[cmd_list.index("audit-show") + 1]
            audit_shows.append(child_id)
            raw = (child_audit_raw or {}).get(child_id)
            if raw is None:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItemId": child_id,
                    "audit": {
                        "workItemId": child_id,
                        "auditedAt": "2026-07-20T10:00:00.000Z",
                        "rawOutput": raw,
                    },
                }),
                stderr="",
            )
        if "show" in cmd_str and "--children" not in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "TEST-1", "status": "open"},
                }),
                stderr="",
            )
        if "update" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )
        if "--children" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "TEST-1",
                        "description": parent_desc,
                        "status": "in_progress",
                    },
                    "children": children,
                }),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"success": True}),
            stderr="",
        )

    mock_runner = mock.MagicMock()
    mock_runner.side_effect = _side_effect
    return mock_runner, audit_shows


class TestPhase1PromptFileScope:
    """AC1: Phase 1 parent and child AC review prompts include the
    file-scope manifest and SCANNING block (Phase 2 performance pattern)."""

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_parent_prompt_includes_manifest_and_scanning(self, capsys):
        key_file = "skill/audit/scripts/audit_runner.py"
        mock_runner, _audit_shows = _make_phase1_runner(
            [_phase1_child(1)], parent_desc=_phase1_parent_desc(key_file),
        )
        prompts: dict[str, str] = {}

        def _capture(issue_id, context, prompt, **kwargs):
            if context == "parent":
                prompts["parent"] = prompt
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        prompt = prompts["parent"]
        assert "READ-ONLY" in prompt  # existing guard language preserved
        assert "FILE SCOPE" in prompt
        assert "SCANNING" in prompt
        assert "scan.py" in prompt
        assert "list-files" in prompt
        assert key_file in prompt  # Key Files manifest injected

    def test_child_prompt_includes_manifest_and_scanning(self, capsys):
        key_file = "skill/audit/scripts/audit_runner.py"
        child = _phase1_child(1, key_file=key_file)
        mock_runner, _audit_shows = _make_phase1_runner([child])
        prompts: dict[str, str] = {}

        def _capture(issue_id, context, prompt, **kwargs):
            if context == "child:CHILD-1":
                prompts["child"] = prompt
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        prompt = prompts["child"]
        assert "READ-ONLY" in prompt
        assert "FILE SCOPE" in prompt
        assert "SCANNING" in prompt
        assert "scan.py" in prompt
        assert key_file in prompt  # child's Key Files manifest injected


class TestPhase1EnableTools:
    """AC2: Phase 1 parent and child AC review calls run with read-only tools
    (enable_tools=True, which adds --tools read,bash,grep,find,ls)."""

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_parent_and_child_phase1_calls_enable_tools(self, capsys):
        mock_runner, _audit_shows = _make_phase1_runner([_phase1_child(1)])
        seen: dict[str, bool] = {}

        def _capture(issue_id, context, prompt, **kwargs):
            seen[context] = kwargs.get("enable_tools")
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        assert seen.get("parent") is True
        assert seen.get("child:CHILD-1") is True


class TestPhase1ChildAuditReuse:
    """AC3/AC5: ready children skip the Phase 1 child AC review; the
    pre-computed verdict is reused (no second lookup in the auto-trigger
    loop) and their AC results come from their own persisted audit."""

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_ready_child_skips_phase1_review_and_reuses_verdict(self, capsys):
        child = _phase1_child(1, stage="in_review")
        mock_runner, audit_shows = _make_phase1_runner(
            [child], child_audit_raw={"CHILD-1": _PHASE1_READY_RAW},
        )
        contexts: list[str] = []

        def _capture(issue_id, context, prompt, **kwargs):
            contexts.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        # No Phase 1 child AC review call for the ready child.
        assert "child:CHILD-1" not in contexts
        # Verdict computed once in the pre-pass and reused: exactly two
        # audit-show calls (pre-pass verdict + own-audit AC extraction),
        # none from the auto-trigger loop.
        assert audit_shows.count("CHILD-1") == 2
        report = capsys.readouterr().out
        assert "CHILD-1" in report
        assert "CAC1: child criterion" in report

    def test_child_acs_from_own_audit_falls_back_to_met(self):
        child = _phase1_child(1)
        mock_runner, _audit_shows = _make_phase1_runner(
            [], child_audit_raw={"CHILD-1": _PHASE1_READY_RAW},
        )
        acs = audit_runner._child_acs_from_own_audit(child, mock_runner)
        assert len(acs) == 1
        assert acs[0]["text"] == "CAC1: child criterion 1"
        assert acs[0]["verdict"] == "met"
        assert "child's own fresh audit" in acs[0]["evidence"]

    def test_child_acs_from_own_audit_uses_parsed_table(self):
        child = _phase1_child(1)
        raw_with_table = (
            "Audit report for work item CHILD-1\n"
            "Ready to close: Yes\n\n"
            "## Acceptance Criteria Status\n\n"
            "| # | Criterion | Verdict | Evidence |\n"
            "|---|-----------|---------|----------|\n"
            "| 1 | CAC1: child criterion 1 | met | child.py:10 |\n"
        )
        mock_runner, _audit_shows = _make_phase1_runner(
            [], child_audit_raw={"CHILD-1": raw_with_table},
        )
        acs = audit_runner._child_acs_from_own_audit(child, mock_runner)
        assert acs == [
            {"text": "CAC1: child criterion 1", "verdict": "met", "evidence": "child.py:10"}
        ]

    def test_no_audit_child_still_phase1_reviewed(self, capsys):
        """Children without a persisted audit keep the Phase 1 review call."""
        mock_runner, _audit_shows = _make_phase1_runner([_phase1_child(1)])
        contexts: list[str] = []

        def _capture(issue_id, context, prompt, **kwargs):
            contexts.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        assert "child:CHILD-1" in contexts


class TestPhase1ChildParallelism:
    """AC3: pending Phase 1 child AC reviews run with bounded parallelism,
    falling back to sequential execution when parallelism=1."""

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_pending_children_reviewed_concurrently(self, capsys):
        import threading
        import time as _time

        children = [_phase1_child(1, "CHILD-1"), _phase1_child(2, "CHILD-2")]
        mock_runner, _audit_shows = _make_phase1_runner(children)
        started = threading.Barrier(2)  # both child calls must be in-flight

        def _slow(issue_id, context, prompt, **kwargs):
            if context.startswith("child:"):
                started.wait(timeout=5)  # raises if not concurrent
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV: "2"},
            clear=False,
        ), mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_slow
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            _t0 = _time.monotonic()
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )
            _elapsed = _time.monotonic() - _t0

        assert rc == 0
        # If sequential, the barrier would have raised (deadlock/timeout).
        assert _elapsed < 10

    def test_pending_children_sequential_when_parallelism_one(self, capsys):
        children = [_phase1_child(1, "CHILD-1"), _phase1_child(2, "CHILD-2")]
        mock_runner, _audit_shows = _make_phase1_runner(children)
        order: list[str] = []

        def _ordered(issue_id, context, prompt, **kwargs):
            if context.startswith("child:"):
                order.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV: "1"},
            clear=False,
        ), mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_ordered
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        assert order == ["child:CHILD-1", "child:CHILD-2"]


class TestPhase1ParseAuditReportAcs:
    """The persisted-audit AC table parser used to reuse ready children."""

    def test_parses_acceptance_criteria_table(self):
        raw = (
            "Audit report for work item SA-X\n"
            "Ready to close: Yes\n\n"
            "## Acceptance Criteria Status\n\n"
            "| # | Criterion | Verdict | Evidence |\n"
            "|---|-----------|---------|----------|\n"
            "| 1 | AC one | met | file.py:1 |\n"
            "| 2 | AC two | adjusted | file.py:2 — acceptable variance |\n"
        )
        acs = audit_runner._parse_audit_report_acs(raw)
        assert acs == [
            {"text": "AC one", "verdict": "met", "evidence": "file.py:1"},
            {
                "text": "AC two",
                "verdict": "adjusted",
                "evidence": "file.py:2 — acceptable variance",
            },
        ]

    def test_returns_none_when_no_table(self):
        assert (
            audit_runner._parse_audit_report_acs(
                "Ready to close: Yes\n\n## Summary\nOK."
            )
            is None
        )


class TestPhase1ChildWorkerExceptionSafety:
    """The Phase 1 child AC review worker never raises and records failures."""

    def test_worker_records_script_failure_on_pi_error(self):
        child = _phase1_child(1)
        mock_runner, _audit_shows = _make_phase1_runner([])
        failures: list[tuple[str, str]] = []

        def _boom(issue_id, context, prompt, **kwargs):
            raise RuntimeError("pi exploded")

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_boom
        ):
            ci, acs = audit_runner._phase1_review_child_acs(
                0, child,
                resolved_model="test-model",
                pi_bin="pi",
                debug_log=None,
                timeout=None,
                runner=mock_runner,
                script_failure_callback=lambda ctx, exc: failures.append(
                    (ctx, str(exc))
                ),
            )

        assert ci == 0
        assert failures and "child AC review" in failures[0][0]
        # Parse-failure fallback yields a diagnostic 'partial' verdict,
        # matching the sequential Phase 1 child path.
        assert acs[0]["text"] == "CAC1: child criterion 1"
        assert acs[0]["verdict"] == "partial"
