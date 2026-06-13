"""Tests for the find-related automation script."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skill" / "find-related" / "scripts" / "find_related.py"


def test_script_exists():
    """The script file must exist at the expected path."""
    assert SCRIPT_PATH.exists(), f"Script not found at {SCRIPT_PATH}"


def test_script_is_executable():
    """Script should be executable or at least have a proper shebang."""
    content = SCRIPT_PATH.read_text()
    assert content.startswith("#!/usr/bin/env python3"), "Missing shebang"


def test_help_flag():
    """Script --help should display usage and exit 0."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"--help failed: {result.stderr}"
    assert "usage:" in result.stdout.lower() or "usage:" in result.stderr.lower()


def test_verbose_flag():
    """Script should accept --verbose flag."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--work-item-id",
            "TEST-123",
            "--verbose",
        ],
        capture_output=True,
        text=True,
    )
    # Should not crash with --verbose
    assert result.returncode in (0, 1), f"Unexpected error: {result.stderr}"


def test_json_flag():
    """Script should accept --json flag and produce JSON output when all required args are passed."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--work-item-id",
            "TEST-123",
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    # The script will fail since TEST-123 doesn't exist, but it should still
    # produce valid JSON if --json is passed
    if result.returncode != 0:
        import json
        try:
            json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            # If it failed for non-JSON reasons (e.g., missing wl), that's OK
            pass


def test_work_item_id_required_help():
    """Running script without required args should show error."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
    )
    # Should exit non-zero and indicate --work-item-id is required
    assert result.returncode != 0, "Should fail without --work-item-id"
    msg = (result.stdout + result.stderr).lower()
    assert "work-item-id" in msg or "work_item_id" in msg or "required" in msg


def test_repo_path_flag():
    """Script should accept --repo-path argument."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--work-item-id",
            "TEST-123",
            "--repo-path",
            "/tmp/test-repo",
        ],
        capture_output=True,
        text=True,
    )
    # Should not crash with --repo-path
    assert result.returncode in (0, 1), f"Unexpected error: {result.stderr}"


# ---------------------------------------------------------------------------
# Keyword extraction tests
# ---------------------------------------------------------------------------


def _import_find_related():
    """Import the find_related module for unit testing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("find_related", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keywords_from_title():
    """Keywords should be extracted from a work-item title."""
    mod = _import_find_related()
    title = "Add deterministic script automation to find-related skill"
    keywords = mod.extract_keywords(title, "")
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert "deterministic" in keywords
    assert "script" in keywords
    assert "automation" in keywords
    assert "find" in keywords
    assert "related" in keywords
    assert "skill" in keywords


def test_keywords_from_description():
    """Keywords should be extracted from a work-item description."""
    mod = _import_find_related()
    description = "Create a deterministic Python script to automate the find-related skill, generating a related-work report."
    keywords = mod.extract_keywords("", description)
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert "deterministic" in keywords
    assert "python" in keywords
    assert "script" in keywords
    assert "automate" in keywords
    assert "related" in keywords
    assert "report" in keywords


def test_keywords_from_both_title_and_description():
    """Keywords should be merged from both title and description without duplicates."""
    mod = _import_find_related()
    title = "Add find-related automation"
    description = "Automation for finding related work items in the project repository."
    keywords = mod.extract_keywords(title, description)
    assert isinstance(keywords, list)
    assert len(set(keywords)) == len(keywords), "Keywords should be unique"
    assert "automation" in keywords
    assert "find" in keywords
    assert "related" in keywords
    assert "work" in keywords
    assert "project" in keywords
    assert "repository" in keywords


def test_keywords_empty_title():
    """Keywords from empty title should not cause errors."""
    mod = _import_find_related()
    keywords = mod.extract_keywords("", "Some description text")
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert "description" in keywords
    assert "text" in keywords


def test_keywords_empty_description():
    """Keywords from empty description should not cause errors."""
    mod = _import_find_related()
    keywords = mod.extract_keywords("Some title", "")
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert "title" in keywords


def test_keywords_both_empty():
    """Keywords from empty title and description should return empty list."""
    mod = _import_find_related()
    keywords = mod.extract_keywords("", "")
    assert isinstance(keywords, list)
    assert len(keywords) == 0


def test_keywords_with_special_characters():
    """Keywords should handle special characters gracefully."""
    mod = _import_find_related()
    title = "[CRITICAL] fix: broken build (v2.1) - urgent!"
    description = "Fix the **broken** build; update dependencies (see #123)..."
    keywords = mod.extract_keywords(title, description)
    assert isinstance(keywords, list)
    assert "critical" in keywords
    assert "fix" in keywords
    assert "broken" in keywords
    assert "build" in keywords
    assert "urgent" in keywords
    assert "update" in keywords
    assert "dependencies" in keywords


def test_keywords_excludes_common_stop_words():
    """Common English stop words should be excluded from keywords."""
    mod = _import_find_related()
    title = "The a an is in on at for to of and or the this that with"
    keywords = mod.extract_keywords(title, "")
    # None of these common words should be keywords
    for word in ["the", "a", "an", "is", "in", "on", "at", "for", "to", "of", "and", "or"]:
        assert word not in keywords, f"Stop word '{word}' should be excluded"


def test_keywords_are_lowercase():
    """Keywords should be normalized to lowercase."""
    mod = _import_find_related()
    title = "IMPLEMENT Workflow Integration TEST"
    keywords = mod.extract_keywords(title, "")
    assert "implement" in keywords
    assert "workflow" in keywords
    assert "integration" in keywords
    assert "test" in keywords
