#!/usr/bin/env python3
"""
Machine-hygiene config loader.

Reads ~/.pi/agent/skills/machine-hygiene/config.yaml and returns a
normalised dictionary.  Missing keys fall back to the built-in defaults.

Usage
-----
    from config import load_config

    cfg = load_config()
    cpu_warn = cfg['cpu_pressure']['warning']
"""

import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Built-in defaults (mirrors config.yaml)
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, Any] = {
    "cpu_pressure": {
        "low": 0,
        "warning": 70,
        "critical": 85,
    },
    "memory_usage": {
        "low": 0,
        "warning": 80,
        "critical": 95,
    },
    "io_pressure": {
        "low": 0,
        "warning": 50,
        "critical": 80,
    },
    "load_average": {
        "low": 0,
        "warning_multiplier": 0.7,
        "critical_multiplier": 1.5,
    },
    "zombie_processes": {
        "low": 0,
        "warning": 5,
        "critical": 20,
    },
    "swap_usage": {
        "low": 0,
        "warning": 30,
        "critical": 70,
    },
    "runnable_processes": {
        "low": 0,
        "warning_multiplier": 2.0,
        "critical_multiplier": 4.0,
    },
}


def _config_path() -> str:
    """Return the filesystem path to the config.yaml.

    Resolved relative to this file so the loader works both from the
    installed global skill directory and from a repo worktree.
    """
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


def load_config() -> dict[str, Any]:
    """Load and return the machine-hygiene configuration.

    Reads *config.yaml* from the skill directory.  Any keys present in the
    file override the built-in defaults; missing keys retain defaults.

    Returns
    -------
    dict
        Merged configuration dictionary.

    Raises
    ------
    ImportError
        When PyYAML is not installed.
    OSError
        When the config file cannot be read.
    """
    # Lazy import to avoid hard dependency at import time
    import yaml

    cfg = {}
    for key, default_val in _DEFAULTS.items():
        cfg[key] = dict(default_val)  # shallow copy

    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            file_cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return cfg
    except OSError as exc:
        # If we can't read the file, fall back to defaults and warn
        print(
            f"WARNING: Could not read config file {path}: {exc}",
            file=sys.stderr,
        )
        return cfg

    # Deep-merge file values over defaults
    for key, file_val in file_cfg.items():
        if key not in cfg:
            cfg[key] = {}
        if isinstance(file_val, dict):
            cfg[key].update(file_val)
        else:
            cfg[key] = file_val

    return cfg
