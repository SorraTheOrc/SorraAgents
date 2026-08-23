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
OPENCODE_GO_QWEN = {
    "qwen3.6-plus",
    "qwen3.7-max",
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