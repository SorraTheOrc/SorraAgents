#!/usr/bin/env python3
"""Regression guard: untracked ``skill/<new-skill>/`` directories do not
break ``pytest`` collection (SA-0MUPDDMXB0088CMM).

When a concurrent agent leaves an untracked skill directory in the main
checkout, its ``tests/`` subdirectory would otherwise collide with the
top-level ``tests`` package — ``ModuleNotFoundError: No module named
'tests.<name>'``.  The repo-root conftest hooks
``pytest_ignore_collect`` to skip any ``skill/<name>/`` directory that is
not tracked by git, so the full suite always passes regardless of concurrent
WIP.

These tests verify that behaviour:

- AC1: a transient untracked ``skill/<name>/tests/`` directory with a test
  file that imports ``tests.<something>`` is silently ignored during
  collection.
- AC2: the same directory is still discovered by the *actual* test (not
  pytest collection) — proving the guard is selective, not blind.
"""  # noqa: EXE001

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

_SKILLS_DIR = REPO_ROOT / "skill"
_TEMP_SKILL = _SKILLS_DIR / "temp-pytest-isolation-test-skill"


def _run(*args: str, cwd: Path | None = None, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "LIVE_REPO_GUARD_ACTIVE": "1",
            **dict(__import__("os").environ),
        },
    )


class TestUntrackedSkillDirectoryIsIgnored:
    """AC1: an untracked skill directory with a colliding test is ignored."""

    @classmethod
    def setup_class(cls):
        """Create an untracked skill directory with a test that would
        otherwise fail during collection."""
        _TEMP_SKILL.mkdir(parents=True, exist_ok=True)
        (_TEMP_SKILL / "__init__.py").write_text(
            "# temp isolation skill", encoding="utf-8"
        )
        tests_dir = _TEMP_SKILL / "tests"
        tests_dir.mkdir(exist_ok=True)
        # This file would trigger the exact ImportError from the bug report.
        tests_dir.joinpath("test_collide.py").write_text(
            "from tests.test_config import something\n",
            encoding="utf-8",
        )

    @classmethod
    def teardown_class(cls):
        """Remove the untracked skill directory."""
        if _TEMP_SKILL.exists():
            import shutil
            shutil.rmtree(_TEMP_SKILL)

    def test_collection_succeeds_with_untracked_skill_present(self):
        """pytest --collect-only must succeed even though the untracked
        directory contains a test that imports a non-existent module."""
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
        node_ids = _run(
            sys.executable, "-m", "pytest",
            "--collect-only", "-q",
            "-p", "no:cacheprovider",
            "-o", "addopts=",
            "tests/test_nested_skill_skill_guard.py",
        ).stdout.strip()
        assert "temp-pytest-isolation-test-skill" not in node_ids, (
            "untracked skill directory was collected"
        )

    def test_tracked_machine_hygiene_still_collected(self):
        """A tracked skill directory must still be collected normally.

        Run collection broadly so skill/shared tests appear.
        """
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
