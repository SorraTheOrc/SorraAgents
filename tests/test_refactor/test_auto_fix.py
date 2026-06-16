"""Tests for auto-fix of session-introduced code smells.

These tests verify that:
- Auto-fix runs ruff --fix on Python session files
- Auto-fix runs eslint --fix on JS/TS session files
- Empty file lists are handled gracefully
- Missing linters are handled gracefully
- The auto-fix results are correctly reported
- The refactor pipeline integrates auto-fix correctly

Related work item: SA-0MQFWFRMQ007CE7H
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ruff_available() -> dict[str, Any]:
    """Mock probe_linter returning ruff available."""
    return {"available": True, "version": "0.9.0"}


@pytest.fixture
def mock_ruff_unavailable() -> dict[str, Any]:
    """Mock probe_linter returning ruff unavailable."""
    return {"available": False, "version": ""}


@pytest.fixture
def mock_eslint_available() -> dict[str, Any]:
    """Mock probe_linter returning eslint available."""
    return {"available": True, "version": "9.0.0"}


@pytest.fixture
def mock_eslint_unavailable() -> dict[str, Any]:
    """Mock probe_linter returning eslint unavailable."""
    return {"available": False, "version": ""}


# ---------------------------------------------------------------------------
# Helper: side effect for probe_linter in pipeline tests
# ---------------------------------------------------------------------------


def _probe_side_effect(name: str) -> dict[str, Any]:
    """Return available for both ruff and eslint, unavailable for others."""
    if name == "ruff":
        return {"available": True, "version": "0.9.0"}
    elif name == "eslint":
        return {"available": True, "version": "9.0.0"}
    return {"available": False, "version": ""}


# ---------------------------------------------------------------------------
# Tests for auto_fix_files (unit-level)
# ---------------------------------------------------------------------------


class TestAutoFixPythonFiles:
    """Tests for auto-fix on Python files."""

    def test_auto_fix_runs_ruff_on_python_files(self, mock_ruff_available):
        """Auto-fix should invoke ruff check --fix on Python files."""
        # Patch probe_linter where auto_fix_files imports it
        patch_target = "skill.refactor.scripts.refactor.probe_linter"
        with (
            patch(patch_target) as mock_probe,
            patch("subprocess.run") as mock_run,
        ):
            from skill.refactor.scripts.refactor import auto_fix_files

            mock_probe.return_value = mock_ruff_available
            mock_run.return_value = MagicMock(
                spec=subprocess.CompletedProcess,
                returncode=1,
                stdout=json.dumps([
                    {
                        "code": "F401",
                        "filename": "/tmp/test/src/main.py",
                        "location": {"row": 1, "column": 0},
                        "message": "`os` imported but unused",
                        "fix": {"applicability": "safe"},
                    }
                ]),
                stderr="",
            )

            result = auto_fix_files(
                files=["/tmp/test/src/main.py"],
            )

            # Verify ruff was invoked with --fix
            assert mock_run.call_count >= 1
            fix_calls = [
                c for c in mock_run.call_args_list
                if "ruff" in str(c) and "--fix" in str(c)
            ]
            assert len(fix_calls) >= 1, (
                "Expected ruff check --fix to be called"
            )

            # Verify result structure
            assert "fixes_applied" in result
            assert "fixed_findings" in result
            assert result["fixes_applied"] is True

    def test_auto_fix_skips_when_ruff_unavailable(self, mock_ruff_unavailable):
        """Auto-fix should gracefully skip when ruff is not available."""
        patch_target = "skill.refactor.scripts.refactor.probe_linter"
        with patch(patch_target) as mock_probe:
            from skill.refactor.scripts.refactor import auto_fix_files

            mock_probe.return_value = mock_ruff_unavailable

            result = auto_fix_files(
                files=["/tmp/test/src/main.py"],
            )

            assert result["fixes_applied"] is False
            assert result["fixed_findings"] == []

    def test_auto_fix_handles_empty_file_list(self):
        """Auto-fix should return empty results for empty file list."""
        from skill.refactor.scripts.refactor import auto_fix_files

        result = auto_fix_files(files=[])

        assert result["fixes_applied"] is False
        assert result["fixed_findings"] == []

    def test_auto_fix_handles_non_python_files_gracefully(self, mock_ruff_available):
        """Auto-fix should skip non-Python/non-JS files without errors."""
        patch_target = "skill.refactor.scripts.refactor.probe_linter"
        with patch(patch_target) as mock_probe:
            from skill.refactor.scripts.refactor import auto_fix_files

            mock_probe.return_value = mock_ruff_available

            # Only non-Python, non-JS files
            result = auto_fix_files(
                files=["README.md", "data.json", "config.yaml"],
            )

            assert result["fixes_applied"] is False
            assert result["fixed_findings"] == []

    def test_auto_fix_no_fixable_issues(self, mock_ruff_available):
        """Auto-fix should report no fixes when no issues found."""
        patch_target = "skill.refactor.scripts.refactor.probe_linter"
        with (
            patch(patch_target) as mock_probe,
            patch("subprocess.run") as mock_run,
        ):
            from skill.refactor.scripts.refactor import auto_fix_files

            mock_probe.return_value = mock_ruff_available
            mock_run.return_value = MagicMock(
                spec=subprocess.CompletedProcess,
                returncode=0,
                stdout="",
                stderr="",
            )

            result = auto_fix_files(
                files=["/tmp/test/src/main.py"],
            )

            assert result["fixes_applied"] is False
            assert result["fixed_findings"] == []

    def test_auto_fix_reports_fixed_findings(self, mock_ruff_available):
        """Auto-fix should report what was fixed."""
        patch_target = "skill.refactor.scripts.refactor.probe_linter"
        with (
            patch(patch_target) as mock_probe,
            patch("subprocess.run") as mock_run,
        ):
            from skill.refactor.scripts.refactor import auto_fix_files

            mock_probe.return_value = mock_ruff_available
            mock_run.return_value = MagicMock(
                spec=subprocess.CompletedProcess,
                returncode=1,
                stdout=json.dumps([
                    {
                        "code": "F401",
                        "filename": "/tmp/test/src/main.py",
                        "location": {"row": 1, "column": 0},
                        "message": "`os` imported but unused",
                        "fix": {"applicability": "safe"},
                    },
                    {
                        "code": "F841",
                        "filename": "/tmp/test/src/utils.py",
                        "location": {"row": 15, "column": 4},
                        "message": "Local variable `x` is assigned to but never used",
                        "fix": {"applicability": "safe"},
                    },
                ]),
                stderr="",
            )

            result = auto_fix_files(
                files=["/tmp/test/src/main.py", "/tmp/test/src/utils.py"],
            )

            assert result["fixes_applied"] is True
            assert len(result["fixed_findings"]) == 2
            assert result["fixed_findings"][0]["code"] == "F401"
            assert result["fixed_findings"][1]["code"] == "F841"


class TestAutoFixJSFiles:
    """Tests for auto-fix on JS/TS files."""

    def test_auto_fix_runs_eslint_on_js_files(self, mock_eslint_available):
        """Auto-fix should invoke eslint --fix on JS files."""
        patch_target = "skill.refactor.scripts.refactor.probe_linter"
        with (
            patch(patch_target) as mock_probe,
            patch("subprocess.run") as mock_run,
        ):
            from skill.refactor.scripts.refactor import auto_fix_files

            def probe_side_effect(name):
                if name == "ruff":
                    return {"available": False, "version": ""}
                return mock_eslint_available

            mock_probe.side_effect = probe_side_effect
            mock_run.return_value = MagicMock(
                spec=subprocess.CompletedProcess,
                returncode=0,
                stdout=json.dumps([
                    {
                        "filePath": "/tmp/test/src/app.js",
                        "messages": [
                            {
                                "ruleId": "no-unused-vars",
                                "line": 5,
                                "message": "'x' is assigned but never used",
                                "severity": 2,
                                "fix": {},
                            }
                        ],
                    }
                ]),
                stderr="",
            )

            result = auto_fix_files(
                files=["/tmp/test/src/app.js"],
            )

            # Verify eslint was invoked with --fix
            fix_calls = [
                c for c in mock_run.call_args_list
                if "eslint" in str(c) and "--fix" in str(c)
            ]
            assert len(fix_calls) >= 1, (
                "Expected eslint --fix to be called"
            )

            assert "fixes_applied" in result
            assert "fixed_findings" in result


class TestAutoFixIntegration:
    """Tests for auto-fix integration in the refactor pipeline."""

    def test_refactor_pipeline_includes_auto_fix_step(self):
        """refactor_pipeline should include auto-fix step before detection."""
        from skill.refactor.scripts.refactor import refactor_pipeline

        import inspect
        sig = inspect.signature(refactor_pipeline)
        params = list(sig.parameters.keys())

        # The pipeline should have auto_fix as built-in, not a parameter
        # (the user doesn't pass --auto-fix, it just happens automatically)
        assert "auto_fix" not in params, (
            "auto_fix should be built-in, not a parameter"
        )

    def test_refactor_pipeline_runs_auto_fix(self):
        """refactor_pipeline should attempt auto-fix on session files."""
        with (
            patch("skill.refactor.scripts.refactor.detect_session_files") as mock_detect,
            patch("skill.refactor.scripts.refactor.auto_fix_files") as mock_autofix,
            patch("skill.refactor.scripts.refactor.run_smell_detection") as mock_detect_smells,
            patch("skill.refactor.scripts.refactor.remediate_pre_existing") as mock_remediate,
        ):
            from skill.refactor.scripts.refactor import refactor_pipeline

            # Mock session detection returning some files
            mock_detect.return_value = {
                "changed": [{"status": "M", "file": "src/main.py"}],
                "untracked": [],
                "all_files": ["src/main.py"],
            }

            # Mock auto-fix returning no fixes
            mock_autofix.return_value = {
                "fixes_applied": False,
                "fixed_findings": [],
            }

            # Mock smell detection returning empty
            mock_detect_smells.return_value = []

            mock_remediate.return_value = {
                "work_items_created": [],
                "comments_injected": 0,
                "comment_errors": 0,
            }

            report = refactor_pipeline()

            # Verify auto_fix_files was called
            mock_autofix.assert_called_once()

            # Verify pipeline completed successfully
            assert report["success"] is True
            assert report["summary"]["total_smells"] == 0

    def test_refactor_pipeline_reports_auto_fix_results(self):
        """refactor_pipeline should include auto-fix results in report."""
        with (
            patch("skill.refactor.scripts.refactor.detect_session_files") as mock_detect,
            patch("skill.refactor.scripts.refactor.auto_fix_files") as mock_autofix,
            patch("skill.refactor.scripts.refactor.run_smell_detection") as mock_detect_smells,
            patch("skill.refactor.scripts.refactor.remediate_pre_existing") as mock_remediate,
        ):
            from skill.refactor.scripts.refactor import refactor_pipeline

            mock_detect.return_value = {
                "changed": [{"status": "M", "file": "src/main.py"}],
                "untracked": [],
                "all_files": ["src/main.py"],
            }

            # Mock auto-fix having applied some fixes
            mock_autofix.return_value = {
                "fixes_applied": True,
                "fixed_findings": [
                    {
                        "file": "src/main.py",
                        "line": 1,
                        "code": "F401",
                        "message": "`os` imported but unused",
                        "severity": "critical",
                        "source": "linter",
                        "smell_type": "unused_import",
                    }
                ],
            }

            mock_detect_smells.return_value = []
            mock_remediate.return_value = {
                "work_items_created": [],
                "comments_injected": 0,
                "comment_errors": 0,
            }

            report = refactor_pipeline()

            # Verify auto-fix results are in the report
            assert report["auto_fix"]["fixes_applied"] is True
            assert len(report["auto_fix"]["fixed_findings"]) == 1
            assert report["auto_fix"]["fixed_findings"][0]["code"] == "F401"

    def test_refactor_pipeline_auto_fix_counts_in_summary(self):
        """Pipeline summary should include auto-fix counts."""
        with (
            patch("skill.refactor.scripts.refactor.detect_session_files") as mock_detect,
            patch("skill.refactor.scripts.refactor.auto_fix_files") as mock_autofix,
            patch("skill.refactor.scripts.refactor.run_smell_detection") as mock_detect_smells,
            patch("skill.refactor.scripts.refactor.remediate_pre_existing") as mock_remediate,
        ):
            from skill.refactor.scripts.refactor import refactor_pipeline

            mock_detect.return_value = {
                "changed": [{"status": "M", "file": "src/main.py"}],
                "untracked": [],
                "all_files": ["src/main.py"],
            }

            mock_autofix.return_value = {
                "fixes_applied": True,
                "fixed_findings": [
                    {
                        "file": "src/main.py",
                        "line": 1,
                        "code": "F401",
                        "message": "`os` imported but unused",
                        "severity": "critical",
                        "source": "linter",
                        "smell_type": "unused_import",
                    }
                ],
            }

            mock_detect_smells.return_value = []
            mock_remediate.return_value = {
                "work_items_created": [],
                "comments_injected": 0,
                "comment_errors": 0,
            }

            report = refactor_pipeline()

            # Summary should include auto-fix count
            assert report["summary"]["auto_fixed"] == 1


class TestAutoFixEdgeCases:
    """Tests for edge cases in auto-fix."""

    def test_auto_fix_handles_subprocess_failure(self, mock_ruff_available):
        """Auto-fix should handle subprocess errors gracefully."""
        patch_target = "skill.refactor.scripts.refactor.probe_linter"
        with (
            patch(patch_target) as mock_probe,
            patch("subprocess.run") as mock_run,
        ):
            from skill.refactor.scripts.refactor import auto_fix_files

            mock_probe.return_value = mock_ruff_available
            mock_run.side_effect = FileNotFoundError("ruff not found")

            result = auto_fix_files(
                files=["/tmp/test/src/main.py"],
            )

            assert result["fixes_applied"] is False
            assert result["fixed_findings"] == []

    def test_auto_fix_with_mixed_file_types(self, mock_ruff_available, mock_eslint_available):
        """Auto-fix should handle mixed Python and JS files."""
        def mock_probe_linter(name):
            if name == "ruff":
                return {"available": True, "version": "0.9.0"}
            elif name == "eslint":
                return {"available": True, "version": "9.0.0"}
            return {"available": False, "version": ""}

        def mock_subprocess_run(cmd, **kwargs):
            if len(cmd) >= 2 and "ruff" in str(cmd) and "--fix" in str(cmd):
                return MagicMock(
                    spec=subprocess.CompletedProcess,
                    returncode=1,
                    stdout=json.dumps([
                        {
                            "code": "F401",
                            "filename": "/tmp/test/src/main.py",
                            "location": {"row": 1, "column": 0},
                            "message": "`os` imported but unused",
                            "fix": {"applicability": "safe"},
                        }
                    ]),
                    stderr="",
                )
            elif len(cmd) >= 2 and "eslint" in str(cmd) and "--fix" in str(cmd):
                return MagicMock(
                    spec=subprocess.CompletedProcess,
                    returncode=0,
                    stdout=json.dumps([
                        {
                            "filePath": "/tmp/test/src/app.js",
                            "messages": [
                                {
                                    "ruleId": "no-unused-vars",
                                    "line": 5,
                                    "message": "'x' is assigned but never used",
                                    "severity": 2,
                                    "fix": {},
                                }
                            ],
                        }
                    ]),
                    stderr="",
                )
            return MagicMock(
                spec=subprocess.CompletedProcess,
                returncode=0,
                stdout="",
                stderr="",
            )

        with (
            patch("skill.refactor.scripts.refactor.probe_linter", side_effect=mock_probe_linter),
            patch("subprocess.run", side_effect=mock_subprocess_run),
        ):
            from skill.refactor.scripts.refactor import auto_fix_files

            result = auto_fix_files(
                files=["/tmp/test/src/main.py", "/tmp/test/src/app.js"],
            )

            assert result["fixes_applied"] is True
            assert len(result["fixed_findings"]) > 0
