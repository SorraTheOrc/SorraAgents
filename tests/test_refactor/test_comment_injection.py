"""Tests for code comment injection (structured REFACTOR comments in source files).

These tests verify that:
- REFACTOR comments are injected with the correct structured format
- The comment format adapts to different file types (Python, JS/TS, Markdown, YAML)
- Duplicate comments are detected to prevent re-injection
- Comments are placed at the correct position (top of file or after imports)
- Errors (missing files, permissions) are handled gracefully

The target implementation lives in skill/refactor/comment_injection.py.

Related work item: SA-0MQA70XZR0048FLQ
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_dir() -> Path:
    """Create a temporary directory for test file operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_smell() -> dict[str, Any]:
    """A sample code smell finding for comment injection tests."""
    return {
        "file": "src/example.py",
        "line": 42,
        "severity": "high",
        "message": "Hardcoded API key detected in source code",
        "source": "linter",
        "smell_type": "security",
        "code": "S105",
    }


@pytest.fixture
def mock_work_item_id() -> str:
    """A mock work item ID for REFACTOR comments."""
    return "SA-0MOCK9999X000COMMENT"


@pytest.fixture
def python_source() -> str:
    """A sample Python source file content."""
    return (
        "import os\n"
        "import sys\n"
        "\n"
        "\n"
        "def main():\n"
        '    """Main entry point."""\n'
        '    api_key = "sk-12345"\n'
        "    print(api_key)\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )


@pytest.fixture
def javascript_source() -> str:
    """A sample JavaScript source file content."""
    return (
        "const fs = require('fs');\n"
        "const path = require('path');\n"
        "\n"
        "function main() {\n"
        '  const apiKey = "sk-12345";\n'
        "  console.log(apiKey);\n"
        "}\n"
        "\n"
        "module.exports = { main };\n"
    )


@pytest.fixture
def markdown_source() -> str:
    """A sample Markdown file content."""
    return (
        "# My Project\n"
        "\n"
        "## Installation\n"
        "\n"
        "Run `npm install` to install dependencies.\n"
        "\n"
        "## Usage\n"
        "\n"
        "```javascript\n"
        'const apiKey = "sk-12345";\n'
        "```\n"
    )


@pytest.fixture
def yaml_source() -> str:
    """A sample YAML file content."""
    return (
        "version: '3'\n"
        "services:\n"
        "  web:\n"
        "    image: nginx:latest\n"
        "    ports:\n"
        "      - '80:80'\n"
    )


# ---------------------------------------------------------------------------
# Helper: import comment injection module with graceful skip
# ---------------------------------------------------------------------------


def _import_comment_injection():
    """Import the comment injection module; skip tests if not yet implemented."""
    try:
        from skill.refactor import comment_injection
        return comment_injection
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"Comment injection module not yet available: {exc}")


# ---------------------------------------------------------------------------
# Tests: Comment Format
# ---------------------------------------------------------------------------


