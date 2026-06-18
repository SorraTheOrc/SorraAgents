"""Tests for risk/effort-based model complexity tiers -- SA-0MPMGZ1VQ0021SO1.

Verify that complexity tier resolution, per-child tier evaluation, and
tier-based model resolution all work correctly, including backwards
compatibility with non-tiered configurations.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import (  # noqa: E402
    DEFAULT_MODEL,
    RalphLoop,
    _extract_phase_model_config,
    _resolve_complexity_tier,
    _resolve_phase_model_value,
)  # noqa: E402


class _FakeProc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _noop_runner(cmd):
    return _FakeProc()

class TestResolveComplexityTier:
    """Unit tests for _resolve_complexity_tier."""

    def _default_config(self):
        return {
            "complexity_tier": {
                "low": {"max_effort": "Small", "max_risk": "Low"},
                "high": {"min_effort": "Large", "min_risk": "High"},
            }
        }

    def test_low_tier_xs_effort_low_risk(self):
        """XS effort + Low risk -> low tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Extra Small", "risk": "Low"}) == "low"

    def test_low_tier_small_effort_low_risk(self):
        """S effort + Low risk -> low tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Small", "risk": "Low"}) == "low"

    def test_medium_tier_medium_effort(self):
        """M effort -> medium tier (even with Low risk)."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Medium", "risk": "Low"}) == "medium"

    def test_medium_tier_medium_risk(self):
        """Medium risk -> medium tier (even with Small effort)."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Small", "risk": "Medium"}) == "medium"

    def test_high_tier_large_effort(self):
        """L effort -> high tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Large", "risk": "Low"}) == "high"

    def test_high_tier_extra_large_effort(self):
        """XL effort -> high tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Extra Large", "risk": "Low"}) == "high"

    def test_high_tier_high_risk(self):
        """High risk -> high tier (even with Small effort)."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Small", "risk": "High"}) == "high"

    def test_low_effort_high_risk_becomes_high(self):
        """S effort + High risk -> high tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Small", "risk": "High"}) == "high"

    def test_high_effort_low_risk_becomes_high(self):
        """L effort + Low risk -> high tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Large", "risk": "Low"}) == "high"

    def test_extra_small_effort_medium_risk_is_medium(self):
        """XS effort + Medium risk -> medium tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Extra Small", "risk": "Medium"}) == "medium"

    def test_large_effort_medium_risk_is_high(self):
        """L effort + Medium risk -> high tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Large", "risk": "Medium"}) == "high"

    def test_missing_effort_defaults_to_medium_tier(self):
        """Missing effort -> defaults to Medium for tier resolution."""
        assert _resolve_complexity_tier(self._default_config(), {"risk": "Low"}) == "medium"

    def test_missing_risk_defaults_to_medium_tier(self):
        """Missing risk -> defaults to Medium for tier resolution."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Small"}) == "medium"

    def test_both_missing_defaults_to_low(self):
        """Both missing -> low tier."""
        assert _resolve_complexity_tier(self._default_config(), {}) == "low"

    def test_none_values_defaults_to_low(self):
        """None values -> low tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": None, "risk": None}) == "low"

    def test_unknown_values_defaults_to_medium(self):
        """Unknown values -> defaults to Medium -> medium tier."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Unknown", "risk": "Unknown"}) == "medium"

    def test_custom_low_threshold_allows_medium(self):
        """Configuring low max_effort to Medium allows M+Low to be low tier."""
        config = {"complexity_tier": {"low": {"max_effort": "Medium", "max_risk": "Low"}, "high": {"min_effort": "Large", "min_risk": "High"}}}
        assert _resolve_complexity_tier(config, {"effort": "Medium", "risk": "Low"}) == "low"
        assert _resolve_complexity_tier(config, {"effort": "Large", "risk": "Low"}) == "high"

    def test_custom_low_threshold_restricts_small(self):
        """Configuring low max_effort to XS restricts low tier further."""
        config = {"complexity_tier": {"low": {"max_effort": "Extra Small", "max_risk": "Low"}, "high": {"min_effort": "Large", "min_risk": "High"}}}
        assert _resolve_complexity_tier(config, {"effort": "Extra Small", "risk": "Low"}) == "low"
        assert _resolve_complexity_tier(config, {"effort": "Small", "risk": "Low"}) == "medium"

    def test_custom_high_threshold_allows_medium(self):
        """Configuring high min_effort to Medium makes more items high tier."""
        config = {"complexity_tier": {"low": {"max_effort": "Small", "max_risk": "Low"}, "high": {"min_effort": "Medium", "min_risk": "High"}}}
        assert _resolve_complexity_tier(config, {"effort": "Medium", "risk": "Low"}) == "high"

    def test_no_tier_config_uses_sensible_defaults(self):
        """No complexity_tier in config -> uses built-in defaults (Small/Low for low, Large/High for high)."""
        # XS + Low still qualifies for low tier (XS <= Small AND Low <= Low)
        assert _resolve_complexity_tier({}, {"effort": "Extra Small", "risk": "Low"}) == "low"
        # XL + High still qualifies for high tier (XL >= Large OR High >= High)
        assert _resolve_complexity_tier({}, {"effort": "Extra Large", "risk": "High"}) == "high"
        # XS + Medium risk -> medium (medium risk breaks low AND)
        assert _resolve_complexity_tier({}, {"effort": "Extra Small", "risk": "Medium"}) == "medium"

    def test_empty_tier_config_uses_sensible_defaults(self):
        """Empty complexity_tier dict -> uses built-in defaults."""
        # XS + Low still qualifies for low tier with default thresholds
        assert _resolve_complexity_tier({"complexity_tier": {}}, {"effort": "Extra Small", "risk": "Low"}) == "low"
        # M + Low is medium (M > Small)
        assert _resolve_complexity_tier({"complexity_tier": {}}, {"effort": "Medium", "risk": "Low"}) == "medium"

    def test_partial_tier_config_uses_defaults_for_missing(self):
        """Only low config provided -> high tier uses default thresholds."""
        config = {"complexity_tier": {"low": {"max_effort": "Small", "max_risk": "Low"}}}
        assert _resolve_complexity_tier(config, {"effort": "Extra Large", "risk": "High"}) == "high"

    def test_small_effort_medium_risk_is_medium(self):
        """S effort + Medium risk -> medium (medium risk breaks low tier AND)."""
        assert _resolve_complexity_tier(self._default_config(), {"effort": "Small", "risk": "Medium"}) == "medium"


class TestResolvePhaseModelValueTiered:
    """Tests for _resolve_phase_model_value with tiered model configurations."""

    def test_tiered_nested_resolves_low_remote(self):
        """Nested tiered structure resolves low tier from remote source."""
        val = {"remote": {"low": "remote-low", "medium": "remote-medium", "high": "remote-high"}}
        assert _resolve_phase_model_value(val, "remote", tier="low") == "remote-low"

    def test_tiered_nested_resolves_medium_remote(self):
        """Nested tiered structure resolves medium tier from remote source."""
        val = {"remote": {"low": "remote-low", "medium": "remote-medium", "high": "remote-high"}}
        assert _resolve_phase_model_value(val, "remote", tier="medium") == "remote-medium"

    def test_tiered_nested_resolves_high_remote(self):
        """Nested tiered structure resolves high tier from remote source."""
        val = {"remote": {"low": "remote-low", "medium": "remote-medium", "high": "remote-high"}}
        assert _resolve_phase_model_value(val, "remote", tier="high") == "remote-high"

    def test_tiered_nested_resolves_local(self):
        """Nested tiered structure resolves from local source."""
        val = {"local": {"low": "local-low", "medium": "local-medium", "high": "local-high"}}
        assert _resolve_phase_model_value(val, "local", tier="low") == "local-low"

    def test_tiered_defaults_to_medium_when_tier_is_none(self):
        """When tier=None, defaults to medium tier model."""
        val = {"remote": {"low": "remote-low", "medium": "remote-medium", "high": "remote-high"}}
        assert _resolve_phase_model_value(val, "remote", tier=None) == "remote-medium"

    def test_direct_string_value_bypasses_tier(self):
        """Direct string value is returned regardless of tier."""
        val = "direct-model"
        assert _resolve_phase_model_value(val, "remote", tier="low") == "direct-model"
        assert _resolve_phase_model_value(val, "remote", tier="high") == "direct-model"

    def test_source_mapped_dict_without_tier(self):
        """Source-mapped dict (non-tiered) still works."""
        val = {"remote": "remote-model", "local": "local-model"}
        assert _resolve_phase_model_value(val, "remote", tier="low") == "remote-model"
        assert _resolve_phase_model_value(val, "local", tier="low") == "local-model"

    def test_none_value_returns_none(self):
        """None value returns None."""
        assert _resolve_phase_model_value(None, "remote", tier="low") is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert _resolve_phase_model_value("", "remote", tier="low") is None

    def test_missing_source_returns_none(self):
        """Missing source in dict returns None."""
        val = {"local": {"low": "local-model"}}
        assert _resolve_phase_model_value(val, "remote", tier="low") is None

    def test_non_dict_value_returns_none(self):
        """Non-dict, non-string value returns None."""
        assert _resolve_phase_model_value(42, "remote", tier="low") is None

    def test_both_sources_tiered(self):
        """Config with both remote and local sources resolves correct source+tier."""
        val = {"remote": {"low": "remote-low", "medium": "remote-medium", "high": "remote-high"}, "local": {"low": "local-low", "medium": "local-medium", "high": "local-high"}}
        assert _resolve_phase_model_value(val, "remote", tier="low") == "remote-low"
        assert _resolve_phase_model_value(val, "local", tier="high") == "local-high"

    def test_tiered_source_then_flat_fallback(self):
        """When source has both tiered and flat values, tiered is preferred."""
        val = {"remote": {"low": "remote-low", "medium": "remote-medium", "high": "remote-high", "intake": "remote-flat-intake"}}
        assert _resolve_phase_model_value(val, "remote", tier="low") == "remote-low"


class TestResolveModelForPhaseWithTier:
    """Integration-level tests for RalphLoop._resolve_model_for_phase with tier."""

    def _make_loop(self, model_source=None, model_config=None, model=None,
                   model_source_explicit=None, legacy_model_explicit=None, **overrides):
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

    def test_tiered_config_resolves_tier_model(self):
        """Tiered config should resolve models based on tier."""
        model_config = {
            "implementation": {
                "remote": {"low": "remote-low", "medium": "remote-medium", "high": "remote-high"},
                "local": {"low": "local-low", "medium": "local-medium", "high": "local-high"},
            }
        }
        loop = self._make_loop(model_source="remote", model_config=model_config, model_source_explicit=True)
        assert loop._resolve_model_for_phase("implementation", tier="low") == "remote-low"
        assert loop._resolve_model_for_phase("implementation", tier="medium") == "remote-medium"
        assert loop._resolve_model_for_phase("implementation", tier="high") == "remote-high"

    def test_cli_override_takes_precedence_over_tier(self):
        """CLI override should take precedence over tiered config."""
        model_config = {
            "implementation": {
                "remote": {"low": "remote-low", "medium": "remote-medium", "high": "remote-high"},
            }
        }
        loop = self._make_loop(model_source="remote", model_config=model_config, model_source_explicit=True,
                               model_implementation="cli-override")
        assert loop._resolve_model_for_phase("implementation", tier="low") == "cli-override"
        assert loop._resolve_model_for_phase("implementation", tier="high") == "cli-override"

    def test_tiered_fallback_to_flat_when_tier_missing(self):
        """When tiered model not found, falls back to flat source value."""
        model_config = {
            "implementation": {
                "remote": "remote-flat",
                "local": "local-flat",
            }
        }
        loop = self._make_loop(model_source="remote", model_config=model_config, model_source_explicit=True)
        assert loop._resolve_model_for_phase("implementation", tier="low") == "remote-flat"

    def test_fallback_to_legacy_model(self):
        """When no config, falls back to legacy model."""
        loop = self._make_loop(model_source="remote", model_config={},
                               model="legacy-model", legacy_model_explicit=True)
        assert loop._resolve_model_for_phase("implementation", tier="low") == "legacy-model"

    def test_fallback_to_default_model(self):
        """When no config and no legacy, defaults to DEFAULT_MODEL."""
        loop = self._make_loop(model_source="remote", model_config={},
                               model=DEFAULT_MODEL, legacy_model_explicit=False)
        assert loop._resolve_model_for_phase("implementation", tier="low") == DEFAULT_MODEL

    def test_unknown_phase_raises_error_with_tier(self):
        """Unknown phase should still raise error even with tier parameter."""
        loop = self._make_loop()
        with pytest.raises(Exception):
            loop._resolve_model_for_phase("nonexistent", tier="low")


class TestExtractPhaseModelConfigTiered:
    """Tests for _extract_phase_model_config with tiered configurations."""

    def test_extract_all_tiers_when_tier_none(self):
        """When tier=None, extracts all tiers from nested config."""
        config = {"model.remote.intake": "remote-intake", "model.remote.low.intake": "remote-low-intake",
                  "model.remote.medium.intake": "remote-medium-intake", "model.remote.high.intake": "remote-high-intake"}
        result = _extract_phase_model_config(config)
        assert "intake" in result
        intake_cfg = result["intake"]
        assert isinstance(intake_cfg, dict)
        assert "remote" in intake_cfg

    def test_extract_specific_tier(self):
        """When tier is specified, extracts only that tier."""
        config = {"model.remote.intake": "remote-intake", "model.remote.low.intake": "remote-low-intake",
                  "model.remote.medium.intake": "remote-medium-intake", "model.remote.high.intake": "remote-high-intake"}
        result = _extract_phase_model_config(config, tier="low")
        assert "intake" in result

    def test_backwards_compat_flat_config(self):
        """Flat legacy config still works without tiered structure."""
        config = {"model": {"remote": {"intake": "remote-intake", "audit": "remote-audit"},
                             "local": {"intake": "local-intake", "audit": "local-audit"}}}
        result = _extract_phase_model_config(config)
        assert "intake" in result
        assert "audit" in result
