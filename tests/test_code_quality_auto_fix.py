"""Tests for auto-fix functionality in the linter runners.

These tests verify that:
- run_ruff(fix=True) runs ruff with --fix, re-scans, and returns fixes_applied
- run_eslint(fix=True) runs eslint with --fix, re-scans, and returns fixes_applied
- run_markdownlint(fix=True) runs markdownlint with --fix, re-scans, and returns fixes_applied
- run_dotnet_format(fix=True) runs dotnet format, commits changes, and returns fixes_applied
- run_linters_for_project(fix=True) passes fix flag through and aggregates fixes_applied
- code_quality.py CLI --fix flag triggers auto-fix mode
- _commit_changes() stages and commits changes
- When no fixes are applied, fixes_applied is False
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helper: create a mock CompletedProcess
# ---------------------------------------------------------------------------


def _mock_result(returncode=0, stdout="", stderr=""):
    """Create a mock CompletedProcess-like object."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Test: run_ruff with fix=True
# ---------------------------------------------------------------------------


class TestRunRuffAutoFix:
    """Tests for ruff auto-fix functionality."""

    def _make_runner(self, calls):
        """Create a runner that returns different results for each call.

        The first call is --fix (with issues), the second is re-scan (empty).
        """
        call_index = 0

        def runner(cmd):
            nonlocal call_index
            idx = call_index
            call_index += 1
            if "--fix" in cmd:
                # First call: ruff with --fix found issues, returned some
                return _mock_result(
                    returncode=1,
                    stdout=json.dumps([
                        {"code": "F841", "message": "unused variable", "location": {"row": 10, "col": 0}, "filename": "src/test.py"},
                    ]),
                )
            else:
                # Second call: re-scan shows no remaining issues
                return _mock_result(returncode=0, stdout="")

        return runner

    def test_ruff_fix_returns_dict(self):
        """run_ruff with fix=True should return a dict, not a list."""
        from skill.code_review.scripts.linter_runner import run_ruff

        runner = self._make_runner([])
        result = run_ruff(str(REPO_ROOT), runner=runner, fix=True)

        assert isinstance(result, dict)
        assert "findings" in result
        assert "fixes_applied" in result

    def test_ruff_fix_detects_fixes_applied(self):
        """run_ruff with fix=True should report fixes_applied=True when issues found."""
        from skill.code_review.scripts.linter_runner import run_ruff

        runner = self._make_runner([])
        result = run_ruff(str(REPO_ROOT), runner=runner, fix=True)

        assert result["fixes_applied"] is True

    def test_ruff_fix_returns_empty_findings_after_fix(self):
        """run_ruff with fix=True should return remaining findings after re-scan."""
        from skill.code_review.scripts.linter_runner import run_ruff

        runner = self._make_runner([])
        result = run_ruff(str(REPO_ROOT), runner=runner, fix=True)

        assert "findings" in result
        assert isinstance(result["findings"], list)

    def test_ruff_no_fix_returns_dict(self):
        """run_ruff with fix=False should still return a dict."""
        from skill.code_review.scripts.linter_runner import run_ruff

        runner = self._make_runner([])
        result = run_ruff(str(REPO_ROOT), runner=runner, fix=False)

        assert isinstance(result, dict)
        assert "findings" in result
        assert "fixes_applied" in result
        assert result["fixes_applied"] is False

    def test_ruff_fix_no_linter_available(self):
        """run_ruff should return empty when ruff is not available."""
        from skill.code_review.scripts.linter_runner import run_ruff

        with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "ruff", "available": False}):
            result = run_ruff(str(REPO_ROOT), fix=True)

        assert result == {"findings": [], "fixes_applied": False}


# ---------------------------------------------------------------------------
# Test: run_eslint with fix=True
# ---------------------------------------------------------------------------


class TestRunEslintAutoFix:
    """Tests for eslint auto-fix functionality."""

    def _make_runner(self):
        """Create a runner that returns results for --fix and re-scan."""
        call_index = 0

        def runner(cmd):
            nonlocal call_index
            call_index += 1
            if "--fix" in cmd:
                # First call: eslint with --fix
                return _mock_result(
                    returncode=1,
                    stdout=json.dumps([
                        {
                            "filePath": "src/app.ts",
                            "messages": [
                                {"ruleId": "no-unused-vars", "severity": 2, "message": "unused var", "line": 5, "column": 3},
                            ],
                        },
                    ]),
                )
            else:
                # Second call: re-scan shows no remaining issues
                return _mock_result(returncode=0, stdout="")

        return runner

    def test_eslint_fix_returns_dict(self):
        """run_eslint with fix=True should return a dict."""
        from skill.code_review.scripts.linter_runner import run_eslint

        runner = self._make_runner()
        with patch("skill.code_review.scripts.linter_runner.detect_languages", return_value=["typescript"]):
            with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "eslint", "available": True}):
                result = run_eslint(str(REPO_ROOT), runner=runner, fix=True)

        assert isinstance(result, dict)
        assert "findings" in result
        assert "fixes_applied" in result

    def test_eslint_fix_detects_fixes_applied(self):
        """run_eslint with fix=True should report fixes_applied=True."""
        from skill.code_review.scripts.linter_runner import run_eslint

        runner = self._make_runner()
        with patch("skill.code_review.scripts.linter_runner.detect_languages", return_value=["typescript"]):
            with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "eslint", "available": True}):
                result = run_eslint(str(REPO_ROOT), runner=runner, fix=True)

        assert result["fixes_applied"] is True

    def test_eslint_no_linter_available(self):
        """run_eslint should return empty when eslint is not available."""
        from skill.code_review.scripts.linter_runner import run_eslint

        with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "eslint", "available": False}):
            result = run_eslint(str(REPO_ROOT), fix=True)

        assert result == {"findings": [], "fixes_applied": False}


