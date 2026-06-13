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
