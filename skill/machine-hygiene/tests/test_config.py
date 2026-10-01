#!/usr/bin/env python3
"""
Tests for the machine-hygiene config loader.

Covers:
- AC3: config.yaml ships the documented default thresholds
- AC4: Config loader reads YAML with fallback defaults
- Config-file override behaviour
- Missing-file fallback
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Resolve the skill directory relative to this test file so the tests run
# both from the installed global skill directory and from a repo worktree.
_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load_config_module():
    """Load scripts/config.py under a unique name to avoid sys.path shadowing.

    Importing a bare ``config`` module by mutating ``sys.path`` risks
    colliding with any other ``config`` module collected by the same pytest
    process, so load it explicitly by file location instead.
    """
    spec = importlib.util.spec_from_file_location(
        "machine_hygiene_config", _SKILL_DIR / "scripts" / "config.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register so ``mock.patch("machine_hygiene_config._config_path")`` can
    # resolve the target by name.
    sys.modules["machine_hygiene_config"] = module
    spec.loader.exec_module(module)
    return module


_config = _load_config_module()
_DEFAULTS = _config._DEFAULTS
load_config = _config.load_config


class TestDefaults(unittest.TestCase):
    """Verify built-in defaults match documented values."""

    def test_cpu_pressure_defaults(self):
        cfg = _DEFAULTS["cpu_pressure"]
        self.assertEqual(cfg["low"], 0)
        self.assertEqual(cfg["warning"], 70)
        self.assertEqual(cfg["critical"], 85)

    def test_memory_usage_defaults(self):
        cfg = _DEFAULTS["memory_usage"]
        self.assertEqual(cfg["low"], 0)
        self.assertEqual(cfg["warning"], 80)
        self.assertEqual(cfg["critical"], 95)

    def test_io_pressure_defaults(self):
        cfg = _DEFAULTS["io_pressure"]
        self.assertEqual(cfg["low"], 0)
        self.assertEqual(cfg["warning"], 50)
        self.assertEqual(cfg["critical"], 80)

    def test_load_average_defaults(self):
        cfg = _DEFAULTS["load_average"]
        self.assertEqual(cfg["low"], 0)
        self.assertEqual(cfg["warning_multiplier"], 0.7)
        self.assertEqual(cfg["critical_multiplier"], 1.5)

    def test_zombie_processes_defaults(self):
        cfg = _DEFAULTS["zombie_processes"]
        self.assertEqual(cfg["low"], 0)
        self.assertEqual(cfg["warning"], 5)
        self.assertEqual(cfg["critical"], 20)

    def test_swap_usage_defaults(self):
        cfg = _DEFAULTS["swap_usage"]
        self.assertEqual(cfg["low"], 0)
        self.assertEqual(cfg["warning"], 30)
        self.assertEqual(cfg["critical"], 70)

    def test_runnable_processes_defaults(self):
        cfg = _DEFAULTS["runnable_processes"]
        self.assertEqual(cfg["low"], 0)
        self.assertEqual(cfg["warning_multiplier"], 2.0)
        self.assertEqual(cfg["critical_multiplier"], 4.0)

    def test_shipped_config_matches_defaults(self):
        """AC3: the shipped config.yaml encodes the documented defaults."""
        import yaml

        with open(_SKILL_DIR / "config.yaml", "r", encoding="utf-8") as fh:
            shipped = yaml.safe_load(fh)
        for key, default_val in _DEFAULTS.items():
            self.assertIn(key, shipped, f"config.yaml missing category {key}")
            self.assertEqual(shipped[key], default_val,
                             f"config.yaml {key} differs from documented default")


class TestLoadConfig(unittest.TestCase):
    """Config loading behaviour."""

    @mock.patch("machine_hygiene_config._config_path")
    def test_missing_file_returns_defaults(self, mock_path):
        mock_path.return_value = "/nonexistent/config.yaml"
        cfg = load_config()
        for key, default_val in _DEFAULTS.items():
            self.assertEqual(cfg[key], dict(default_val),
                             f"Key {key} should equal defaults when file is missing")

    @mock.patch("machine_hygiene_config._config_path")
    def test_override_single_value(self, mock_path):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as fh:
            fh.write("cpu_pressure:\n  warning: 99\n")
            temp_path = fh.name

        try:
            mock_path.return_value = temp_path
            cfg = load_config()
            self.assertEqual(cfg["cpu_pressure"]["warning"], 99)
            # Other keys should retain defaults
            self.assertEqual(cfg["cpu_pressure"]["critical"], 85)
            self.assertEqual(cfg["memory_usage"]["warning"], 80)
        finally:
            os.unlink(temp_path)

    @mock.patch("machine_hygiene_config._config_path")
    def test_partial_file_preserves_other_categories(self, mock_path):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as fh:
            fh.write("zombie_processes:\n  warning: 10\n  critical: 50\n")
            temp_path = fh.name

        try:
            mock_path.return_value = temp_path
            cfg = load_config()
            self.assertEqual(cfg["zombie_processes"]["warning"], 10)
            self.assertEqual(cfg["zombie_processes"]["critical"], 50)
            # cpu_pressure should still be default
            self.assertEqual(cfg["cpu_pressure"]["warning"], 70)
        finally:
            os.unlink(temp_path)

    @mock.patch("machine_hygiene_config._config_path")
    def test_empty_file_returns_defaults(self, mock_path):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as fh:
            fh.write("")
            temp_path = fh.name

        try:
            mock_path.return_value = temp_path
            cfg = load_config()
            for key, default_val in _DEFAULTS.items():
                self.assertEqual(cfg[key], dict(default_val))
        finally:
            os.unlink(temp_path)

    @mock.patch("machine_hygiene_config._config_path")
    def test_load_config_returns_dict(self, mock_path):
        mock_path.return_value = "/nonexistent/config.yaml"
        cfg = load_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn("cpu_pressure", cfg)
        self.assertIn("memory_usage", cfg)
        self.assertIn("io_pressure", cfg)
        self.assertIn("load_average", cfg)
        self.assertIn("zombie_processes", cfg)
        self.assertIn("swap_usage", cfg)
        self.assertIn("runnable_processes", cfg)

    def test_real_config_loads(self):
        """AC4: load_config() reads the shipped config.yaml."""
        cfg = load_config()
        self.assertIsInstance(cfg, dict)
        self.assertEqual(cfg["cpu_pressure"]["warning"], 70)


if __name__ == "__main__":
    unittest.main()
