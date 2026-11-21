"""Tests for Qwen preserve-thinking configuration in the global pi model config.

The global pi agent configuration (`.pi-config/agent/models.json`) is the
canonical, git-tracked source for model overrides. These tests assert that
Qwen model variants carry the thinking configuration required for reasoning
content to be requested and preserved in responses (SA-0MT5RXZZ1006EW4R).
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_JSON = REPO_ROOT / ".pi-config" / "agent" / "models.json"

# Qwen variants on the opencode provider use the anthropic-messages API;
# thinkingLevelMap drives adaptive/budget thinking on that code path.
OPENCODE_QWEN = {
    "qwen3.5-plus",
    "qwen3.6-plus",
}

# Qwen variants on the opencode-go provider use openai-completions;
# thinkingFormat "qwen" sends enable_thinking (preserve-thinking compatible).
# Only the ids the provider actually serves are tracked here: qwen3.6-plus and
# qwen3.7-max were pruned (SA-0MWL5AL4B004X0FN) because they are absent from
# the cached opencode-go catalogue.
OPENCODE_GO_QWEN = {
    "qwen3.7-plus",
    "qwen3.8-max",
}


def load_models_config():
    return json.loads(MODELS_JSON.read_text(encoding="utf-8"))


def test_models_json_is_valid_json():
    data = load_models_config()
    assert "providers" in data
    assert isinstance(data["providers"], dict)


def test_opencode_qwen_overrides_have_thinking_level_map():
    data = load_models_config()
    overrides = data["providers"]["opencode"]["modelOverrides"]
    for model_id in OPENCODE_QWEN:
        assert model_id in overrides, f"missing modelOverride for {model_id}"
        level_map = overrides[model_id]["thinkingLevelMap"]
        assert level_map["high"] == "high"
        assert level_map["max"] == "max"
        assert level_map["off"] is None


def test_opencode_go_qwen_overrides_have_thinking_format():
    data = load_models_config()
    overrides = data["providers"]["opencode-go"]["modelOverrides"]
    for model_id in OPENCODE_GO_QWEN:
        assert model_id in overrides, f"missing modelOverride for {model_id}"
        compat = overrides[model_id]["compat"]
        assert compat["thinkingFormat"] in ("qwen", "qwen-chat-template"), (
            f"{model_id} thinkingFormat must enable thinking"
        )
        level_map = overrides[model_id]["thinkingLevelMap"]
        assert level_map["high"] == "high"
        assert level_map["max"] == "max"
        assert level_map["off"] is None


def test_local_proxy_lists_no_phantom_model_ids():
    """AC 1: the proxy does not serve qwen3/gpt120 (400 model not found)."""
    data = load_models_config()
    ids = {model["id"] for model in data["providers"]["Local Proxy"]["models"]}
    assert "qwen3" not in ids
    assert "gpt120" not in ids
    # Served ids are retained.
    assert {"plan", "code", "author"} <= ids


def test_deepseek_uses_catalogued_ids_and_metadata():
    """AC 2/AC 3: deepseek ids exist in the catalogue and carry full metadata."""
    data = load_models_config()
    models = {m["id"]: m for m in data["providers"]["deepseek"]["models"]}

    # The undocumented alias is gone; the canonical id is tracked.
    assert "deepseek-v4-flash" not in models
    assert "deepseek-flash" in models
    assert "deepseek-v4-pro" in models

    for model_id, expected_input in (
        ("deepseek-flash", ["text", "image"]),
        ("deepseek-v4-pro", ["text"]),
    ):
        model = models[model_id]
        assert model["contextWindow"] == 1048576
        assert model["maxTokens"] == 393216
        assert model["input"] == expected_input
        # Catalogue effort levels are low/high/max; no null level may be
        # mapped to an effort in the deprecated compat.reasoningEffortMap.
        level_map = model["thinkingLevelMap"]
        assert level_map["low"] == "low"
        assert level_map["high"] == "high"
        assert level_map["max"] == "max"
        assert "reasoningEffortMap" not in model.get("compat", {})


def test_opencode_go_retired_overrides_are_absent():
    """AC 4: ids absent from the cached catalogue are pruned."""
    data = load_models_config()
    overrides = data["providers"]["opencode-go"]["modelOverrides"]
    assert "qwen3.6-plus" not in overrides
    assert "qwen3.7-max" not in overrides