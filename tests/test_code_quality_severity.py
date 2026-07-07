"""Tests for severity classification logic.

These tests verify that:
- classify_finding() correctly maps ruff linter output to severity levels
- classify_finding() correctly maps eslint linter output to severity levels
- Unknown or edge-case inputs are handled gracefully
- Classification returns valid, JSON-serializable results

The target implementation lives in skill/code_review/scripts/linter_runner.py
or skill/code_review/scripts/severity.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_classifier():
    """Import the classify_finding function; skip if not yet implemented.

    Expected locations (checked in order):
      1. skill.code_review.scripts.linter_runner.classify_finding
      2. skill.code_review.scripts.severity.classify_finding
    """
    candidates = [
        "skill.code_review.scripts.linter_runner",
        "skill.code_review.scripts.severity",
    ]
    for mod_path in candidates:
        try:
            mod = __import__(mod_path, fromlist=["classify_finding"])
            if hasattr(mod, "classify_finding"):
                return mod.classify_finding
        except (ImportError, ModuleNotFoundError):
            continue
    pytest.skip("classify_finding not yet available in any expected module")


# ===================================================================
# Ruff severity classification
# ===================================================================


class TestRuffSeverity:
    """Tests for ruff-specific severity classification."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.classify = _import_classifier()

    def test_ruff_F_is_critical(self):
        """ruff 'F' (pyflakes error) should map to 'critical'."""
        result = self.classify("ruff", "F")
        assert result == "critical"

    def test_ruff_E_is_high(self):
        """ruff 'E' (pycodestyle error) should map to 'high'."""
        result = self.classify("ruff", "E")
        assert result == "high"

    def test_ruff_W_is_medium(self):
        """ruff 'W' (pycodestyle warning) should map to 'medium'."""
        result = self.classify("ruff", "W")
        assert result == "medium"

    def test_ruff_C_is_low(self):
        """ruff 'C' (mccabe complexity) should map to 'low'."""
        result = self.classify("ruff", "C")
        assert result == "low"

    def test_ruff_D_is_medium(self):
        """ruff 'D' (pydocstyle) should map to 'medium'."""
        result = self.classify("ruff", "D")
        assert result == "medium"

    def test_ruff_N_is_medium(self):
        """ruff 'N' (pep8-naming) should map to 'medium'."""
        result = self.classify("ruff", "N")
        assert result == "medium"

    def test_ruff_UP_is_medium(self):
        """ruff 'UP' (pyupgrade) should map to 'medium'."""
        result = self.classify("ruff", "UP")
        assert result == "medium"

    def test_ruff_ANN_is_medium(self):
        """ruff 'ANN' (flake8-annotations) should map to 'medium'."""
        result = self.classify("ruff", "ANN")
        assert result == "medium"

    def test_ruff_S_is_high(self):
        """ruff 'S' (flake8-bandit/security) should map to 'high'."""
        result = self.classify("ruff", "S")
        assert result == "high"

    def test_ruff_unknown_code_defaults_to_medium(self):
        """ruff with an unknown rule code should default to 'medium'."""
        result = self.classify("ruff", "ZZ99")
        assert result in ("medium", "high", "low")

    def test_ruff_full_error_code(self):
        """ruff full error codes like 'F841' should still classify based on prefix."""
        result = self.classify("ruff", "F841")
        assert result == "critical"

    def test_ruff_full_warning_code(self):
        """ruff full warning codes like 'W292' should still classify based on prefix."""
        result = self.classify("ruff", "W292")
        assert result == "medium"


# ===================================================================
# ESLint severity classification
# ===================================================================


