#!/usr/bin/env python3
"""Regression guard: no accidental nested ``skill/skill/`` artefact.

LP-0MUHOHP82006NT3W: an absolute symlink ``skill/skill/shared`` (plus the
empty ``skill/skill/__init__.py`` package marker) was tracked by mistake. It
duplicated every ``skill/shared`` test under a second node-id prefix and, in a
git worktree, the symlink resolved to the MAIN checkout's copy — so the
full-suite gate could test a worktree change against two different revisions.

These tests pin the removal and the reintroduction guard:

- no path under ``skill/skill`` is tracked or resolvable (AC1);
- the path is ignored, so it cannot be silently re-added (AC4);
- a from-root ``pytest --collect-only`` produces no ``skill/skill/...`` node
  ids and every collected ``skill/shared`` test resolves under this checkout
  (AC2/AC3).

They run unchanged in the main checkout and in a git worktree: ``REPO_ROOT``
is always the checkout under test, so a nested copy that resolved elsewhere
would fail the resolution assertions.
"""  # noqa: EXE001

from __future__ import annotations

import functools
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NESTED_DIR = REPO_ROOT / "skill" / "skill"
NESTED_SHARED = NESTED_DIR / "shared"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command from the repository root."""
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


@functools.lru_cache(maxsize=1)
def _collect_node_ids() -> tuple[str, ...]:
    """Collect node ids from the repository root, with no masking config.

    Runs a *subprocess* pytest so it observes the on-disk repository exactly
    as a maintainer's ``pytest --collect-only`` would. ``-o addopts=`` clears
    any configured ``--ignore`` so a reintroduced artefact cannot hide behind
    the very mitigation this change removed, and the environment/cache flags
    keep the collection side-effect free (the live-repo mutation guard in the
    parent session must not see a spurious checkout change).
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LIVE_REPO_GUARD_ACTIVE"] = "1"
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q",
            "-p", "no:cacheprovider",
            "-o", "addopts=",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"pytest --collect-only failed ({proc.returncode}):\n"
        f"{proc.stderr[-4000:]}"
    )
    return tuple(line.strip() for line in proc.stdout.splitlines() if "::" in line)


class TestNoNestedSkillSkillArtefact:
    """AC1/AC4: the tracked artefact is gone and cannot come back silently."""

    def test_no_skill_skill_path_is_tracked(self):
        result = _git("ls-files", "--", "skill/skill")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", (
            f"unexpected tracked artefact(s) under skill/skill/: {result.stdout}"
        )

    def test_nested_shared_path_is_absent(self):
        assert not NESTED_SHARED.exists(), (
            "skill/skill/shared exists: it duplicates skill/shared collection "
            "and may resolve to another checkout's revision"
        )
        assert not NESTED_SHARED.is_symlink(), "skill/skill/shared is a dangling symlink"
        assert not (NESTED_SHARED / "status_lifecycle.py").exists()
        assert not (NESTED_DIR / "__init__.py").exists()

    def test_gitignore_blocks_reintroduction(self):
        result = _git("check-ignore", "-v", "skill/skill/shared")
        assert result.returncode == 0, (
            "skill/skill/ is not covered by .gitignore; a re-added artefact "
            "would be tracked silently"
        )
        assert "skill/skill" in result.stdout

    def test_pytest_ini_retains_no_skill_skill_ignore(self):
        lines = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8").splitlines()
        active = [ln for ln in lines if not ln.lstrip().startswith("#")]
        offenders = [ln for ln in active if "--ignore=skill/skill" in ln]
        assert offenders == [], (
            "the interim --ignore=skill/skill mitigation is still active "
            f"(AC5): {offenders}"
        )


class TestCollectionHasNoDuplicates:
    """AC2/AC3: collection is unique and resolves under this checkout."""

    def test_no_skill_skill_node_ids(self):
        node_ids = _collect_node_ids()
        offenders = [n for n in node_ids if n.startswith("skill/skill/")]
        assert offenders == [], (
            f"{len(offenders)} duplicate skill/skill node id(s) collected, "
            f"e.g. {offenders[:5]}"
        )

    def test_every_collected_node_id_is_unique(self):
        node_ids = _collect_node_ids()
        duplicates = len(node_ids) - len(set(node_ids))
        assert duplicates == 0, f"{duplicates} duplicate node id(s) collected"

    def test_skill_shared_tests_resolve_under_this_checkout(self):
        node_ids = _collect_node_ids()
        shared = [n for n in node_ids if n.startswith("skill/shared/")]
        assert shared, "no skill/shared tests were collected"
        for node_id in shared:
            path = (REPO_ROOT / node_id.split("::", 1)[0]).resolve()
            assert path == REPO_ROOT or REPO_ROOT in path.parents, (
                f"{node_id} resolves outside this checkout: {path}"
            )

    def test_shared_package_resolves_under_this_checkout(self):
        from shared import status_lifecycle as module

        module_path = Path(module.__file__).resolve()
        assert module_path == REPO_ROOT or REPO_ROOT in module_path.parents, (
            f"shared.status_lifecycle resolves outside this checkout: "
            f"{module_path}"
        )
