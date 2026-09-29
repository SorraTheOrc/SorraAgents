"""Project-name resolution for skill reports.

Resolves a human-readable project name from the Worklog configuration so that
skill reports (refactor, standup, etc.) can include the project name in their
output heading.

Resolution order
----------------
1. ``projectName`` in ``<worklog_dir>/config.yaml``
2. Parent directory name of the ``.worklog`` directory
3. ``Path.cwd().name`` as a last resort

This helper accepts an explicit ``worklog_dir`` so that callers (and tests) are
deterministic and do not rely on the current working directory.
"""  # noqa: D205, D400

from __future__ import annotations

import os
from pathlib import Path

_WORKLOG_DIR_ENV = "WL_WORKLOG_DIR"


def _first_non_empty(*candidates: str | None) -> str:
    """Return the first non-empty candidate, or ``"unknown-project"``.

    Args:
        *candidates: Candidate name strings in priority order.

    Returns:
        The first non-empty, stripped candidate; ``"unknown-project"`` if all
        candidates are empty/whitespace.
    """
    for candidate in candidates:
        if candidate is None:
            continue
        value = str(candidate).strip()
        if value:
            return value
    return "unknown-project"


def resolve_project_name(worklog_dir: str | os.PathLike | None = None) -> str:
    """Resolve the project name from the Worklog configuration.

    Reads ``.worklog/config.yaml`` for a ``projectName`` key. Falls back to
    the worklog directory's parent name, then the current working directory
    name if the config is unavailable or the key is missing.

    Args:
        worklog_dir: Path to the ``.worklog`` directory. If ``None``, falls
            back to the ``WL_WORKLOG_DIR`` environment variable, then to the
            current working directory.

    Returns:
        A non-empty project name string. Never raises.
    """
    # Read the env var at call time so tests and callers can override it.
    wd = worklog_dir if worklog_dir is not None else os.environ.get(_WORKLOG_DIR_ENV)

    # ── Branch 1: projectName from config.yaml ──────────────────────────
    if wd:
        config_path = Path(wd) / "config.yaml"
        if config_path.is_file():
            try:
                import yaml  # PyYAML — already a project dependency.

                cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                name = (cfg or {}).get("projectName") if isinstance(cfg, dict) else None
                if name:
                    return _first_non_empty(str(name))
            except Exception:  # noqa: BLE001, S110
                # Config exists but is unreadable/malformed — fall through.
                pass

    # ── Branch 2: parent directory name of the worklog dir ──────────────
    parent_name: str | None = None
    if wd:
        try:
            parent_name = Path(wd).parent.name
        except Exception:  # noqa: BLE001, S110
            parent_name = None

    # ── Branch 3: current working directory name ────────────────────────
    cwd_name: str | None = None
    try:
        cwd_name = Path.cwd().name
    except Exception:  # noqa: BLE001, S110
        cwd_name = None

    return _first_non_empty(parent_name, cwd_name)
