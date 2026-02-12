"""Fallback configuration helpers for interactive sessions."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Optional

VALID_MODES = {"hold", "auto-accept", "auto-decline"}


def _tool_output_dir() -> str:
    path = os.getenv("AMPA_TOOL_OUTPUT_DIR")
    if path:
        return path
    return os.path.join(tempfile.gettempdir(), "opencode_tool_output")


def normalize_mode(value: Optional[str]) -> str:
    if not value:
        return "hold"
    raw = str(value).strip().lower()
    aliases = {
        "auto_accept": "auto-accept",
        "auto-accept": "auto-accept",
        "auto_decline": "auto-decline",
        "auto-decline": "auto-decline",
        "accept": "auto-accept",
        "decline": "auto-decline",
        "hold": "hold",
        "pause": "hold",
        "queue": "hold",
    }
    mode = aliases.get(raw, "hold")
    if mode not in VALID_MODES:
        return "hold"
    return mode


def config_path(tool_output_dir: Optional[str] = None) -> str:
    override = os.getenv("AMPA_FALLBACK_CONFIG_FILE")
    if override:
        return override
    base = tool_output_dir or _tool_output_dir()
    return os.path.join(base, "ampa_fallback_config.json")


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {"default": "hold", "projects": {}}
    if not isinstance(data, dict):
        return {"default": "hold", "projects": {}}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    default_mode = normalize_mode(data.get("default"))
    normalized_projects: Dict[str, str] = {}
    for key, value in projects.items():
        if not key:
            continue
        normalized_projects[str(key)] = normalize_mode(value)
    return {"default": default_mode, "projects": normalized_projects}


def save_config(config: Dict[str, Any], path: Optional[str] = None) -> Dict[str, Any]:
    path = path or config_path()
    normalized = {
        "default": normalize_mode(config.get("default")),
        "projects": {},
    }
    projects = config.get("projects")
    if isinstance(projects, dict):
        for key, value in projects.items():
            if not key:
                continue
            normalized["projects"][str(key)] = normalize_mode(value)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2, sort_keys=True)
    except Exception:
        pass
    return normalized


def resolve_mode(
    project_id: Optional[str],
    *,
    tool_output_dir: Optional[str] = None,
    env_override: bool = True,
) -> str:
    if env_override:
        env_mode = os.getenv("AMPA_FALLBACK_MODE")
        if env_mode:
            return normalize_mode(env_mode)
    cfg = load_config(config_path(tool_output_dir))
    projects = cfg.get("projects") or {}
    if project_id:
        project_key = str(project_id)
        if project_key in projects:
            return normalize_mode(projects.get(project_key))
    return normalize_mode(cfg.get("default"))
