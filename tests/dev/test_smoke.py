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
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_smoke_workflows_exist():
    """Both dev CI workflow files are present."""
    smoke = _WORKFLOWS_DIR / "dev-smoke.yml"
    full = _WORKFLOWS_DIR / "dev-full-suite.yml"
    assert smoke.is_file(), f"Missing {smoke}"
    assert full.is_file(), f"Missing {full}"


@pytest.mark.smoke
def test_smoke_dev_smoke_has_correct_trigger():
    """dev-smoke.yml triggers on push to dev."""
    import yaml

    with open(_WORKFLOWS_DIR / "dev-smoke.yml") as f:
        wf = yaml.safe_load(f)
    triggers = wf.get("on", {}) or wf.get(True, {})
    # GitHub Actions uses None key for bare 'on:' but yaml.safe_load
    # may use True as the key for 'on'
    push = triggers.get("push", {})
    branches = push.get("branches", []) if isinstance(push, dict) else []
    assert "dev" in branches, f"dev branch not in push triggers: {branches}"


@pytest.mark.smoke
def test_smoke_dev_full_suite_has_dispatch():
    """dev-full-suite.yml supports workflow_dispatch."""
    import yaml

    with open(_WORKFLOWS_DIR / "dev-full-suite.yml") as f:
        wf = yaml.safe_load(f)
    triggers = wf.get("on", {}) or wf.get(True, {})
    assert "workflow_dispatch" in triggers, "Missing workflow_dispatch trigger"


@pytest.mark.smoke
def test_smoke_readme_documents_ci():
    """README.md documents the CI workflows."""
    readme = _REPO_ROOT / "README.md"
    content = readme.read_text()
    assert "dev-smoke" in content, "README missing dev-smoke reference"
    assert "dev-full-suite" in content, "README missing dev-full-suite reference"


# ---------------------------------------------------------------------------
# Critical tests
# ---------------------------------------------------------------------------


@pytest.mark.critical
def test_critical_wl_cli_available():
    """Worklog CLI is available on PATH."""
    result = subprocess.run(["wl", "--version"], capture_output=True, text=True)
    assert result.returncode == 0, f"wl CLI not available: {result.stderr}"


@pytest.mark.critical
def test_critical_skills_directory_exists():
    """The skill/ directory exists with expected skills."""
    skill_dir = _REPO_ROOT / "skill"
    assert skill_dir.is_dir(), f"Missing {skill_dir}"
    expected = ["audit", "implement", "implement-single", "ralph", "triage"]
    for s in expected:
        assert (skill_dir / s).is_dir(), f"Missing skill: {s}"


@pytest.mark.critical
def test_critical_workflow_files_are_valid_yaml():
    """All GitHub workflow files parse as valid YAML."""
    import yaml

    for wf_file in sorted(_WORKFLOWS_DIR.glob("*.yml")):
        with open(wf_file) as f:
            data = yaml.safe_load(f)
        assert data is not None, f"{wf_file.name} is empty or invalid YAML"
        assert "name" in data, f"{wf_file.name} missing 'name' key"
        assert "jobs" in data, f"{wf_file.name} missing 'jobs' key"


@pytest.mark.critical
def test_critical_release_process_documented():
    """The release process is documented and references the dev-full-suite gate."""
    readme = _REPO_ROOT / "README.md"
    content = readme.read_text().lower()
    assert "release process" in content, "README missing release process section"
    assert "dev-full-suite" in content, "Release process does not reference dev-full-suite"
    assert "gate" in content or "block" in content, "Release process does not describe gating behaviour"