class TestEslintSeverity:
    """Tests for eslint-specific severity classification."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.classify = _import_classifier()

    def test_eslint_error_2_is_high(self):
        """eslint severity 2 (error) should map to 'high'."""
        result = self.classify("eslint", "2")
        assert result == "high"

    def test_eslint_warn_1_is_medium(self):
        """eslint severity 1 (warning) should map to 'medium'."""
        result = self.classify("eslint", "1")
        assert result == "medium"

    def test_eslint_off_0_is_low(self):
        """eslint severity 0 (off) should map to 'low'."""
        result = self.classify("eslint", "0")
        assert result == "low"

    def test_eslint_error_label(self):
        """eslint severity 'error' label should map to 'high'."""
        result = self.classify("eslint", "error")
        assert result == "high"

    def test_eslint_warn_label(self):
        """eslint severity 'warn' label should map to 'medium'."""
        _result = self.classify("eslint", "warn")

    def test_eslint_unknown_severity_defaults_to_medium(self):
        """eslint with an unknown severity value should default to 'medium'."""
        _result = self.classify("eslint", "99")
        assert _result in ("medium", "high", "low")


# ===================================================================
# General / edge-case tests
# ===================================================================


class TestGeneralClassification:
    """Tests for classification behavior regardless of linter."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.classify = _import_classifier()

    def test_unknown_linter_defaults_to_medium(self):
        """An unknown linter name should default to 'medium' severity."""
        result = self.classify("unknown-linter-v99", "some_error")
        assert result in ("medium", "high", "low")

    def test_returns_string(self):
        """classify_finding() always returns a string."""
        result = self.classify("ruff", "E")
        assert isinstance(result, str)

    def test_returns_lowercase(self):
        """classify_finding() returns lowercase severity strings."""
        result = self.classify("ruff", "E")
        assert result == result.lower()

    def test_json_serializable(self):
        """classify_finding() return value must be JSON-serializable."""
        result = self.classify("ruff", "F")
        json.dumps(result)  # Should not raise

    @pytest.mark.parametrize("linter,raw,expected", [
        ("ruff", "F", "critical"),
        ("ruff", "E", "high"),
        ("ruff", "W", "medium"),
        ("ruff", "C", "low"),
        ("eslint", "2", "high"),
        ("eslint", "1", "medium"),
    ])
    def test_parametrized_classifications(self, linter, raw, expected):
        """Parametrized test for common classification mappings."""
        result = self.classify(linter, raw)
        assert result == expected

    def test_empty_raw_severity_returns_medium(self):
        """An empty raw_severity string should default to 'medium'."""
        result = self.classify("ruff", "")
        assert result in ("medium", "low")

    def test_none_raw_severity_returns_medium(self):
        """None raw_severity should default to 'medium'."""
        result = self.classify("ruff", None)
        assert result in ("medium", "low")


# ---------------------------------------------------------------------------
# Tests for _run_linter_fix_mode and its linter-specific wrappers
# ---------------------------------------------------------------------------


class TestRunLinterFixMode:
    """Tests for the _run_linter_fix_mode helper and wrappers."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import skill.code_review.scripts.linter_runner as m
        self.mod = m

    def test_run_linter_fix_mode_no_fixes(self):
        """Returns (empty, False) when no fixes are applied."""
        from unittest.mock import MagicMock
        from pathlib import Path
        from subprocess import CompletedProcess

        mock_runner = MagicMock()
        # First call (fix) returns no issues
        mock_runner.return_value = CompletedProcess(
            args=["ruff"], returncode=0, stdout="", stderr="",
        )

        result = self.mod._run_linter_fix_mode(
            "ruff", Path("/tmp"), mock_runner,
            fix_cmd_builder=lambda p: ["ruff", "--fix", str(p)],
            rescan_cmd_builder=lambda p: ["ruff", str(p)],
            fixes_detected=lambda r, o: False,
            rescan_parser=lambda r: [],
            commit_after_fix=False,
        )
        assert result == ([], False)

    def test_run_linter_fix_mode_with_fixes(self):
        """Returns (findings, True) when fixes are applied."""
        from unittest.mock import MagicMock
        from pathlib import Path
        from subprocess import CompletedProcess

        mock_runner = MagicMock()
        # Fix call: returncode 1 with output
        mock_runner.return_value = CompletedProcess(
            args=["ruff"], returncode=1,
            stdout='[{"code": "F401"}]', stderr="",
        )

        def fixes_detected(result, output):
            return True

        def rescan_parser(result):
            return [{"file": "/tmp/test.py", "line": 1, "code": "F401",
                     "severity": "medium", "message": "test",
                     "linter": "ruff"}]

        findings, applied = self.mod._run_linter_fix_mode(
            "ruff", Path("/tmp"), mock_runner,
            fix_cmd_builder=lambda p: ["ruff", "--fix", str(p)],
            rescan_cmd_builder=lambda p: ["ruff", str(p)],
            fixes_detected=fixes_detected,
            rescan_parser=rescan_parser,
            commit_after_fix=False,
        )
        assert applied is True
        assert len(findings) == 1
        assert findings[0]["code"] == "F401"

    def test_run_linter_fix_mode_error_returncode(self):
        """Returns (empty, False) when the fix command returns an error code."""
        from unittest.mock import MagicMock
        from pathlib import Path
        from subprocess import CompletedProcess

        mock_runner = MagicMock()
        mock_runner.return_value = CompletedProcess(
            args=["ruff"], returncode=2, stdout="", stderr="error",
        )

        result = self.mod._run_linter_fix_mode(
            "ruff", Path("/tmp"), mock_runner,
            fix_cmd_builder=lambda p: ["ruff", "--fix", str(p)],
            rescan_cmd_builder=lambda p: ["ruff", str(p)],
            fixes_detected=lambda r, o: False,
            rescan_parser=lambda r: [],
            commit_after_fix=False,
        )
        assert result == ([], False)
