"""Scoped eslint must only receive JS/TS file targets (SA-0MUA6W7P1007H2AU).

The audit's code-quality check passes the git changed-file list to the
linters. Previously the eslint path forwarded that list verbatim, so a
Python (``.py``) file in scope was parsed by eslint as JavaScript and
produced a fatal ``Parsing error`` surfaced as a blocking high-severity
finding. These tests pin the extension filter for both the read-only scan
and the fix/rescan path, mirroring the existing ruff guard.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_review.scripts import linter_runner


def _mock_result(returncode: int = 0, stdout: str = "[]", stderr: str = ""):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class _RecordingRunner:
    """Runner stub that records every constructed command."""

    def __init__(self, stdout: str = "[]", returncode: int = 0):
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._returncode = returncode

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return _mock_result(self._returncode, self._stdout)


class TestEslintFindingsCheckFilter:
    """_run_eslint_findings_check filters scoped targets to JS/TS."""

    def test_py_only_scope_returns_empty_without_invoking_eslint(self):
        runner = _RecordingRunner()
        findings = linter_runner._run_eslint_findings_check(
            Path("/repo"), runner, files=["skill/a.py", "tests/test_b.py"]
        )
        assert findings == []
        assert runner.calls == []

    def test_non_js_ts_extensions_never_invoke_eslint(self):
        runner = _RecordingRunner()
        findings = linter_runner._run_eslint_findings_check(
            Path("/repo"), runner, files=["README.md", "notes.txt", "script.py"]
        )
        assert findings == []
        assert runner.calls == []

    def test_mixed_scope_targets_only_js_ts_files(self):
        runner = _RecordingRunner()
        findings = linter_runner._run_eslint_findings_check(
            Path("/repo"), runner, files=["a.py", "b.js", "c.ts"]
        )
        assert findings == []
        assert runner.calls == [
            ["eslint", "b.js", "c.ts", "-f", "json", "--quiet"]
        ]

    def test_whole_project_scope_is_unaffected(self):
        """An empty/absent files list keeps the whole-project invocation."""
        runner = _RecordingRunner()
        linter_runner._run_eslint_findings_check(Path("/repo"), runner, files=None)
        assert runner.calls == [
            ["eslint", "/repo", "-f", "json", "--quiet"]
        ]

        runner = _RecordingRunner()
        linter_runner._run_eslint_findings_check(Path("/repo"), runner, files=[])
        assert runner.calls == [
            ["eslint", "/repo", "-f", "json", "--quiet"]
        ]


class TestEslintFixModeFilter:
    """_run_eslint_fix_mode filters both fix and rescan commands."""

    def test_py_only_scope_returns_empty_without_invoking_eslint(self):
        runner = _RecordingRunner()
        findings, applied = linter_runner._run_eslint_fix_mode(
            Path("/repo"), runner, files=["skill/a.py"]
        )
        assert findings == []
        assert applied is False
        assert runner.calls == []

    def test_mixed_scope_filters_fix_and_rescan_commands(self):
        runner = _RecordingRunner()
        findings, applied = linter_runner._run_eslint_fix_mode(
            Path("/repo"), runner, files=["a.py", "b.js", "c.tsx"]
        )
        assert findings == []
        assert applied is False
        assert runner.calls == [
            ["eslint", "b.js", "c.tsx", "-f", "json", "--fix", "--quiet"],
            ["eslint", "b.js", "c.tsx", "-f", "json", "--quiet"],
        ]
        for call in runner.calls:
            assert not any(target.endswith(".py") for target in call)

    def test_whole_project_fix_mode_is_unaffected(self):
        runner = _RecordingRunner()
        linter_runner._run_eslint_fix_mode(Path("/repo"), runner, files=None)
        assert runner.calls == [
            ["eslint", "/repo", "-f", "json", "--fix", "--quiet"],
            ["eslint", "/repo", "-f", "json", "--quiet"],
        ]


class TestRunEslintScopedRegression:
    """Public run_eslint entry point skips eslint for a .py-only scope."""

    def _patch_detection(self, monkeypatch, languages=("python", "javascript")):
        monkeypatch.setattr(
            linter_runner,
            "probe_linter",
            lambda name: {"name": name, "available": True},
        )
        monkeypatch.setattr(
            linter_runner, "detect_languages", lambda root: list(languages)
        )

    def test_py_only_scope_completes_without_eslint_findings(
        self, tmp_path, monkeypatch
    ):
        self._patch_detection(monkeypatch)
        runner = _RecordingRunner()
        result = linter_runner.run_eslint(
            tmp_path, runner=runner, files=["skill/audit/scripts/scan.py"]
        )
        assert result["findings"] == []
        assert result["fixes_applied"] is False
        # eslint is skipped entirely — no subprocess invocation at all.
        assert runner.calls == []

    def test_mixed_scope_passes_only_js_targets(self, tmp_path, monkeypatch):
        self._patch_detection(monkeypatch)
        runner = _RecordingRunner()
        result = linter_runner.run_eslint(
            tmp_path, runner=runner, files=["a.py", "b.mjs"]
        )
        assert result["findings"] == []
        assert runner.calls == [
            ["eslint", "b.mjs", "-f", "json", "--quiet"]
        ]

    def test_fix_mode_forwards_scoped_files(self, tmp_path, monkeypatch):
        """run_eslint(fix=True) must not silently widen scope to the repo."""
        self._patch_detection(monkeypatch)
        runner = _RecordingRunner()
        result = linter_runner.run_eslint(
            tmp_path, runner=runner, fix=True, files=["a.py", "b.js"]
        )
        assert result["findings"] == []
        assert runner.calls == [
            ["eslint", "b.js", "-f", "json", "--fix", "--quiet"],
            ["eslint", "b.js", "-f", "json", "--quiet"],
        ]
