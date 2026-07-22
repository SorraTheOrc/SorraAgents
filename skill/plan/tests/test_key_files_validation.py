"""Tests for key_files validation helpers in skill/plan/plan_helpers.py.

Written before the implementation exists (TDD).
"""


from skill.plan.plan_helpers import validate_key_files_format, validate_key_files_in_description


# =========================================================================
# 1. validate_key_files_format — syntactic validation of a raw key-files block
# =========================================================================


class TestValidateKeyFilesFormat:
    """Verify syntactic validation of ``**Key Files:**`` block content."""

    def test_valid_single_file(self):
        """A single well-formed path returns no issues."""
        text = """\
**Key Files:**
- `src/main.py` — Main module"""
        issues = validate_key_files_format(text)
        assert issues == []

    def test_valid_multiple_files(self):
        """Multiple well-formed paths return no issues."""
        text = """\
**Key Files:**
- `src/main.py` — Main module
- `src/utils/helper.py` — Helper utilities
- `tests/test_main.py` — Tests"""
        issues = validate_key_files_format(text)
        assert issues == []

    def test_path_without_slash(self):
        """A path without '/' raises an issue."""
        text = """\
**Key Files:**
- `main.py` — No directory separator"""
        issues = validate_key_files_format(text)
        assert len(issues) == 1
        assert "/" in issues[0].lower() or "directory separator" in issues[0]

    def test_path_without_extension(self):
        """A path without a file extension raises an issue."""
        text = """\
**Key Files:**
- `src/myfile` — No extension"""
        issues = validate_key_files_format(text)
        assert len(issues) == 1
        assert "extension" in issues[0].lower()

    def test_multiple_invalid_paths(self):
        """Multiple invalid paths raise multiple issues."""
        text = """\
**Key Files:**
- `main.py` — No directory
- `src/myfile` — No extension
- `justadir/` — No extension"""
        issues = validate_key_files_format(text)
        assert len(issues) >= 2

    def test_no_key_files_section(self):
        """When there is no ``**Key Files:**`` section, return empty list (no issues)."""
        text = """\
# Some work item
No key files here."""
        issues = validate_key_files_format(text)
        assert issues == []

    def test_empty_key_files_section(self):
        """When ``**Key Files:**`` section exists but is empty, warn."""
        text = """\
**Key Files:**

No files listed."""
        issues = validate_key_files_format(text)
        assert len(issues) == 1
        assert "no valid file paths" in issues[0].lower()

    def test_paths_without_backticks(self):
        """Paths without backticks still get validated."""
        text = """\
**Key Files:**
- src/main.py — Note without backticks"""
        issues = validate_key_files_format(text)
        assert issues == []

    def test_paths_with_spaces(self):
        """Paths with spaces (after stripping backticks) are validated."""
        text = """\
**Key Files:**
- `path/to/my file.py` — Space in path"""
        issues = validate_key_files_format(text)
        assert issues == []

    def test_path_with_complex_filename(self):
        """Complex paths with multiple dots are valid."""
        text = """\
**Key Files:**
- `src/utils/test.v1.helper.py` — Dotted filename"""
        issues = validate_key_files_format(text)
        assert issues == []

    def test_mixed_valid_and_invalid(self):
        """Mixed valid and invalid paths report only the invalid ones."""
        text = """\
**Key Files:**
- `src/main.py` — Good
- `main.py` — No directory
- `src/other.py` — Good
- `src/noext` — No extension"""
        issues = validate_key_files_format(text)
        assert len(issues) == 2

    def test_multiline_explanation_no_path(self):
        """Bullet items without backtick paths or obvious paths are not flagged."""
        text = """\
**Key Files:**
- This is just a note, not a path
- `src/real.py` — This is a real path"""
        issues = validate_key_files_format(text)
        # The first bullet has no path, so only the valid one counts
        assert issues == []


# =========================================================================
# 2. validate_key_files_in_description — integraton-style checks
# =========================================================================


class TestValidateKeyFilesInDescription:
    """Verify the higher-level wrapper that works on work item descriptions."""

    def test_returns_issues_for_bad_description(self):
        """Invalid paths in a full description are caught."""
        desc = """\
# Work Item Title

## Problem
Something needs fixing.

**Key Files:**
- `src/main.py` — Good
- `standalone.py` — No directory"""
        issues = validate_key_files_in_description(desc)
        assert len(issues) >= 1

    def test_returns_no_issues_for_clean_description(self):
        """A clean description with well-formed paths returns no issues."""
        desc = """\
# Work Item Title

**Key Files:**
- `src/main.py` — Good
- `src/utils/helper.py` — Good"""
        issues = validate_key_files_in_description(desc)
        assert issues == []

    def test_handles_missing_section_gracefully(self):
        """A description without a Key Files section returns no issues."""
        desc = """\
# Work Item Title

## Problem
No key files."""
        issues = validate_key_files_in_description(desc)
        assert issues == []
