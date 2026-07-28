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

import pytest

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