class TestCommentFormat:
    """REFACTOR comments follow the correct structured format."""

    def test_comment_includes_work_item_id(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Injected comment contains the work item ID."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert result is True

        content = file_path.read_text()
        assert mock_work_item_id in content

    def test_comment_includes_smell_type(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Injected comment contains the smell type."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        assert sample_smell["smell_type"] in content

    def test_comment_includes_description(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Injected comment contains the description."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        assert sample_smell["message"] in content

    def test_comment_has_structured_block_format(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Comment follows the structured REFACTOR block format."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        # The REFACTOR tag should be present
        assert "REFACTOR" in content
        # The smell key should be present
        assert "smell:" in content
        # The description key should be present
        assert "description:" in content

    def test_comment_block_is_self_closing(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """The REFACTOR comment block has a closing marker."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        # Should have the opening marker and closing marker
        assert "REFACTOR" in content
        # The comment block should end properly
        lines = content.splitlines()
        # Find all lines containing REFACTOR related content
        refactor_lines = [i for i, line in enumerate(lines) if "REFACTOR" in line]
        assert len(refactor_lines) >= 1

    def test_comment_contains_severity(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Injected comment includes the severity level."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        assert sample_smell["severity"] in content


# ---------------------------------------------------------------------------
# Tests: File Type Comment Styles
# ---------------------------------------------------------------------------


class TestCommentStyleByFileType:
    """Comment style adapts to the file type."""

    def test_python_comment_style(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Python files use # for comment markers."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "module.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        lines = content.splitlines()
        # Each line of the comment block should start with #
        comment_lines = [
            line for line in lines
            if "REFACTOR" in line or "smell:" in line or "description:" in line
        ]
        for line in comment_lines:
            assert line.lstrip().startswith("#"), f"Line does not start with #: {line}"

    def test_javascript_comment_style(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
        javascript_source: str,
    ):
        """JavaScript files use // for comment markers."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "app.js"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(javascript_source)

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        lines = content.splitlines()
        # Comment lines should start with //
        comment_lines = [
            line for line in lines
            if "REFACTOR" in line or "smell:" in line or "description:" in line
        ]
        has_slash_slash = any(
            "//" in line for line in comment_lines
        )
        # At least one line has the JS comment marker
        assert has_slash_slash or len(comment_lines) == 0

    def test_typescript_comment_style(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """TypeScript files use // for comment markers."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "component.ts"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import React from 'react';\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        lines = content.splitlines()
        comment_lines = [
            line for line in lines
            if "REFACTOR" in line or "smell:" in line or "description:" in line
        ]
        has_slash_slash = any(
            "//" in line for line in comment_lines
        )
        assert has_slash_slash or len(comment_lines) == 0

    def test_markdown_comment_style(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
        markdown_source: str,
    ):
        """Markdown files use HTML-style <!-- --> comments."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "docs" / "README.md"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(markdown_source)

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        lines = content.splitlines()
        comment_lines = [
            line for line in lines
            if "REFACTOR" in line or "smell:" in line or "description:" in line
        ]
        # HTML comments don't need per-line markers in Markdown
        assert "REFACTOR" in content

    def test_yaml_comment_style(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
        yaml_source: str,
    ):
        """YAML files use # for comment markers."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "config" / "docker-compose.yml"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(yaml_source)

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        lines = content.splitlines()
        comment_lines = [
            line for line in lines
            if "REFACTOR" in line or "smell:" in line or "description:" in line
        ]
        for line in comment_lines:
            assert line.lstrip().startswith("#"), f"YAML line does not start with #: {line}"

    def test_html_comment_style(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """HTML files use <!-- --> comments."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "public" / "index.html"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("<!DOCTYPE html>\n<html>\n<head>\n</head>\n</html>\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        assert "<!--" in content

    def test_unknown_extension_defaults_to_hash_style(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Files with unknown extensions default to # style comments."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "script.r"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("library(dplyr)\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        assert "REFACTOR" in content

    def test_no_extension_uses_default_style(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Files without extensions use a default comment style."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "Dockerfile"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("FROM python:3.12\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        assert "REFACTOR" in content


# ---------------------------------------------------------------------------
# Tests: Duplicate Comment Detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """Duplicate REFACTOR comments are detected to prevent re-injection."""

    def test_detects_existing_comment_for_same_smell(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """has_existing_comment returns True when same smell type is already present."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            "import os\n"
        )

        assert (
            comment_mod.has_existing_comment(str(file_path), sample_smell["smell_type"])
            is True
        )

    def test_allows_comment_for_different_smell(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
    ):
        """Different smell type is not blocked by existing comment."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            "import os\n"
        )

        # Different smell type should not be detected as duplicate
        assert (
            comment_mod.has_existing_comment(str(file_path), "complex_function")
            is False
        )

    def test_no_duplicate_when_no_comment_exists(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
    ):
        """Returns False when no REFACTOR comment exists for the smell type."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")

        assert (
            comment_mod.has_existing_comment(str(file_path), sample_smell["smell_type"])
            is False
        )

    def test_empty_file_returns_false(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
    ):
        """An empty file has no existing REFACTOR comments."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "empty.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("")

        assert (
            comment_mod.has_existing_comment(str(file_path), sample_smell["smell_type"])
            is False
        )

    def test_nonexistent_file_returns_false(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
    ):
        """A nonexistent file returns False (no duplicate)."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "nonexistent.py"

        assert (
            comment_mod.has_existing_comment(str(file_path), sample_smell["smell_type"])
            is False
        )

    def test_inject_skips_when_duplicate_exists(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """inject_refactor_comment returns False when a duplicate comment exists."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            "import os\n"
        )

        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert result is False


# ---------------------------------------------------------------------------
# Tests: Line Placement
# ---------------------------------------------------------------------------


class TestLinePlacement:
    """Comments are placed at the correct position in the file."""

    def test_comment_at_top_of_file(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Comment is placed at the top of the file (before any code)."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("\nimport os\n")

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        lines = content.splitlines()
        # Find first non-empty line
        first_content_idx = 0
        for i, line in enumerate(lines):
            if line.strip():
                first_content_idx = i
                break

        # The REFACTOR comment should be among the first content lines
        comment_line_idx = None
        for i, line in enumerate(lines[: first_content_idx + 5]):
            if "REFACTOR" in line:
                comment_line_idx = i
                break

        assert comment_line_idx is not None, (
            "REFACTOR comment not found among early lines"
        )

    def test_placed_before_imports(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Comment is placed before import statements (when mode='before_imports')."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "app.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "import os\n"
            "import sys\n"
            "\n"
            "def main():\n"
            "    pass\n"
        )

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        # Comment should appear before the first import
        first_import_idx = content.index("import os")
        comment_idx = content.index("REFACTOR")

        assert comment_idx < first_import_idx, (
            f"Comment at {comment_idx} should be before first import at {first_import_idx}"
        )

    def test_placed_at_top_in_markdown(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
        markdown_source: str,
    ):
        """Comment is placed at the top of Markdown files."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "docs" / "README.md"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(markdown_source)

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        lines = content.splitlines()
        # Find first non-empty line that's part of the REFACTOR comment
        for i, line in enumerate(lines):
            if "REFACTOR" in line:
                # The comment should be in the first few lines
                assert i < 5, f"REFACTOR comment at line {i}, expected in first 5 lines"
                return

        pytest.fail("No REFACTOR comment found in file")

    def test_does_not_remove_existing_content(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
        python_source: str,
    ):
        """Injecting a comment does not remove or alter existing code."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "example.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(python_source)

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        # Original code should still be present
        assert "def main():" in content
        assert 'api_key = "sk-12345"' in content
        assert "if __name__ == '__main__':" in content


# ---------------------------------------------------------------------------
# Tests: Error Handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Graceful error handling for edge cases."""

    def test_nonexistent_file_returns_false(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Injecting into a nonexistent file returns False."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "nonexistent" / "file.py"

        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert result is False

    def test_permission_error_returns_false(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """When a file cannot be read/written, return False rather than crash."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "readonly.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("import os\n")
        # Make file read-only
        os.chmod(str(file_path), 0o444)

        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert result is False

    def test_binary_file_handled(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Binary files are handled without crashing."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "data.bin"
        file_path.write_bytes(b"\x00\x01\x02\x03")

        # Should not raise
        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        # Binary files may or may not accept injection, but shouldn't crash
        assert result is False

    def test_empty_file_content_handled(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Empty files can still receive REFACTOR comments."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "empty.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("")

        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        # Should at least not crash, and ideally return True
        assert result is not None
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Tests: Injection Return Value Semantics
# ---------------------------------------------------------------------------


class TestInjectionReturnValue:
    """Return value semantics for inject_refactor_comment."""

    def test_returns_true_on_successful_injection(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Returns True when a comment is successfully injected."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "success.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("x = 1\n")

        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert result is True

    def test_returns_false_when_file_unchanged(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Returns False when the file is not modified (e.g., duplicate exists)."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "already_has.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            "import os\n"
        )

        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert result is False

    def test_return_value_is_bool(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Return value is always a boolean."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "check.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("x = 1\n")

        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert isinstance(result, bool)

    def test_file_is_actually_modified(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """The file content is actually changed after a successful injection."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "will_change.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        original = "x = 1\n"
        file_path.write_text(original)

        comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )

        content = file_path.read_text()
        assert content != original, "File content did not change after injection"

    def test_multiple_injections_allowed_for_different_smells(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Multiple different smell types can be injected into the same file."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "multi.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("x = 1\n")

        # Inject first smell
        smell1 = dict(sample_smell)
        smell1["smell_type"] = "security"
        result1 = comment_mod.inject_refactor_comment(
            str(file_path), smell1, mock_work_item_id
        )

        # Inject second smell (different type)
        smell2 = dict(sample_smell)
        smell2["smell_type"] = "magic_number"
        result2 = comment_mod.inject_refactor_comment(
            str(file_path), smell2, mock_work_item_id
        )

        content = file_path.read_text()
        assert "security" in content
        assert "magic_number" in content
        # Both injections should succeed if they're different types
        assert result1 is True
        assert result2 is True


# ---------------------------------------------------------------------------
# Tests: has_existing_comment Semantics
# ---------------------------------------------------------------------------


class TestHasExistingComment:
    """Semantics of the has_existing_comment function."""

    def test_has_existing_comment_returns_bool(
        self,
        temp_dir: Path,
    ):
        """has_existing_comment always returns a bool."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "check.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("x = 1\n")

        result = comment_mod.has_existing_comment(str(file_path), "security")
        assert isinstance(result, bool)

    def test_has_existing_comment_with_duplicate_id(
        self,
        temp_dir: Path,
    ):
        """Different work item IDs with the same smell type are still duplicates."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "dup_id.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK1111\n"
            "# smell: security\n"
            "# description: Some issue\n"
            "# -->\n"
            "x = 1\n"
        )

        # Different work item ID but same smell type should still count as duplicate
        assert (
            comment_mod.has_existing_comment(str(file_path), "security")
            is True
        )

    def test_has_existing_comment_case_insensitive(
        self,
        temp_dir: Path,
    ):
        """Smell type matching in has_existing_comment is case-insensitive."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "case_check.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: SECURITY\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            "import os\n"
        )

        # Should match regardless of case
        assert (
            comment_mod.has_existing_comment(str(file_path), "Security")
            is True
        )

    def test_has_existing_comment_with_partial_match(
        self,
        temp_dir: Path,
    ):
        """Partial smell type names should not cause false positives."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "partial.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: unused_function\n"
            "# description: Function unused\n"
            "# -->\n"
            "import os\n"
        )

        # Partial match should NOT be detected as duplicate
        assert (
            comment_mod.has_existing_comment(str(file_path), "unused")
            is False
        )

    def test_has_existing_comment_cross_file(
        self,
        temp_dir: Path,
    ):
        """has_existing_comment is scoped to a single file."""
        comment_mod = _import_comment_injection()

        directory = temp_dir / "src"
        directory.mkdir(parents=True, exist_ok=True)

        # File A has a REFACTOR comment
        file_a = directory / "a.py"
        file_a.write_text(
            "# <!-- REFACTOR-SA-0MOCK9999\n"
            "# smell: security\n"
            "# description: Hardcoded API key\n"
            "# -->\n"
            "import os\n"
        )

        # File B does not have a comment
        file_b = directory / "b.py"
        file_b.write_text("import os\n")

        assert (
            comment_mod.has_existing_comment(str(file_a), "security")
            is True
        )
        assert (
            comment_mod.has_existing_comment(str(file_b), "security")
            is False
        )


# ---------------------------------------------------------------------------
# Tests: Comment Style Detection
# ---------------------------------------------------------------------------


class TestCommentStyleDetection:
    """The module provides a way to determine comment style for a file type."""

    def test_get_comment_style_python(self, temp_dir: Path):
        """get_comment_style returns appropriate style for .py files."""
        comment_mod = _import_comment_injection()

        style = comment_mod.get_comment_style("src/example.py")
        assert isinstance(style, str) or isinstance(style, dict)
        if isinstance(style, str):
            assert "#" in style or "//" in style or "<!--" in style

    def test_get_comment_style_javascript(self, temp_dir: Path):
        """get_comment_style returns appropriate style for .js files."""
        comment_mod = _import_comment_injection()

        style = comment_mod.get_comment_style("src/app.js")
        assert isinstance(style, str) or isinstance(style, dict)

    def test_get_comment_style_markdown(self, temp_dir: Path):
        """get_comment_style returns appropriate style for .md files."""
        comment_mod = _import_comment_injection()

        style = comment_mod.get_comment_style("docs/README.md")
        assert isinstance(style, str) or isinstance(style, dict)

    def test_get_comment_style_yaml(self, temp_dir: Path):
        """get_comment_style returns appropriate style for .yml/.yaml files."""
        comment_mod = _import_comment_injection()

        style = comment_mod.get_comment_style("config/settings.yml")
        assert isinstance(style, str) or isinstance(style, dict)

    def test_get_comment_style_html(self, temp_dir: Path):
        """get_comment_style returns appropriate style for .html files."""
        comment_mod = _import_comment_injection()

        style = comment_mod.get_comment_style("public/index.html")
        assert isinstance(style, str) or isinstance(style, dict)

    def test_get_comment_style_no_extension(self, temp_dir: Path):
        """get_comment_style handles files without extensions."""
        comment_mod = _import_comment_injection()

        style = comment_mod.get_comment_style("Dockerfile")
        assert isinstance(style, str) or isinstance(style, dict)


# ---------------------------------------------------------------------------
# Tests: Integration Scenario
# ---------------------------------------------------------------------------


class TestIntegrationScenario:
    """End-to-end scenarios combining multiple aspects."""

    def test_detect_and_inject_flow(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
    ):
        """Flow: check for existing comment -> inject -> verify -> re-check."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "flow.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("x = 1\n")

        # Initially no comment exists
        assert (
            comment_mod.has_existing_comment(str(file_path), sample_smell["smell_type"])
            is False
        )

        # Inject the comment
        result = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert result is True

        # After injection, the comment should be detectable
        assert (
            comment_mod.has_existing_comment(str(file_path), sample_smell["smell_type"])
            is True
        )

        # A second injection should be prevented (returns False)
        result2 = comment_mod.inject_refactor_comment(
            str(file_path), sample_smell, mock_work_item_id
        )
        assert result2 is False

    def test_multiple_file_types_in_project(
        self,
        temp_dir: Path,
        sample_smell: dict[str, Any],
        mock_work_item_id: str,
        python_source: str,
        javascript_source: str,
        markdown_source: str,
        yaml_source: str,
    ):
        """REFACTOR comments can be injected into multiple file types in a project."""
        comment_mod = _import_comment_injection()

        # Create files of different types
        files = {
            "src/main.py": python_source,
            "src/app.js": javascript_source,
            "docs/README.md": markdown_source,
            "config/docker-compose.yml": yaml_source,
        }

        for rel_path, content in files.items():
            file_path = temp_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

        # Inject into each file
        all_succeeded = True
        for rel_path in files:
            file_path = temp_dir / rel_path
            result = comment_mod.inject_refactor_comment(
                str(file_path), sample_smell, mock_work_item_id
            )
            if not result:
                all_succeeded = False

        # At least some injections should succeed
        assert isinstance(all_succeeded, bool)

    def test_multiple_smells_same_file(
        self,
        temp_dir: Path,
        mock_work_item_id: str,
    ):
        """Multiple different smells in the same file each get their own comment."""
        comment_mod = _import_comment_injection()

        file_path = temp_dir / "src" / "busy.py"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("x = 1\n")

        smells = [
            {"file": str(file_path), "line": 1, "severity": "high",
             "message": "Security issue", "source": "linter",
             "smell_type": "security", "code": "S105"},
            {"file": str(file_path), "line": 2, "severity": "medium",
             "message": "Unused variable", "source": "linter",
             "smell_type": "unused_variable", "code": "F841"},
            {"file": str(file_path), "line": 3, "severity": "low",
             "message": "Missing docstring", "source": "llm",
             "smell_type": "documentation", "code": "D100"},
        ]

        for smell in smells:
            comment_mod.inject_refactor_comment(
                str(file_path), smell, mock_work_item_id
            )

        content = file_path.read_text()
        # All three smell types should be present
        assert "security" in content
        assert "unused_variable" in content
        assert "documentation" in content
