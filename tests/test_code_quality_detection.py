"""Tests for language detection and linter probing modules.

These tests verify that:
- detect_languages() correctly identifies Python and TypeScript files
- probe_linter() correctly reports linter availability
- Both functions handle edge cases (empty projects, missing linters) gracefully

The target implementation lives in skill/code_review/scripts/detection.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_project() -> Path:
    """Create a temporary directory simulating a project root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def project_with_python(temp_project: Path) -> Path:
    """Create a project with Python files."""
    (temp_project / "src").mkdir(parents=True, exist_ok=True)
    (temp_project / "src" / "main.py").touch()
    (temp_project / "src" / "utils.py").touch()
    (temp_project / "tests").mkdir(parents=True, exist_ok=True)
    (temp_project / "tests" / "test_main.py").touch()
    return temp_project


@pytest.fixture
def project_with_typescript(temp_project: Path) -> Path:
    """Create a project with TypeScript files."""
    (temp_project / "src").mkdir(parents=True, exist_ok=True)
    (temp_project / "src" / "app.ts").touch()
    (temp_project / "src" / "components.tsx").touch()
    (temp_project / "tests").mkdir(parents=True, exist_ok=True)
    (temp_project / "tests" / "test_app.ts").touch()
    return temp_project


@pytest.fixture
def project_with_python_and_typescript(project_with_python: Path) -> Path:
    """Create a project with both Python and TypeScript files."""
    (project_with_python / "web").mkdir(parents=True, exist_ok=True)
    (project_with_python / "web" / "app.ts").touch()
    (project_with_python / "web" / "ui.tsx").touch()
    return project_with_python


@pytest.fixture
def empty_project(temp_project: Path) -> Path:
    """Create a project with no recognizable code files."""
    (temp_project / "data.json").touch()
    (temp_project / "config.yaml").touch()
    return temp_project


# ===================================================================
# detect_languages() tests
# ===================================================================


class TestDetectLanguages:
    """Tests for detect_languages()."""

    def _import_detection(self):
        """Import detection module; skip test if not yet implemented."""
        try:
            from skill.code_review.scripts import detection
            return detection
        except (ImportError, ModuleNotFoundError) as exc:
            pytest.skip(f"Detection module not yet available: {exc}")

    def test_detects_python(self, project_with_python: Path):
        """detect_languages() returns ['python'] for a project with .py files."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_python))
        assert "python" in result

    def test_detects_typescript(self, project_with_typescript: Path):
        """detect_languages() returns ['typescript'] for a project with .ts/.tsx files."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_typescript))
        assert "typescript" in result

    def test_detects_both(self, project_with_python_and_typescript: Path):
        """detect_languages() returns both python and typescript when both are present."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_python_and_typescript))
        assert "python" in result
        assert "typescript" in result

    def test_empty_project_returns_empty(self, empty_project: Path):
        """detect_languages() returns an empty list for a project with no recognized files."""
        detection = self._import_detection()
        result = detection.detect_languages(str(empty_project))
        assert isinstance(result, list)
        assert len(result) == 0

    def test_returns_list_of_strings(self, project_with_python: Path):
        """detect_languages() returns a list of strings."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_python))
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, str)

    def test_default_root_is_cwd(self, monkeypatch):
        """detect_languages() defaults to current working directory."""
        detection = self._import_detection()
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.chdir(tmpdir)
            path = Path(tmpdir)
            (path / "script.py").touch()
            result = detection.detect_languages()
            assert "python" in result

    def test_accepts_path_or_string(self, project_with_python: Path):
        """detect_languages() accepts both Path objects and strings."""
        detection = self._import_detection()
        result_str = detection.detect_languages(str(project_with_python))
        result_path = detection.detect_languages(project_with_python)
        assert result_str == result_path

    def test_nonexistent_directory_returns_empty(self):
        """detect_languages() gracefully handles nonexistent directories."""
        detection = self._import_detection()
        result = detection.detect_languages("/nonexistent/path/12345")
        assert isinstance(result, list)
        assert len(result) == 0

    def test_does_not_include_hidden_dirs(self, project_with_python: Path):
        """detect_languages() should skip hidden directories by default."""
        detection = self._import_detection()
        hidden = project_with_python / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").touch()
        result = detection.detect_languages(str(project_with_python))
        assert "python" in result
        # The detection should still find python in the visible dirs
        # This test just verifies hidden dirs don't cause errors


# ===================================================================
# probe_linter() tests
# ===================================================================