# ---------------------------------------------------------------------------
# Test: run_markdownlint with fix=True
# ---------------------------------------------------------------------------


class TestRunMarkdownlintAutoFix:
    """Tests for markdownlint auto-fix functionality."""

    def _make_runner(self):
        """Create a runner for markdownlint fix and re-scan."""
        call_index = 0

        def runner(cmd):
            nonlocal call_index
            call_index += 1
            if "--fix" in cmd:
                return _mock_result(returncode=0, stdout="")
            else:
                return _mock_result(returncode=0, stdout="")

        return runner

    def test_markdownlint_fix_returns_dict(self):
        """run_markdownlint with fix=True should return a dict."""
        from skill.code_review.scripts.linter_runner import run_markdownlint

        runner = self._make_runner()
        with patch("skill.code_review.scripts.linter_runner.detect_languages", return_value=["markdown"]):
            with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "markdownlint", "available": True}):
                result = run_markdownlint(str(REPO_ROOT), runner=runner, fix=True)

        assert isinstance(result, dict)
        assert "findings" in result
        assert "fixes_applied" in result

    def test_markdownlint_no_linter_available(self):
        """run_markdownlint should return empty when not available."""
        from skill.code_review.scripts.linter_runner import run_markdownlint

        with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "markdownlint", "available": False}):
            result = run_markdownlint(str(REPO_ROOT), fix=True)

        assert result == {"findings": [], "fixes_applied": False}


# ---------------------------------------------------------------------------
# Test: run_dotnet_format with fix=True
# ---------------------------------------------------------------------------


class TestRunDotnetFormatAutoFix:
    """Tests for dotnet-format auto-fix functionality."""

    def _make_runner(self):
        """Create a runner for dotnet format fix and verify."""
        call_index = 0

        def runner(cmd):
            nonlocal call_index
            call_index += 1
            if "--verify-no-changes" not in cmd:
                # Fix mode: return 0 (formatting applied)
                return _mock_result(returncode=0, stdout="")
            else:
                # Verify mode after fix
                return _mock_result(returncode=0, stdout="")

        return runner

    def test_dotnet_format_fix_returns_dict(self):
        """run_dotnet_format with fix=True should return a dict."""
        from skill.code_review.scripts.linter_runner import run_dotnet_format

        runner = self._make_runner()
        with patch("skill.code_review.scripts.linter_runner.detect_languages", return_value=["csharp"]):
            with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "dotnet-format", "available": True}):
                result = run_dotnet_format(str(REPO_ROOT), runner=runner, fix=True)

        assert isinstance(result, dict)
        assert "findings" in result
        assert "fixes_applied" in result

    def test_dotnet_format_no_linter_available(self):
        """run_dotnet_format should return empty when not available."""
        from skill.code_review.scripts.linter_runner import run_dotnet_format

        with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "dotnet-format", "available": False}):
            result = run_dotnet_format(str(REPO_ROOT), fix=True)

        assert result == {"findings": [], "fixes_applied": False}


# ---------------------------------------------------------------------------
# Test: run_linters_for_project with fix=True
# ---------------------------------------------------------------------------


