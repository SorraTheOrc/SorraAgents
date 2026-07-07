"""Session boundary detection for the refactor step.

Identifies files modified in the current implementation session by
comparing the current branch against a parent branch (default: ``dev``).

Usage:

    from skill.refactor.session_boundary import (
        get_changed_files,
        get_untracked_files,
        get_session_files,
        has_changes,
    )

    files = get_changed_files(parent_branch="dev")
    untracked = get_untracked_files()
    session_files = get_session_files(parent_branch="dev")
    changed = has_changes(parent_branch="dev")
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

if sys.version_info >= (3, 11):
    from typing import Self
else:
    Self = Any  # pragma: no cover


LOG = logging.getLogger("refactor.session_boundary")

# Default parent branch name used for session boundary detection.
DEFAULT_PARENT_BRANCH = "dev"

# Git command templates.
_GIT_DIFF_NAMESTATUS = ["git", "diff", "--name-status"]
_GIT_LS_UNTRACKED = ["git", "ls-files", "--others", "--exclude-standard"]
_GIT_DIFF_EXIT_CODE = ["git", "diff", "--exit-code"]
_GIT_MERGE_BASE = ["git", "merge-base"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_changed_files(
    parent_branch: str = DEFAULT_PARENT_BRANCH,
) -> list[dict[str, str]]:
    """Return files modified in the current session compared to *parent_branch*.

    Each entry in the returned list is a dict with at least ``status`` and
    ``file`` keys.  Renamed files also include an ``old_file`` key.

    When a merge-base between *parent_branch* and ``HEAD`` exists, the diff
    is performed against the merge-base to produce accurate results across
    merge commits.  Falls back to a direct diff against *parent_branch* when
    the merge-base computation fails.
    """
    merge_base = _get_merge_base(parent_branch)
    if merge_base is not None:
        return _run_diff(merge_base)

    return _run_diff(parent_branch)


def get_untracked_files() -> list[str]:
    """Return a list of untracked file paths (relative to repo root)."""
    try:
        proc = subprocess.run(
            _GIT_LS_UNTRACKED,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        LOG.warning("Failed to list untracked files: %s", exc)
        return []

    output = (proc.stdout or "").strip()
    if not output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def get_session_files(
    parent_branch: str = DEFAULT_PARENT_BRANCH,
) -> list[dict[str, str]]:
    """Combine changed and untracked files into a single session file list.

    Untracked files are marked with status ``?``.
    """
    changed = get_changed_files(parent_branch=parent_branch)
    untracked = get_untracked_files()
    result = list(changed)
    for path in untracked:
        # Avoid duplicates: skip if the path already appears in changed files.
        if not any(f["file"] == path for f in changed):
            result.append({"status": "?", "file": path})
    return result


def has_changes(parent_branch: str = DEFAULT_PARENT_BRANCH) -> bool:
    """Check whether there are any changes compared to *parent_branch*.

    Returns ``True`` if the diff is non-empty, ``False`` otherwise.
    The ``--exit-code`` flag makes ``git diff`` return 0 when there are no
    changes and 1 when there are changes.
    """
    try:
        cmd = _GIT_DIFF_EXIT_CODE + [parent_branch]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        LOG.warning("Failed to check for changes: %s", exc)
        return False
    return proc.returncode != 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_merge_base(parent_branch: str) -> str | None:
    """Return the merge-base commit hash between *parent_branch* and HEAD.

    Returns ``None`` when the merge-base cannot be determined (e.g. the
    parent branch is not a valid reference).
    """
    try:
        cmd = _GIT_MERGE_BASE + [parent_branch, "HEAD"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            commit = (proc.stdout or "").strip()
            return commit if commit else None
    except Exception as exc:
        LOG.debug("merge-base failed: %s", exc)
    return None


def _run_diff(commit: str) -> list[dict[str, str]]:
    """Run ``git diff --name-status <commit>`` and parse the output."""
    try:
        cmd = _GIT_DIFF_NAMESTATUS + [commit]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        LOG.warning("git diff failed for %s: %s", commit, exc)
        return []

    if proc.returncode != 0:
        LOG.warning(
            "git diff returned non-zero (%d) for %s: %s",
            proc.returncode,
            commit,
            (proc.stderr or "").strip(),
        )
        return []

    output = (proc.stdout or "").strip()
    if not output:
        return []

    files: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            # Malformed line; skip.
            continue
        status = parts[0]
        file_path = parts[-1]  # Last segment is the destination path.
        entry: dict[str, str] = {"status": status, "file": file_path}
        # Renamed files have the old path as the middle segment.
        if len(parts) >= 3 and status.startswith("R"):
            entry["old_file"] = parts[1]
        files.append(entry)

    return files
