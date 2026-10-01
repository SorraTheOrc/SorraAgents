#!/usr/bin/env python3
"""Regression guard: untracked ``skill/<new-skill>/`` directories do not
break the full suite (SA-0MUPDDMXB0088CMM).

When a concurrent agent leaves an untracked skill directory in the main
checkout, two independent failure modes appear:

1. **Collection.** Its ``tests/`` subdirectory collides with the top-level
   ``tests`` package (``ModuleNotFoundError: No module named
   'tests.<name>'``). The repo-root conftest hooks
   ``pytest_ignore_collect`` to skip any untracked ``skill/<name>/`` dir.
2. **Inventory.** The skill-hygiene tests discover the unwired ``SKILL.md``.
   ``measure_context.tracked_skill_dirs`` filters it out and
   ``untracked_skill_dirs`` surfaces it for a clear warning.

These tests reproduce both scenarios and verify the tracked-skill path is
unaffected.
"""  # noqa: EXE001

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SKILLS_DIR = REPO_ROOT / "skill"
_TEMP_SKILL = _SKILLS_DIR / "temp-pytest-isolation-test-skill"

# Load the context-audit measurement module by path (single source of truth
# for the tracked-skill inventory).
_MEASURE_DIR = REPO_ROOT / "skill" / "context-audit" / "scripts"
if str(_MEASURE_DIR) not in sys.path:
    sys.path.insert(0, str(_MEASURE_DIR))

import measure_context as mc


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "LIVE_REPO_GUARD_ACTIVE": "1",
            **dict(os.environ),
        },
    )


class _TempUntrackedSkill:
    """Context helper: create/remove an untracked skill directory."""

    def __init__(self, *, with_skill_md: bool = False):
        self.with_skill_md = with_skill_md

    def __enter__(self) -> Path:
        _TEMP_SKILL.mkdir(parents=True, exist_ok=True)
        (_TEMP_SKILL / "__init__.py").write_text(
            "# temp isolation skill", encoding="utf-8"
        )
        if self.with_skill_md:
            (_TEMP_SKILL / "SKILL.md").write_text(
                "---\n"
                "name: temp-pytest-isolation-test-skill\n"
                "description: Temp untracked skill for isolation tests.\n"
                "---\n\n# Temp\n",
                encoding="utf-8",
            )
        tests_dir = _TEMP_SKILL / "tests"
        tests_dir.mkdir(exist_ok=True)
        # Would trigger the exact ImportError from the bug report.
        (tests_dir / "test_collide.py").write_text(
            "from tests.test_config import something\n",
            encoding="utf-8",
        )
        return _TEMP_SKILL

    def __exit__(self, *exc: object) -> None:
        if _TEMP_SKILL.exists():
            shutil.rmtree(_TEMP_SKILL)


class TestUntrackedSkillCollectionIsolation:
    """AC1: an untracked skill dir cannot break pytest collection."""

    def test_collection_succeeds_with_untracked_skill_present(self):
        """pytest --collect-only must succeed even though the untracked
        directory contains a test that imports a non-existent module."""
        with _TempUntrackedSkill():
            result = _run(
                sys.executable, "-m", "pytest",
                "--collect-only", "-q",
                "-p", "no:cacheprovider",
                "-o", "addopts=",
                "tests/test_nested_skill_skill_guard.py",
            )
        assert result.returncode == 0, (
            f"collection failed with untracked skill dir present:\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )

    def test_untracked_skill_not_in_collected_node_ids(self):
        """The untracked skill's test must NOT appear in collected node ids."""
        with _TempUntrackedSkill():
            result = _run(
                sys.executable, "-m", "pytest",
                "--collect-only", "-q",
                "-p", "no:cacheprovider",
                "-o", "addopts=",
                "skill/machine-hygiene/tests",
                "tests/test_nested_skill_skill_guard.py",
            )
        assert "temp-pytest-isolation-test-skill" not in result.stdout, (
            "untracked skill directory was collected"
        )

    def test_tracked_skill_is_still_collected(self):
        """A tracked skill directory must still be collected normally."""
        result = _run(
            sys.executable, "-m", "pytest",
            "--collect-only", "-q",
            "-p", "no:cacheprovider",
            "-o", "addopts=",
            "skill/machine-hygiene/tests",
        )
        assert result.returncode == 0
        assert "skill/machine-hygiene/tests" in result.stdout, (
            "tracked skill 'machine-hygiene' was incorrectly skipped"
        )

    def test_collection_emits_clear_untracked_warning(self):
        """The suite reports a clear, actionable warning instead of a
        confusing ImportError (AC2)."""
        with _TempUntrackedSkill():
            result = _run(
                sys.executable, "-m", "pytest",
                "--collect-only", "-q",
                "-p", "no:cacheprovider",
                "-o", "addopts=",
                "tests/test_nested_skill_skill_guard.py",
            )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined[-2000:]
        assert "untracked skill dir" in combined, combined[-2000:]
        assert "temp-pytest-isolation-test-skill" in combined


class TestUntrackedSkillInventoryIsolation:
    """AC1: the skill inventory ignores untracked skill dirs."""

    def test_untracked_skill_reported(self):
        with _TempUntrackedSkill(with_skill_md=True):
            assert "temp-pytest-isolation-test-skill" in mc.untracked_skill_dirs(
                REPO_ROOT
            )

    def test_untracked_skill_excluded_from_prose(self):
        with _TempUntrackedSkill(with_skill_md=True):
            prose = mc.skill_description_prose(REPO_ROOT, include_hidden=True)
            assert "temp-pytest-isolation-test-skill" not in prose
            # The tracked inventory is unaffected.
            assert "machine-hygiene" in prose

    def test_tracked_skill_not_reported_as_untracked(self):
        # A clean checkout has no untracked skill dirs.
        assert "machine-hygiene" not in mc.untracked_skill_dirs(REPO_ROOT)