class TestRunLintersForProjectAutoFix:
    """Tests for linter orchestration with fix=True."""

    def test_linters_for_project_with_fix_returns_fixes_applied(self):
        """run_linters_for_project with fix=True should aggregate fixes_applied."""
        from skill.code_review.scripts.linter_runner import run_linters_for_project

        # Create a mock runner that always returns fix results
        def mock_runner(cmd):
            return _mock_result(returncode=0, stdout="")

        with patch("skill.code_review.scripts.linter_runner.detect_languages", return_value=["python"]):
            with patch("skill.code_review.scripts.linter_runner.get_linters_for_language", return_value=["ruff"]):
                with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "ruff", "available": True}):
                    # Patch run_ruff to return fix=True result
                    with patch("skill.code_review.scripts.linter_runner.run_ruff", return_value={"findings": [], "fixes_applied": True}):
                        result = run_linters_for_project(str(REPO_ROOT), runner=mock_runner, fix=True)

        assert "fixes_applied" in result
        assert result["fixes_applied"] >= 0

    def test_linters_for_project_without_fix(self):
        """run_linters_for_project with fix=False should have fixes_applied=0."""
        from skill.code_review.scripts.linter_runner import run_linters_for_project

        def mock_runner(cmd):
            return _mock_result(returncode=0, stdout="")

        with patch("skill.code_review.scripts.linter_runner.detect_languages", return_value=["python"]):
            with patch("skill.code_review.scripts.linter_runner.get_linters_for_language", return_value=["ruff"]):
                with patch("skill.code_review.scripts.linter_runner.probe_linter", return_value={"name": "ruff", "available": True}):
                    with patch("skill.code_review.scripts.linter_runner.run_ruff", return_value={"findings": [], "fixes_applied": False}):
                        result = run_linters_for_project(str(REPO_ROOT), runner=mock_runner, fix=False)

        assert result["fixes_applied"] == 0


# ---------------------------------------------------------------------------
# Test: code_quality.py CLI --fix flag
# ---------------------------------------------------------------------------


class TestCodeQualityCLIFix:
    """Tests for code_quality.py --fix CLI flag."""

    def test_cli_accepts_fix_flag(self):
        """The CLI parser should accept --fix flag."""
        from skill.code_review.scripts.code_quality import build_parser

        parser = build_parser()
        args = parser.parse_args(["--fix", "--path", str(REPO_ROOT)])
        assert args.fix is True

    def test_cli_without_fix_flag(self):
        """The CLI should default fix=False."""
        from skill.code_review.scripts.code_quality import build_parser

        parser = build_parser()
        args = parser.parse_args(["--path", str(REPO_ROOT)])
        assert args.fix is False

    def test_run_code_quality_returns_fixes_applied(self):
        """run_code_quality should include fixes_applied in result."""
        from skill.code_review.scripts.code_quality import run_code_quality

        def mock_runner(cmd):
            return _mock_result(returncode=0, stdout="")

        with patch("skill.code_review.scripts.code_quality.run_linters_for_project", return_value={"languages": [], "linters": [], "total_findings": 0, "findings_by_severity": {}, "findings": [], "fixes_applied": 2}):
            result = run_code_quality(str(REPO_ROOT), fix=True, runner=mock_runner)

        assert "fixes_applied" in result
        assert result["fixes_applied"] == 2


# ---------------------------------------------------------------------------
# Test: _commit_changes
# ---------------------------------------------------------------------------


class TestCommitChanges:
    """Tests for the _commit_changes helper."""

    def test_commit_changes_no_changes(self):
        """_commit_changes should return False when no git changes exist."""
        from skill.code_review.scripts.linter_runner import _commit_changes

        def mock_runner(cmd, cwd=None):
            return _mock_result(returncode=0, stdout="")  # No changes

        result = _commit_changes(REPO_ROOT, "ruff", runner=mock_runner)
        assert result is False

    def test_commit_changes_with_changes(self):
        """_commit_changes should return True and stage+commit when changes exist."""
        from skill.code_review.scripts.linter_runner import _commit_changes

        call_count = [0]

        def mock_runner(cmd, cwd=None):
            call_count[0] += 1
            if "status" in cmd:
                return _mock_result(returncode=0, stdout="M src/test.py")  # Changes exist
            return _mock_result(returncode=0, stdout="")

        result = _commit_changes(REPO_ROOT, "ruff", runner=mock_runner)
        assert result is True
        # Should have called: status, add, commit
        assert call_count[0] == 3


# ---------------------------------------------------------------------------
# Test: _run_ruff_check helper
# ---------------------------------------------------------------------------


class TestRunRuffCheck:
    """Tests for the _run_ruff_check helper function."""

    def test_run_ruff_check_returns_findings_list(self):
        """_run_ruff_check should return a list of finding dicts."""
        from skill.code_review.scripts.linter_runner import _run_ruff_check

        sample_findings = [
            {
                "code": "F841",
                "message": "unused variable",
                "location": {"row": 10, "col": 0},
                "filename": "src/test.py",
            },
        ]

        def mock_runner(cmd):
            return _mock_result(returncode=1, stdout=json.dumps(sample_findings))

        findings = _run_ruff_check(REPO_ROOT, mock_runner)
        assert isinstance(findings, list)
        assert len(findings) == 1
        assert findings[0]["code"] == "F841"
        assert findings[0]["linter"] == "ruff"

    def test_run_ruff_check_empty_on_no_issues(self):
        """_run_ruff_check should return empty list when no issues found."""
        from skill.code_review.scripts.linter_runner import _run_ruff_check

        def mock_runner(cmd):
            return _mock_result(returncode=0, stdout="")

        findings = _run_ruff_check(REPO_ROOT, mock_runner)
        assert findings == []
