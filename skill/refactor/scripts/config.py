"""Configuration system for the refactor skill.

Provides a ``RefactorConfig`` class that wraps the rule loading from
``smell_detection.load_rules()`` and adds convenience properties and
validation.

Usage:

    from skill.refactor.scripts.config import RefactorConfig

    config = RefactorConfig.load(".refactor.json")
    if config.enabled:
        linter_cfg = config.linter_config
        llm_cfg = config.llm_config
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


LOG = logging.getLogger("refactor.config")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = ".refactor.json"

DEFAULT_LINTER_CONFIG: dict[str, Any] = {
    "enabled": True,
    "severity_overrides": {},
}

DEFAULT_LLM_CONFIG: dict[str, Any] = {
    "enabled": True,
    "model": "default",
    "temperature": 0.1,
    "max_tokens": 2000,
}

DEFAULT_SEVERITY_MAPPING: dict[str, dict[str, str]] = {
    "critical": {"priority": "critical", "color": "red"},
    "high": {"priority": "high", "color": "orange"},
    "medium": {"priority": "medium", "color": "yellow"},
    "low": {"priority": "low", "color": "green"},
}

DEFAULT_SMELL_TYPES: list[str] = [
    "unused_import",
    "unused_variable",
    "unused_function",
    "complex_function",
    "magic_number",
    "duplicate_code",
    "long_method",
    "god_class",
    "feature_envy",
    "inappropriate_intimacy",
    "shotgun_surgery",
]


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class RefactorConfig:
    """Immutable configuration for the refactor skill.

    Attributes:
        enabled: Whether the refactor step is enabled.
        linter_config: Configuration for linter-based detection.
        llm_config: Configuration for LLM-based detection.
        severity_mapping: Mapping from severity levels to priority/color.
        smell_types: List of supported smell types.
        raw: The raw configuration dict (for debugging).
    """

    enabled: bool = True
    linter_config: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LINTER_CONFIG))
    llm_config: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LLM_CONFIG))
    severity_mapping: dict[str, dict[str, str]] = field(
        default_factory=lambda: dict(DEFAULT_SEVERITY_MAPPING)
    )
    smell_types: list[str] = field(default_factory=lambda: list(DEFAULT_SMELL_TYPES))
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: str | None = None) -> RefactorConfig:
        """Load configuration from a JSON file, merging with defaults.

        Args:
            config_path: Path to a ``.refactor.json`` file. If ``None`` or
                         the file does not exist, default configuration is
                         returned.

        Returns:
            A ``RefactorConfig`` instance.
        """
        raw: dict[str, Any] = {}
        if config_path is not None:
            try:
                path = Path(config_path)
                if path.is_file():
                    with open(path, "r") as f:
                        raw = json.load(f)
                    if not isinstance(raw, dict):
                        LOG.warning("Config file %s is not a dict; using defaults", config_path)
                        raw = {}
            except (json.JSONDecodeError, OSError) as exc:
                LOG.warning("Failed to load config from %s: %s", config_path, exc)
                raw = {}

        return cls._from_dict(raw)

    @classmethod
    def defaults(cls) -> RefactorConfig:
        """Return a configuration with all default values."""
        return cls()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> RefactorConfig:
        """Build a ``RefactorConfig`` from an arbitrary dict."""
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = True

        linter_raw = raw.get("linter", {})
        linter_config = dict(DEFAULT_LINTER_CONFIG)
        if isinstance(linter_raw, dict):
            linter_config.update(linter_raw)

        llm_raw = raw.get("llm", {})
        llm_config = dict(DEFAULT_LLM_CONFIG)
        if isinstance(llm_raw, dict):
            llm_config.update(llm_raw)

        severity_raw = raw.get("severity_mapping", {})
        severity_mapping = dict(DEFAULT_SEVERITY_MAPPING)
        if isinstance(severity_raw, dict):
            severity_mapping.update(severity_raw)

        smell_types_raw = raw.get("smell_types")
        if isinstance(smell_types_raw, list):
            smell_types = [str(s) for s in smell_types_raw if isinstance(s, str)]
        else:
            smell_types = list(DEFAULT_SMELL_TYPES)

        return cls(
            enabled=enabled,
            linter_config=linter_config,
            llm_config=llm_config,
            severity_mapping=severity_mapping,
            smell_types=smell_types,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def linter_enabled(self) -> bool:
        """Whether linter-based detection is enabled."""
        return bool(self.linter_config.get("enabled", True))

    @property
    def llm_enabled(self) -> bool:
        """Whether LLM-based detection is enabled."""
        return bool(self.llm_config.get("enabled", True))

    def severity_priority(self, severity: str) -> str:
        """Map a severity level to its corresponding work item priority.

        Args:
            severity: One of ``"critical"``, ``"high"``, ``"medium"``,
                      ``"low"``.

        Returns:
            A priority string (``"critical"``, ``"high"``, ``"medium"``,
            ``"low"``).
        """
        mapping = self.severity_mapping.get(severity, {})
        if isinstance(mapping, dict):
            return str(mapping.get("priority", severity))
        return severity

    def to_dict(self) -> dict[str, Any]:
        """Serialize this configuration to a dict."""
        return {
            "enabled": self.enabled,
            "linter": dict(self.linter_config),
            "llm": dict(self.llm_config),
            "severity_mapping": dict(self.severity_mapping),
            "smell_types": list(self.smell_types),
        }


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def load_refactor_config(config_path: str | None = None) -> RefactorConfig:
    """Load refactor configuration, falling back to defaults.

    This is a convenience wrapper around ``RefactorConfig.load()``.

    Args:
        config_path: Optional path to a ``.refactor.json`` file.

    Returns:
        A ``RefactorConfig`` instance.
    """
    return RefactorConfig.load(config_path)