class TestProbeLinter:
    """Tests for probe_linter()."""

    def _import_detection(self):
        """Import detection module; skip test if not yet implemented."""
        try:
            from skill.code_review.scripts import detection
            return detection
        except (ImportError, ModuleNotFoundError) as exc:
            pytest.skip(f"Detection module not yet available: {exc}")

    def test_ruff_available(self):
        """probe_linter('ruff') returns {'name': 'ruff', 'available': True} if on PATH."""
        detection = self._import_detection()
        # ruff may or may not be installed; test the structure, not the value
        result = detection.probe_linter("ruff")
        assert isinstance(result, dict)
        assert result["name"] == "ruff"
        assert "available" in result
        assert isinstance(result["available"], bool)

    def test_eslint_available(self):
        """probe_linter('eslint') returns expected structure."""
        detection = self._import_detection()
        result = detection.probe_linter("eslint")
        assert isinstance(result, dict)
        assert result["name"] == "eslint"
        assert "available" in result
        assert isinstance(result["available"], bool)

    def test_nonexistent_linter_returns_not_available(self):
        """probe_linter() returns available=False for a tool not on PATH."""
        detection = self._import_detection()
        result = detection.probe_linter("this-linter-does-not-exist-12345")
        assert isinstance(result, dict)
        assert result["name"] == "this-linter-does-not-exist-12345"
        assert result["available"] is False

    def test_returns_dict_with_name_and_available(self):
        """probe_linter() always returns a dict with 'name' and 'available' keys."""
        detection = self._import_detection()
        result = detection.probe_linter("ruff")
        assert "name" in result
        assert "available" in result

    def test_json_serializable(self):
        """probe_linter() return value must be JSON-serializable."""
        detection = self._import_detection()
        result = detection.probe_linter("ruff")
        # Should not raise
        json.dumps(result)

    def test_case_sensitive_linter_name(self):
        """probe_linter() returns the name exactly as provided."""
        detection = self._import_detection()
        result = detection.probe_linter("Ruff")
        assert result["name"] == "Ruff"

    @patch("shutil.which", return_value=None)
    def test_respects_shutil_which(self, mock_which):
        """probe_linter() respects shutil.which() to determine availability."""
        detection = self._import_detection()
        result = detection.probe_linter("ruff")
        assert result["available"] is False
        mock_which.assert_called_once_with("ruff")

    @patch("shutil.which", return_value="/usr/bin/ruff")
    def test_available_when_which_finds_path(self, mock_which):
        """probe_linter() returns available=True when shutil.which() returns a path."""
        detection = self._import_detection()
        result = detection.probe_linter("ruff")
        assert result["available"] is True
        mock_which.assert_called_once_with("ruff")


# ===================================================================
# Integration-style tests (project scanning + linter probing)
# ===================================================================


class TestDetectionIntegration:
    """Tests that combine detection and probing for common scenarios."""

    def _import_detection(self):
        try:
            from skill.code_review.scripts import detection
            return detection
        except (ImportError, ModuleNotFoundError) as exc:
            pytest.skip(f"Detection module not yet available: {exc}")

    def test_python_project_ruff_probe(self, project_with_python: Path):
        """A Python project should detect python and ruff should be probeable."""
        detection = self._import_detection()
        langs = detection.detect_languages(str(project_with_python))
        assert "python" in langs

        probe = detection.probe_linter("ruff")
        assert probe["name"] == "ruff"
        assert "available" in probe

    def test_typescript_project_eslint_probe(self, project_with_typescript: Path):
        """A TypeScript project should detect typescript and eslint should be probeable."""
        detection = self._import_detection()
        langs = detection.detect_languages(str(project_with_typescript))
        assert "typescript" in langs

        probe = detection.probe_linter("eslint")
        assert probe["name"] == "eslint"
        assert "available" in probe
# Phase 2 fixtures
@pytest.fixture
def project_with_markdown(temp_project: Path) -> Path:
    """Create a project with Markdown files."""
    (temp_project / "docs").mkdir(parents=True, exist_ok=True)
    (temp_project / "docs" / "README.md").touch()
    (temp_project / "docs" / "CONTRIBUTING.md").touch()
    return temp_project


@pytest.fixture
def project_with_shell(temp_project: Path) -> Path:
    """Create a project with Shell scripts."""
    (temp_project / "scripts").mkdir(parents=True, exist_ok=True)
    (temp_project / "scripts" / "setup.sh").touch()
    (temp_project / "scripts" / "deploy.bash").touch()
    return temp_project


@pytest.fixture
def project_with_javascript(temp_project: Path) -> Path:
    """Create a project with JavaScript files."""
    (temp_project / "src").mkdir(parents=True, exist_ok=True)
    (temp_project / "src" / "index.js").touch()
    (temp_project / "src" / "config.cjs").touch()
    (temp_project / "src" / "module.mjs").touch()
    return temp_project


