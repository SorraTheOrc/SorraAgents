#!/usr/bin/env python3
"""Unit tests for TARGET_PROJECT_ROOT auto-detection in audit_runner.py.

Tests cover:
  - _detect_project_root() git-root detection and fallback
  - _default_debug_log_path() using TARGET_PROJECT_ROOT
  - Code quality invocation passing TARGET_PROJECT_ROOT as project_root
"""  # noqa: EXE001
from __future__ import annotations

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

from skill.audit.scripts import audit_runner

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
    """Tests for _default_debug_log_path() using TARGET_PROJECT_ROOT (AC3)."""

    def test_uses_target_project_root(self):
        """AC3: Debug log path uses TARGET_PROJECT_ROOT, not REPO_ROOT.

        Calls ``_default_debug_log_path`` and asserts the returned path is
        rooted under ``TARGET_PROJECT_ROOT /.worklog / audit_debug_<id>...``.
        """
        log_path = audit_runner._default_debug_log_path("TEST-123", "parent")

        # The path should be TARGET_PROJECT_ROOT / .worklog / audit_debug_<id>.jsonl
        assert log_path.parent == audit_runner.TARGET_PROJECT_ROOT / ".worklog"
        assert log_path.name == "audit_debug_TEST-123.jsonl"

    def test_uses_context_in_filename(self):
        """Verify the context parameter is used when constructing the file name."""
        log_path = audit_runner._default_debug_log_path("CHILD-456", "child")
        assert log_path.parent == audit_runner.TARGET_PROJECT_ROOT / ".worklog"
        assert log_path.name == "audit_debug_CHILD-456.jsonl"

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
                          enable_tools=False, timeout=None):
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
                         debug_log=None, enable_tools=False, timeout=None):
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
                       debug_log=None, enable_tools=False, timeout=None):
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
