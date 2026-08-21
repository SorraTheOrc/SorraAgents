"""Tests for implement.py sys.path resolution when the skills dir is a symlink.

These tests verify that `skill/implement/scripts/implement.py` is importable
(runs its module-level code) when invoked through the skills directory in
both layouts:

1. **Real-directory layout** — the script lives at ``<repo>/skill/implement/scripts/implement.py``.
2. **Symlinked layout** — the script is invoked through a symlink such as
   ``~/.pi/agent/skills -> <repo>/skill`` (the production layout on this
   machine).

The root cause was a sys.path computation that inserted
``_SKILLS_ROOT.parent / "skill"`` (e.g. ``<repo>/../skill``) instead of the
repo root itself, so ``from skill.shared.status_lifecycle import
StatusLifecycle`` failed with ``ModuleNotFoundError`` when the symlink was
resolved.

Related work item: SA-0MS9B91Q5002AXZ3
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_python(script_path: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Run implement.py as a subprocess with a pristine environment.

    PYTHONPATH is cleared so the test only exercises the script's own
    sys.path setup (no accidental resolution via environment).
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(script_path), "--help"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def _make_skill_layout(tmp_path: Path, use_symlink: bool) -> tuple[Path, Path]:
    """Stage a fake repo layout under tmp_path and return (repo_root, script_path).

    Copies the real skill/implement directory into a staged repo so we can
    create symlink / real-dir variants without touching the checked-in tree.

    Args:
        tmp_path: Pytest tmp dir.
        use_symlink: If True, invoke the script through a symlinked
            ``~/.pi/agent/skills`` path.

    Returns:
        A tuple of (repo_root, script_path) where script_path is the
        invocation path (through the symlink when use_symlink=True).
    """
    repo_root = tmp_path / "repo"
    skill_pkg = repo_root / "skill"
    (skill_pkg / "implement" / "scripts").mkdir(parents=True)
    (skill_pkg / "shared").mkdir(parents=True)

    # Copy the real implement.py into the staged repo
    shutil_copy = _IMPLEMENT_PY.read_text()
    (skill_pkg / "implement" / "scripts" / "implement.py").write_text(shutil_copy)

    # Provide the status_lifecycle module the import needs (stdlib-only deps)
    real_slc = (
        _REPO_ROOT / "skill" / "shared" / "status_lifecycle.py"
    ).read_text()
    (skill_pkg / "shared" / "status_lifecycle.py").write_text(real_slc)
    # Provide the code_freeze module (SA-0MSBU4OBU005WJNB) — implement.py
    # imports is_code_freeze_active from it at module load.
    real_cf = (_REPO_ROOT / "skill" / "shared" / "code_freeze.py").read_text()
    (skill_pkg / "shared" / "code_freeze.py").write_text(real_cf)
    # Provide the test cache + runner modules (SA-0MSGN5OJ4002OZKY) —
    # implement.py's run_tests() routes through the cache at import time.
    real_tc = (_REPO_ROOT / "skill" / "test_cache.py").read_text()
    (skill_pkg / "test_cache.py").write_text(real_tc)
    real_tr = (_REPO_ROOT / "skill" / "test_runner.py").read_text()
    (skill_pkg / "test_runner.py").write_text(real_tr)
    # Provide the graceful-failure guard (F3, SA-0MSWJ9ZEU001HDVT) —
    # implement.py imports guard_shared_import at module load.
    real_ig = (_REPO_ROOT / "skill" / "import_guard.py").read_text()
    (skill_pkg / "import_guard.py").write_text(real_ig)
    (skill_pkg / "__init__.py").touch()
    (skill_pkg / "shared" / "__init__.py").touch()

    if use_symlink:
        skills_link = tmp_path / "skills-link"
        skills_link.symlink_to(skill_pkg, target_is_directory=True)
        script_path = skills_link / "implement" / "scripts" / "implement.py"
    else:
        script_path = skill_pkg / "implement" / "scripts" / "implement.py"

    return repo_root, script_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestImplementSysPathResolution:
    """implement.py must resolve skill imports in both skills-dir layouts."""

    @pytest.mark.parametrize("use_symlink", [True, False], ids=["symlink", "real-dir"])
    def test_import_resolves_in_both_layouts(self, tmp_path, use_symlink):
        """--help must run without ModuleNotFoundError in both layouts."""
        repo_root, script_path = _make_skill_layout(tmp_path, use_symlink)
        result = _run_python(script_path, repo_root)

        assert result.returncode == 0, (
            f"implement.py failed to start in {('symlink' if use_symlink else 'real-dir')} layout\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "usage:" in result.stdout.lower() or "implement.py" in result.stdout.lower()

    def test_symlink_path_really_is_a_symlink(self, tmp_path):
        """Guard: the test's symlink scenario exercises a genuine symlink."""
        repo_root, script_path = _make_skill_layout(tmp_path, use_symlink=True)
        assert script_path.is_symlink() or script_path.parents[2].is_symlink()
        # The symlinked skills dir must resolve into the staged repo
        assert str(script_path.resolve()).startswith(str(repo_root))