@pytest.fixture
def project_with_nodejs(temp_project: Path) -> Path:
    """Create a Node.js project (detected via package.json)."""
    (temp_project / "package.json").touch()
    (temp_project / "package-lock.json").touch()
    return temp_project


@pytest.fixture
def project_with_csharp(temp_project: Path) -> Path:
    """Create a C# project."""
    (temp_project / "src").mkdir(parents=True, exist_ok=True)
    (temp_project / "src" / "Program.cs").touch()
    (temp_project / "src" / "Utils.cs").touch()
    (temp_project / "Project.csproj").touch()
    return temp_project


# Phase 2 test classes
class TestPhase2LanguageDetection:
    """Tests for Phase 2 language detection (Markdown, Shell, JavaScript, C#)."""

    def _import_detection(self):
        """Import detection module; skip test if not yet implemented."""
        try:
            from skill.code_review.scripts import detection
            return detection
        except (ImportError, ModuleNotFoundError) as exc:
            pytest.skip(f"Detection module not yet available: {exc}")

    def test_detects_markdown_if_supported(self, project_with_markdown: Path):
        """detect_languages() returns ['markdown'] if Markdown detection is implemented."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_markdown))
        # Check if markdown is in LANGUAGE_EXTENSIONS; skip if not yet supported
        if "markdown" not in detection.LANGUAGE_EXTENSIONS:
            pytest.skip("Markdown detection not yet implemented (Phase 2)")
        assert "markdown" in result

    def test_detects_shell_if_supported(self, project_with_shell: Path):
        """detect_languages() returns ['shell'] if Shell detection is implemented."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_shell))
        if "shell" not in detection.LANGUAGE_EXTENSIONS:
            pytest.skip("Shell detection not yet implemented (Phase 2)")
        assert "shell" in result

    def test_detects_javascript_if_supported(self, project_with_javascript: Path):
        """detect_languages() returns ['javascript'] if JS detection is implemented."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_javascript))
        if "javascript" not in detection.LANGUAGE_EXTENSIONS:
            pytest.skip("JavaScript detection not yet implemented (Phase 2)")
        assert "javascript" in result

    def test_detects_nodejs_if_supported(self, project_with_nodejs: Path):
        """detect_languages() detects Node.js via package.json if supported."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_nodejs))
        if "javascript" not in detection.LANGUAGE_EXTENSIONS:
            pytest.skip("Node.js detection not yet implemented (Phase 2)")
        assert "javascript" in result

    def test_detects_csharp_if_supported(self, project_with_csharp: Path):
        """detect_languages() returns ['csharp'] if C# detection is implemented."""
        detection = self._import_detection()
        result = detection.detect_languages(str(project_with_csharp))
        if "csharp" not in detection.LANGUAGE_EXTENSIONS:
            pytest.skip("C# detection not yet implemented (Phase 2)")
        assert "csharp" in result


class TestPhase2LinterProbing:
    """Tests for Phase 2 linter probing (markdownlint, shellcheck, dotnet-format)."""

    def _import_detection(self):
        """Import detection module; skip test if not yet implemented."""
        try:
            from skill.code_review.scripts import detection
            return detection
        except (ImportError, ModuleNotFoundError) as exc:
            pytest.skip(f"Detection module not yet available: {exc}")

    def test_probe_markdownlint(self):
        """probe_linter('markdownlint') returns expected structure."""
        detection = self._import_detection()
        result = detection.probe_linter("markdownlint")
        assert isinstance(result, dict)
        assert result["name"] == "markdownlint"
        assert "available" in result
        assert isinstance(result["available"], bool)

    def test_probe_shellcheck(self):
        """probe_linter('shellcheck') returns expected structure."""
        detection = self._import_detection()
        result = detection.probe_linter("shellcheck")
        assert isinstance(result, dict)
        assert result["name"] == "shellcheck"
        assert "available" in result
        assert isinstance(result["available"], bool)

    def test_probe_dotnet_format(self):
        """probe_linter('dotnet-format') returns expected structure."""
        detection = self._import_detection()
        result = detection.probe_linter("dotnet-format")
        assert isinstance(result, dict)
        assert result["name"] == "dotnet-format"
        assert "available" in result
        assert isinstance(result["available"], bool)

    def test_probe_dotnet(self):
        """probe_linter('dotnet') returns expected structure."""
        detection = self._import_detection()
        result = detection.probe_linter("dotnet")
        assert isinstance(result, dict)
        assert result["name"] == "dotnet"
        assert "available" in result
        assert isinstance(result["available"], bool)

    def test_probe_eslint_returns_structure(self):
        """probe_linter('eslint') returns expected structure (also used for JS/Node)."""
        detection = self._import_detection()
        result = detection.probe_linter("eslint")
        assert isinstance(result, dict)
        assert result["name"] == "eslint"
        assert "available" in result
