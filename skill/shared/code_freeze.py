#!/usr/bin/env python3
"""Shared Code Freeze marker helper (SA-0MSBU4OBU005WJNB).

Reads the Code Freeze marker at ``<project-root>/.worklog/code-freeze.json``
and reports whether a ship release is currently in progress.

Marker contract (cross-repo, see WL-0MSBU4KMA004PKSR)::

    {
      "active": true,
      "reason": "ship release in progress",
      "startedAt": "<ISO>",
      "pid": <pid>
    }

Fail-open semantics: a missing or corrupt marker is treated as NOT frozen so
that a stale/broken marker never blocks legitimate implementation work. A
marker with ``active: true`` IS frozen (conservative default — rely on the
ship-side trap/finally cleanup to remove the marker; a stale marker can be
removed manually by deleting ``.worklog/code-freeze.json``).
"""  # noqa: EXE001

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

LOG = logging.getLogger("skill.shared.code_freeze")

MARKER_FILENAME = "code-freeze.json"


# ======================================================================
# Worklog-dir / marker path resolution
# ======================================================================


def _detect_worklog_dir(project_root: str | Path | None = None) -> Path | None:
    """Detect the target project's ``.worklog`` directory.

    Resolution order (mirrors ``wl --worklog-dir`` / status_lifecycle):
      1. explicit ``project_root/.worklog`` (when provided)
      2. ``<cwd>/.worklog``
      3. ``<git root>/.worklog`` via ``git rev-parse --show-toplevel``
      4. nearest ancestor directory containing ``.worklog``

    Returns ``None`` when no worklog directory can be resolved.
    """
    if project_root is not None:
        cand = Path(project_root) / ".worklog"
        return cand if cand.is_dir() else None

    cwd = Path.cwd()
    cand = cwd / ".worklog"
    if cand.is_dir():
        return cand
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            cand = Path(proc.stdout.strip()) / ".worklog"
            if cand.is_dir():
                return cand
    except OSError:
        pass
    for parent in cwd.parents:
        cand = parent / ".worklog"
        if cand.is_dir():
            return cand
    return None


def code_freeze_marker_path(project_root: str | Path | None = None) -> Path:
    """Resolve the Code Freeze marker path for a project.

    Args:
        project_root: Explicit project root. When ``None`` the marker path is
            resolved from cwd / git root / nearest ancestor (same resolution
            as ``wl --worklog-dir``).

    Returns:
        ``Path`` to ``<project-root>/.worklog/code-freeze.json``. When no
        worklog directory can be resolved, falls back to
        ``<cwd>/.worklog/code-freeze.json``.
    """
    wl_dir = _detect_worklog_dir(project_root)
    if wl_dir is not None:
        return wl_dir / MARKER_FILENAME
    return Path.cwd() / ".worklog" / MARKER_FILENAME


# ======================================================================
# Marker read
# ======================================================================


def is_code_freeze_active(project_root: str | Path | None = None) -> bool:
    """Return True when a Code Freeze is active for the project.

    Fail-open semantics:

    - missing marker file → ``False``
    - unreadable/corrupt (unparseable) marker → ``False``
    - valid JSON that is not an object → ``False``
    - valid object with ``active`` falsy → ``False``
    - valid object with ``active`` truthy → ``True``

    Args:
        project_root: Explicit project root. When ``None`` the marker path is
            resolved from cwd / git root / nearest ancestor (same resolution
            as ``wl --worklog-dir``), so the helper works from the main
            checkout, a worktree, or any subdirectory.

    Returns:
        ``True`` when the marker exists and declares ``active: true``.
    """
    marker = code_freeze_marker_path(project_root)
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as exc:
        LOG.warning("Code Freeze marker unreadable (%s): %s", marker, exc)
        return False

    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        LOG.warning("Code Freeze marker corrupt (%s): %s", marker, exc)
        return False

    if not isinstance(data, dict):
        LOG.warning("Code Freeze marker is not a JSON object (%s)", marker)
        return False

    return bool(data.get("active", False))
