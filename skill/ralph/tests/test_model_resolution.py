"""Regression tests for Ralph model resolution — SA-0MPLR89HJ0068F9J.

Verify that --model-source remote resolves to the configured remote models
and --model-source local resolves to the configured local models from the
shipped asset config.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_SOURCE,
    MODEL_PHASES,
    RalphLoop,
    _extract_phase_model_config,
    _load_asset_config,
    _load_config,
    _normalize_model_source,
    _preprocess_args,
    _resolve_model,
    _resolve_phase_model_value,
    build_parser,
)


# ---------------------------------------------------------------------------
# Helper: a minimal runner that always succeeds
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _noop_runner(cmd):
    return _FakeProc()


# ---------------------------------------------------------------------------
# Asset config loading
# ---------------------------------------------------------------------------

class TestLoadAssetConfig:
    def test_loads_valid_json(self):
        data = _load_asset_config()
        assert isinstance(data, dict)
        assert "model" in data

    def test_asset_config_has_remote_and_local(self):
        data = _load_asset_config()
        model = data["model"]
        assert "remote" in model
        assert "local" in model

    def test_asset_config_has_all_phases(self):
        data = _load_asset_config()
        for source in ("remote", "local"):
            model_source = data["model"][source]
            for priority in model_source:
                for phase in MODEL_PHASES:
                    assert phase in model_source[priority], (
                        f"Missing {source}.{priority}.{phase} in asset config"
                    )


# ---------------------------------------------------------------------------
# _normalize_model_source
# ---------------------------------------------------------------------------

class TestNormalizeModelSource:
    def test_remote(self):
        assert _normalize_model_source("remote") == "remote"

    def test_local(self):
        assert _normalize_model_source("local") == "local"

    def test_none_defaults(self):
        assert _normalize_model_source(None) == DEFAULT_MODEL_SOURCE

    def test_empty_string_defaults(self):
        assert _normalize_model_source("") == DEFAULT_MODEL_SOURCE

    def test_invalid_value_defaults(self):
        assert _normalize_model_source("bogus") == DEFAULT_MODEL_SOURCE

    def test_case_insensitive(self):
        assert _normalize_model_source("REMOTE") == "remote"
        assert _normalize_model_source("Local") == "local"


# ---------------------------------------------------------------------------
# _resolve_model (legacy single-model)
# ---------------------------------------------------------------------------

class TestResolveModelLegacy:
    def test_cli_takes_precedence(self):
        assert _resolve_model("cli-model", "config-model") == "cli-model"

    def test_config_used_when_no_cli(self):
        assert _resolve_model(None, "config-model") == "config-model"

    def test_default_when_neither(self):
        assert _resolve_model(None, None) == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# _extract_phase_model_config
# ---------------------------------------------------------------------------

class TestExtractPhaseModelConfig:
    def test_source_first_nested_shape(self):
        """model.remote.implementation / model.local.implementation shape."""
        config = {
            "model": {
                "remote": {
                    "implementation": "remote-impl",
                    "audit": "remote-audit",
                },
                "local": {
                    "implementation": "local-impl",
                    "audit": "local-audit",
                },
            }
        }
        result = _extract_phase_model_config(config)
        assert result["implementation"] == {
            "remote": "remote-impl",
            "local": "local-impl",
        }
        assert result["audit"] == {
            "remote": "remote-audit",
            "local": "local-audit",
        }

    def test_dotted_keys(self):
        """model.implementation = string."""
        config = {"model.implementation": "dotted-model"}
        result = _extract_phase_model_config(config)
        assert result["implementation"] == "dotted-model"

    def test_dotted_source_keys(self):
        """model.remote.implementation as flat dotted key."""
        config = {
            "model.remote.implementation": "dotted-remote-impl",
            "model.local.implementation": "dotted-local-impl",
        }
        result = _extract_phase_model_config(config)
        assert result["implementation"] == {
            "remote": "dotted-remote-impl",
            "local": "dotted-local-impl",
        }

    def test_phase_first_nested_shape(self):
        """model.implementation = {remote: ..., local: ...} shape."""
        config = {
            "model": {
                "implementation": {
                    "remote": "phase-first-remote",
                    "local": "phase-first-local",
                },
            }
        }
        result = _extract_phase_model_config(config)
        assert result["implementation"] == {
            "remote": "phase-first-remote",
            "local": "phase-first-local",
        }

    def test_legacy_string_model(self):
        """model = string should NOT appear in phase config."""
        config = {"model": "legacy-single-model"}
        result = _extract_phase_model_config(config)
        # Legacy single model should not be extracted as a phase model
        assert "implementation" not in result

    def test_partial_source_map(self):
        """Only remote defined, local absent."""
        config = {
            "model": {
                "remote": {"implementation": "only-remote"},
            }
        }
        result = _extract_phase_model_config(config)
        assert result["implementation"] == {"remote": "only-remote"}

    def test_empty_config(self):
        result = _extract_phase_model_config({})
        assert result == {}


# ---------------------------------------------------------------------------
# _resolve_phase_model_value
# ---------------------------------------------------------------------------

class TestResolvePhaseModelValue:
    def test_string_value(self):
        assert _resolve_phase_model_value("direct-model", "remote") == "direct-model"

    def test_source_mapped_dict(self):
        val = {"remote": "remote-m", "local": "local-m"}
        assert _resolve_phase_model_value(val, "remote") == "remote-m"
        assert _resolve_phase_model_value(val, "local") == "local-m"

    def test_source_mapped_dict_missing_source(self):
        val = {"remote": "remote-m"}
        assert _resolve_phase_model_value(val, "local") is None

    def test_none_value(self):
        assert _resolve_phase_model_value(None, "remote") is None

    def test_empty_string(self):
        assert _resolve_phase_model_value("", "remote") is None


# ---------------------------------------------------------------------------
# RalphLoop._resolve_model_for_phase
# ---------------------------------------------------------------------------

class TestResolveModelForPhase:
    """Integration-level tests using RalphLoop with controlled config."""

    def _make_loop(self, model_source=None, model_config=None, model=None,
                   model_source_explicit=None, legacy_model_explicit=None,
                   **overrides):
        return RalphLoop(
            runner=_noop_runner,
            pi_bin="pi",
            model=model or DEFAULT_MODEL,
            model_source=model_source,
            model_config=model_config or {},
            model_source_explicit=model_source_explicit,
            legacy_model_explicit=legacy_model_explicit,
            **overrides,
        )

    def test_remote_source_resolves_implementation_from_config(self):
        """--model-source remote should resolve implementation to the remote model."""
        model_config = {
            "implementation": {
                "remote": "opencode-go/qwen3.6-plus",
                "local": "Qwen 32B",
            }
        }
        loop = self._make_loop(
            model_source="remote",
            model_config=model_config,
            model_source_explicit=True,
        )
        result = loop._resolve_model_for_phase("implementation")
        assert result == "opencode-go/qwen3.6-plus", (
            f"Expected remote implementation model, got: {result}"
        )

    def test_remote_source_resolves_audit_from_config(self):
        """--model-source remote should resolve audit to the remote model."""
        model_config = {
            "audit": {
                "remote": "opencode-go/kimi-k2.5",
                "local": "Llama-3.1 70B (Q4_K_M)",
            }
        }
        loop = self._make_loop(
            model_source="remote",
            model_config=model_config,
            model_source_explicit=True,
        )
        result = loop._resolve_model_for_phase("audit")
        assert result == "opencode-go/kimi-k2.5", (
            f"Expected remote audit model, got: {result}"
        )

    def test_local_source_resolves_implementation_from_config(self):
        """--model-source local should resolve to the local model."""
        model_config = {
            "implementation": {
                "remote": "opencode-go/qwen3.6-plus",
                "local": "Qwen 32B",
            }
        }
        loop = self._make_loop(
            model_source="local",
            model_config=model_config,
            model_source_explicit=True,
        )
        result = loop._resolve_model_for_phase("implementation")
        assert result == "Qwen 32B", (
            f"Expected local implementation model, got: {result}"
        )

    def test_cli_phase_override_takes_precedence(self):
        """--model-implementation should override config resolution."""
        model_config = {
            "implementation": {
                "remote": "remote-model",
                "local": "local-model",
            }
        }
        loop = self._make_loop(
            model_source="remote",
            model_config=model_config,
            model_source_explicit=True,
            model_implementation="cli-override",
        )
        result = loop._resolve_model_for_phase("implementation")
        assert result == "cli-override"

    def test_legacy_model_used_when_no_per_phase_config(self):
        """When no per-phase config exists and --model is set, use legacy model."""
        loop = self._make_loop(
            model_source="remote",
            model_config={},
            model="legacy-model",
            legacy_model_explicit=True,
        )
        result = loop._resolve_model_for_phase("implementation")
        assert result == "legacy-model"

    def test_default_when_no_config_and_no_legacy(self):
        """When no config, no legacy model, defaults are used."""
        loop = self._make_loop(
            model_source="remote",
            model_config={},
            model=DEFAULT_MODEL,
            legacy_model_explicit=False,
        )
        result = loop._resolve_model_for_phase("implementation")
        assert result == DEFAULT_MODEL

    def test_all_phases_resolve_with_remote_source(self):
        """Verify all phases resolve correctly with remote source."""
        model_config = {
            "intake": {"remote": "remote-intake", "local": "local-intake"},
            "planning": {"remote": "remote-planning", "local": "local-planning"},
            "implementation": {"remote": "remote-impl", "local": "local-impl"},
            "audit": {"remote": "remote-audit", "local": "local-audit"},
        }
        loop = self._make_loop(
            model_source="remote",
            model_config=model_config,
            model_source_explicit=True,
        )
        expected = {
            "intake": "remote-intake",
            "planning": "remote-planning",
            "implementation": "remote-impl",
            "audit": "remote-audit",
        }
        for phase, expected_model in expected.items():
            result = loop._resolve_model_for_phase(phase)
            assert result == expected_model, (
                f"Phase {phase}: expected {expected_model}, got {result}"
            )

    def test_all_phases_resolve_with_local_source(self):
        """Verify all phases resolve correctly with local source."""
        model_config = {
            "intake": {"remote": "remote-intake", "local": "local-intake"},
            "planning": {"remote": "remote-planning", "local": "local-planning"},
            "implementation": {"remote": "remote-impl", "local": "local-impl"},
            "audit": {"remote": "remote-audit", "local": "local-audit"},
        }
        loop = self._make_loop(
            model_source="local",
            model_config=model_config,
            model_source_explicit=True,
        )
        expected = {
            "intake": "local-intake",
            "planning": "local-planning",
            "implementation": "local-impl",
            "audit": "local-audit",
        }
        for phase, expected_model in expected.items():
            result = loop._resolve_model_for_phase(phase)
            assert result == expected_model, (
                f"Phase {phase}: expected {expected_model}, got {result}"
            )

    def test_unknown_phase_raises_error(self):
        loop = self._make_loop()
        with pytest.raises(Exception):
            loop._resolve_model_for_phase("nonexistent")


# ---------------------------------------------------------------------------
# End-to-end: asset config + _load_config integration
# ---------------------------------------------------------------------------

class TestLoadConfigIntegration:
    def test_asset_config_models_extracted_correctly(self):
        """The shipped asset config should produce correct phase models."""
        config = _load_asset_config()
        phase_config = _extract_phase_model_config(config)

        assert "implementation" in phase_config
        impl_cfg = phase_config["implementation"]
        assert isinstance(impl_cfg, dict), (
            f"Expected dict for implementation config, got {type(impl_cfg)}"
        )
        assert "remote" in impl_cfg, "Missing 'remote' key in implementation config"
        assert "local" in impl_cfg, "Missing 'local' key in implementation config"

    def test_asset_config_remote_implementation_value(self):
        config = _load_asset_config()
        phase_config = _extract_phase_model_config(config)
        impl_model = _resolve_phase_model_value(phase_config["implementation"], "remote")
        assert impl_model is not None
        assert "opencode" in impl_model or "qwen" in impl_model.lower(), (
            f"Expected remote model to contain opencode/qwen, got: {impl_model}"
        )

    def test_asset_config_local_implementation_value(self):
        config = _load_asset_config()
        phase_config = _extract_phase_model_config(config)
        impl_model = _resolve_phase_model_value(phase_config["implementation"], "local")
        assert impl_model is not None


# ---------------------------------------------------------------------------
# CLI preprocessing: model source shorthand
# ---------------------------------------------------------------------------

class TestPreprocessArgs:
    def test_remote_shorthand(self):
        result = _preprocess_args(["SA-123", "remote"])
        assert result == ["SA-123", "--model-source", "remote"]

    def test_local_shorthand(self):
        result = _preprocess_args(["SA-123", "local"])
        assert result == ["SA-123", "--model-source", "local"]

    def test_shorthand_with_flags(self):
        result = _preprocess_args(["SA-123", "remote", "--verbose"])
        assert result == ["SA-123", "--model-source", "remote", "--verbose"]

    def test_no_shorthand(self):
        result = _preprocess_args(["SA-123"])
        assert result == ["SA-123"]

    def test_explicit_flag_not_duplicated(self):
        result = _preprocess_args(["SA-123", "--model-source", "remote"])
        assert result == ["SA-123", "--model-source", "remote"]


class TestBuildParser:
    def test_parse_remote(self):
        parser = build_parser()
        args = parser.parse_args(_preprocess_args(["SA-123", "remote"]))
        assert args.model_source == "remote"

    def test_parse_local(self):
        parser = build_parser()
        args = parser.parse_args(_preprocess_args(["SA-123", "local"]))
        assert args.model_source == "local"
