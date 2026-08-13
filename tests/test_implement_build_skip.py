"""Tests for implement.py run_build() build-script skip behaviour.

Contract (per work item SA-0MSM6JHG9002CZA1 ACs):

- AC1: run_build() reports a skipped/no-op success when the repo root
  package.json has no ``build`` script (or no package.json at all), so
  ``implement.py finish`` proceeds to tests → commit → push instead of
  aborting on ``npm run build`` exit 1 (``Missing script: "build"``).
- AC2: repos WITH a ``scripts.build`` entry are unaffected — ``npm run
  build`` still runs and a real build failure still blocks finish
  (``success`` False).
- AC3: unit tests cover both paths (no build script → skipped; build
  script present → runs, failure blocks).

Background: ``run_build()`` unconditionally ran ``npm run build`` and
treated any non-zero exit as fatal, so Python-only repos (no build
script) aborted the finish phase for every implementation. The fix
detects the absence of a ``scripts.build`` entry in the root
package.json and reports the build step as a skipped no-op instead.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py).

    Loaded via importlib so the module's own ``_REPO_ROOT`` computation and
    ``skill.*`` imports resolve against the real repo.
    """
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_build_skip", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_build_skip"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    """Scratch directory standing in for the worktree root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _write_package_json(repo_dir: Path, content: dict) -> None:
    """Write a root package.json in the scratch repo dir."""
    (repo_dir / "package.json").write_text(json.dumps(content))


# ---------------------------------------------------------------------------
# AC1: no build script → build step is a skipped no-op (never a failure)
# ---------------------------------------------------------------------------


def test_build_skipped_when_no_package_json(implement_mod, repo_dir):
    """No root package.json at all → build reports a skipped no-op."""
    result = implement_mod.run_build(str(repo_dir))

    assert result["success"] is True, (
        f"finish must not abort on a missing build script: {result}"
    )
    assert result["exit_code"] == 0
    assert result["skipped"] is True
    assert "skipping build" in result["stdout"].lower(), (
        "skipped build step must be reported as a no-op in stdout"
    )


def test_build_skipped_when_package_json_has_no_build_script(
    implement_mod, repo_dir
):
    """package.json without scripts.build (e.g. Python-only repo) → skipped."""
    _write_package_json(repo_dir, {"scripts": {"test": "pytest"}})

    result = implement_mod.run_build(str(repo_dir))

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["skipped"] is True


def test_build_skipped_when_package_json_is_malformed(implement_mod, repo_dir):
    """Malformed package.json must not block finish — treat as no build step."""
    (repo_dir / "package.json").write_text("{ not valid json")

    result = implement_mod.run_build(str(repo_dir))

    assert result["success"] is True
    assert result["skipped"] is True


def test_build_skipped_when_scripts_is_not_a_dict(implement_mod, repo_dir):
    """scripts present but not an object → no build script → skipped."""
    _write_package_json(repo_dir, {"scripts": "build"})

    result = implement_mod.run_build(str(repo_dir))

    assert result["success"] is True
    assert result["skipped"] is True


# ---------------------------------------------------------------------------
# AC2: build script present → npm run build runs; failures still block
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("npm") is None, reason="npm not available on this host"
)
def test_build_runs_when_build_script_present(implement_mod, repo_dir):
    """scripts.build present → npm run build actually executes the script."""
    _write_package_json(repo_dir, {"scripts": {"build": "echo build-ran"}})

    result = implement_mod.run_build(str(repo_dir))

    assert result["success"] is True
    assert result["skipped"] is False
    assert result["exit_code"] == 0
    assert "build-ran" in result["stdout"], (
        f"npm run build must have executed the script; stdout: {result['stdout']}"
    )


@pytest.mark.skipif(
    shutil.which("npm") is None, reason="npm not available on this host"
)
def test_build_failure_still_blocks_when_build_script_present(
    implement_mod, repo_dir
):
    """scripts.build present + failing build → finish must be blocked."""
    _write_package_json(repo_dir, {"scripts": {"build": "exit 1"}})

    result = implement_mod.run_build(str(repo_dir))

    assert result["success"] is False
    assert result["skipped"] is False
    assert result["exit_code"] != 0
