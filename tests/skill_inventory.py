"""Locate git-tracked ``skill/<name>/`` directories.

A concurrent agent may leave an untracked ``skill/<name>/`` WIP directory in
the main checkout. Doc-convention guards that scan every ``skill/*/SKILL.md``
must ignore those dirs: the untracked docs are uncommitted, unreviewed work
and must not turn the full suite red in the main checkout
(SA-0MUU206QT001M66F / SA-0MUQ96V5W000TMTU).

This mirrors the production filter in
``skill/context-audit/scripts/measure_context.py`` (``tracked_skill_dirs``)
and the pytest collection guard in the repo-root ``conftest.py``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def tracked_skill_names(repo_root: Path | None = None) -> set[str] | None:
    """Return the names of the ``skill/<name>/`` dirs tracked by git.

    Returns ``None`` when git is unavailable (e.g. an exported tarball or a
    non-git checkout), so callers can fall back to including every directory
    rather than silently skipping the whole scan.
    """
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--", "skill/"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split("/")
        if len(parts) >= 2 and parts[0] == "skill":
            names.add(parts[1])
    return names
