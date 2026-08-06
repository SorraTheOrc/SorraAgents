"""Smoke and critical tests for the dev branch.

These are fast, high-confidence tests that run on every push to ``dev``
to catch breakages before they reach the full test suite.

Run:
    pytest tests/dev/test_smoke.py -v
    pytest tests/dev/test_smoke.py -v -k smoke
    pytest tests/dev/test_smoke.py -v -k critical
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_GITHUB_DIR = _REPO_ROOT / ".github"

# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_smoke_no_ci_workflows():
    """No GitHub CI workflow files remain (project operates fully locally)."""
    assert not (_GITHUB_DIR / "workflows").exists(), \
        "CI workflows directory should have been removed"
    assert not (_GITHUB_DIR / "ampa-dev-baseline-size").exists(), \
        "CI baseline size file should have been removed"


@pytest.mark.smoke
def test_smoke_readme_does_not_document_ci():
    """README.md does not reference the removed CI workflows."""
    readme = _REPO_ROOT / "README.md"
    content = readme.read_text()
    for ref in ("dev-smoke", "dev-full-suite", "workflows/ci.yml"):
        assert ref not in content, f"README still references removed CI workflow: {ref}"


# ---------------------------------------------------------------------------
# Critical tests
# ---------------------------------------------------------------------------


@pytest.mark.critical
def test_critical_wl_cli_available():
    """Worklog CLI is available on PATH."""
    result = subprocess.run(["wl", "--version"], capture_output=True, text=True)  # noqa: PLW1510
    assert result.returncode == 0, f"wl CLI not available: {result.stderr}"


@pytest.mark.critical
def test_critical_skills_directory_exists():
    """The skill/ directory exists with expected skills."""
    skill_dir = _REPO_ROOT / "skill"
    assert skill_dir.is_dir(), f"Missing {skill_dir}"
    expected = ["audit", "implement", "triage"]
    for s in expected:
        assert (skill_dir / s).is_dir(), f"Missing skill: {s}"


@pytest.mark.critical
def test_critical_release_process_documented():
    """The release process is documented and does not reference removed CI gates."""
    readme = _REPO_ROOT / "README.md"
    content = readme.read_text().lower()
    assert "release process" in content, "README missing release process section"
    assert "dev-full-suite" not in content, "Release process still references dev-full-suite"
