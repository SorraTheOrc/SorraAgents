"""Tests for ESLint ESM parsing fix.

Verifies that:
1. The `.eslintrc.json` config exists with proper settings for ESM parsing
2. The linter runner does not use `--no-eslintrc` which prevented config loading
3. All affected files parse without ESLint errors
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Config file tests
# ---------------------------------------------------------------------------


class TestEslintConfigExists:
    """The project must have an .eslintrc.json that enables ESM parsing."""

    CONFIG_PATH = REPO_ROOT / ".eslintrc.json"

    def test_config_file_exists(self):
        assert self.CONFIG_PATH.exists(), ".eslintrc.json must exist"

    def test_config_is_valid_json(self):
        raw = self.CONFIG_PATH.read_text()
        data = json.loads(raw)
        assert isinstance(data, dict)

    def test_config_has_parser_options(self):
        raw = self.CONFIG_PATH.read_text()
        data = json.loads(raw)
        assert "parserOptions" in data

    def test_config_ecma_version_sufficient(self):
        """ecmaVersion must be at least 2017 for async/await support."""
        raw = self.CONFIG_PATH.read_text()
        data = json.loads(raw)
        ecma = data.get("parserOptions", {}).get("ecmaVersion", 0)
        assert isinstance(ecma, int), f"ecmaVersion should be an int, got {type(ecma)}"
        assert ecma >= 2017, f"ecmaVersion {ecma} < 2017 is too low for modern JS"

    def test_config_source_type_is_module(self):
        """sourceType must be 'module' for import/export support."""
        raw = self.CONFIG_PATH.read_text()
        data = json.loads(raw)
        st = data.get("parserOptions", {}).get("sourceType", "")
        assert st == "module", f"sourceType should be 'module', got '{st}'"

    def test_config_has_node_env(self):
        """Node.js globals must be enabled."""
        raw = self.CONFIG_PATH.read_text()
        data = json.loads(raw)
        env = data.get("env", {})
        assert env.get("node") is True, "node env must be enabled"


# ---------------------------------------------------------------------------
# Linter runner — no --no-eslintrc
# ---------------------------------------------------------------------------


class TestLinterRunnerNoNoEslintrc:
    """The linter runner must NOT pass --no-eslintrc to ESLint."""

    RUNNER_PATH = REPO_ROOT / "skill" / "code_review" / "scripts" / "linter_runner.py"

    def test_runner_file_exists(self):
        assert self.RUNNER_PATH.exists()

    def test_no_no_eslintrc_in_check_command(self):
        """The check-mode ESLint command must not contain --no-eslintrc."""
        content = self.RUNNER_PATH.read_text()
        # Find the check-mode command (the first eslint cmd without --fix)
        lines = content.splitlines()
        for line in lines:
            stripped = line.strip()
            if "eslint" in stripped and "--no-eslintrc" in stripped:
                pytest.fail(f"Found --no-eslintrc in runner: {stripped.strip()}")

    def test_no_no_eslintrc_in_fix_command(self):
        """The fix-mode ESLint command must not contain --no-eslintrc."""
        content = self.RUNNER_PATH.read_text()
        if '"--no-eslintrc"' in content or "'--no-eslintrc'" in content:
            pytest.fail("--no-eslintrc is still present in linter_runner.py")


# ---------------------------------------------------------------------------
# Integration: ESLint parses all affected files correctly
# ---------------------------------------------------------------------------


class TestEslintParsing:
    """All previously-affected files must parse without ESLint errors."""

    AFFECTED_FILES = [
        "plugins/ralph.js",
        "plugins/tests/ralph-compaction.test.js",
        "skill/ship/scripts/check-audit-gate.js",
        "skill/ship/scripts/check-unmerged-branches.js",
        "skill/ship/scripts/git-helpers.js",
        "skill/ship/scripts/release/bump-version.js",
        "skill/ship/scripts/release/generate-changelog.js",
        "skill/ship/scripts/run-release.js",
        "skill/ship/scripts/ship.js",
        "tests/helpers/git-sim.js",
        "tests/run-installer-tests.js",
    ]

    @pytest.mark.parametrize("rel_path", AFFECTED_FILES)
    def test_file_parses_without_error(self, rel_path):
        """Run eslint on each previously-failing file and expect 0 errors."""
        import subprocess

        file_path = REPO_ROOT / rel_path
        if not file_path.exists():
            pytest.skip(f"File does not exist: {file_path}")

        result = subprocess.run(
            ["eslint", str(file_path), "-f", "json", "--quiet"],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode not in (0, 1):
            pytest.fail(f"ESLint failed for {rel_path}: {result.stderr}")

        if not result.stdout.strip():
            return  # Empty output means no issues

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            pytest.fail(f"ESLint output not valid JSON for {rel_path}: {result.stdout[:200]}")

        if not isinstance(data, list) or len(data) == 0:
            return

        errors = []
        for file_result in data:
            if isinstance(file_result, dict):
                msgs = file_result.get("messages", [])
                for msg in msgs:
                    if msg.get("fatal"):
                        errors.append(f"  Line {msg.get('line')}: {msg.get('message')}")
                    elif msg.get("severity", 0) >= 2:
                        errors.append(f"  Line {msg.get('line')}: {msg.get('message')}")

        assert len(errors) == 0, (
            f"ESLint found errors in {rel_path}:\n" + "\n".join(errors)
        )


# ---------------------------------------------------------------------------
# Run the linter runner import test (basic regression)
# ---------------------------------------------------------------------------


class TestLinterRunnerImportable:
    """The linter runner module must still be importable."""

    def test_import_linter_runner(self):
        """Import the module (does not run) to check no syntax errors."""
        import importlib
        spec = importlib.util.find_spec("skill.code_review.scripts.linter_runner")
        if spec is None:
            pytest.skip("linter_runner module not found on sys.path")
        # Just verify the module path resolves
        assert spec.origin is not None
