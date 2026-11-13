"""Unit tests for the pi agent config merge helper (SA-0MW6S2MAL004U5KX).

These tests exercise the public API of ``scripts/merge_pi_agent_config.py``
directly. The helper is a dependency-free (stdlib-only) module used by
``scripts/install_pi.sh`` to merge the tracked ``.pi-config/agent`` providers
into an existing ``~/.pi/agent/models.json`` and to union the tracked
``enabledModels`` into ``~/.pi/agent/settings.json`` without clobbering local
customisations.

Public API under test (this module defines the contract that the merge-helper
work item SA-0MW6S07QM005C6U2 implements):

- ``merge_providers(tracked_providers, dest_providers) -> (merged, summary)``
- ``merge_enabled_models(tracked_patterns, dest_patterns) -> (merged, added)``
- ``merge_models_document(tracked_doc, dest_doc) -> (merged, report)``
- ``merge_settings_document(tracked_doc, dest_doc) -> (merged, report)``
- ``write_json_atomic(path, data) -> None``
- ``merge_models_file(tracked_path, dest_path, *, backup_stamp=None) -> dict``
- ``merge_settings_file(tracked_path, dest_path, *, backup_stamp=None) -> dict``
- ``merge_agent_config(tracked_dir, dest_dir, *, backup_stamp=None) -> dict``
- ``main(argv=None) -> int``

Report dictionaries expose ``status`` (one of ``ok``/``unchanged``/
``copied``/``invalid_json``), ``changed`` (bool), ``backup`` (path or
``None``) and provider/pattern detail. Invalid destination JSON is fail-open:
the file is left byte-for-byte intact, no backup is written, and the CLI exits
non-zero so the install script can warn without aborting.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "merge_pi_agent_config.py"


def _load_helper():
    """Load ``scripts/merge_pi_agent_config.py`` by file path.

    The repository has two distinct ``scripts`` packages (repo-root ``scripts/``
    and ``skill/scripts/``) that share a top-level module name; a plain
    ``from scripts.merge_pi_agent_config import ...`` resolves to whichever one
    happens to be cached in ``sys.modules`` first, making the import
    order-dependent. Loading by file location (as the repo-root conftest does
    for its guards) keeps this test deterministic.
    """
    spec = importlib.util.spec_from_file_location(
        "_merge_pi_agent_config", HELPER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_merge = _load_helper()
merge_agent_config = _merge.merge_agent_config
merge_enabled_models = _merge.merge_enabled_models
merge_models_document = _merge.merge_models_document
merge_models_file = _merge.merge_models_file
merge_providers = _merge.merge_providers
merge_settings_document = _merge.merge_settings_document
merge_settings_file = _merge.merge_settings_file
write_json_atomic = _merge.write_json_atomic

# ---------------------------------------------------------------------------
# Fixtures / sample documents
# ---------------------------------------------------------------------------

TRACKED_MODELS = {
    "providers": {
        "MunitoAI": {
            "api": "openai-completions",
            "baseUrl": "https://api.munito.ai/inference/v1",
            "apiKey": "$MUNITOAI_API_KEY",
            "models": [
                {
                    "id": "deepseek-ai/deepseek-v4-flash",
                    "name": "MunitoAI DeepSeek V4 Flash (paid)",
                    "contextWindow": 400000,
                    "maxTokens": 16384,
                    "input": ["text"],
                },
                {
                    "id": "deepseek-ai/deepseek-v4.1-flash-1017",
                    "name": "MunitoAI DeepSeek V4.1 Flash 1017 (paid)",
                    "contextWindow": 400000,
                    "maxTokens": 16384,
                    "input": ["text"],
                },
            ],
        },
        "opencode-go": {
            "modelOverrides": {
                "qwen3.6-plus": {"thinkingLevelMap": {"off": None, "high": "high"}},
            },
        },
    }
}

DEST_MODELS = {
    "providers": {
        "LocalProxy": {
            "api": "openai-completions",
            "models": [{"id": "plan"}, {"id": "code"}],
        },
        "opencode-go": {
            "modelOverrides": {
                # Tracked entry wins over this stale local value.
                "qwen3.6-plus": {"thinkingLevelMap": {"off": None}},
                # Local-only override must survive untouched.
                "local-only-model": {"thinkingLevelMap": {"high": "max"}},
            },
        },
    }
}

TRACKED_SETTINGS = {
    "defaultProvider": "Local Proxy",
    "enabledModels": [
        "Local Proxy/plan",
        "Local Proxy/code",
        "MunitoAI/*",
        "deepseek/deepseek*",
    ],
    "theme": "dark",
}

DEST_SETTINGS = {
    "defaultProvider": "Local Proxy",
    "enabledModels": ["Local Proxy/plan", "my/local/*"],
    "theme": "light",
    "someLocalOnlyKey": {"nested": True},
}


# ---------------------------------------------------------------------------
# merge_providers: add / update / preserve
# ---------------------------------------------------------------------------


def test_merge_providers_adds_new_provider() -> None:
    merged, summary = merge_providers(
        TRACKED_MODELS["providers"], DEST_MODELS["providers"]
    )

    assert merged["MunitoAI"] == TRACKED_MODELS["providers"]["MunitoAI"]
    assert summary["MunitoAI"] == "added"
    # Local-only provider is preserved untouched.
    assert merged["LocalProxy"] == DEST_MODELS["providers"]["LocalProxy"]


def test_merge_providers_updates_existing_and_preserves_rest() -> None:
    tracked = {
        "providers": {
            "MunitoAI": TRACKED_MODELS["providers"]["MunitoAI"],
        }
    }
    dest = {
        "providers": {
            "MunitoAI": {
                "api": "openai-completions",
                "baseUrl": "https://stale.example/v1",
                "apiKey": "stale-local-key",
                "models": [
                    {
                        "id": "deepseek-ai/deepseek-v4-flash",
                        "name": "stale name",
                    },
                    {"id": "local-only-model", "name": "keep me"},
                ],
                "localExtraField": "preserved",
            }
        }
    }

    merged, summary = merge_providers(
        tracked["providers"], dest["providers"]
    )
    munito = merged["MunitoAI"]

    # Tracked scalar fields win.
    assert munito["baseUrl"] == "https://api.munito.ai/inference/v1"
    assert munito["apiKey"] == "$MUNITOAI_API_KEY"
    # Untouched provider-level local field survives.
    assert munito["localExtraField"] == "preserved"
    # Tracked model replaces the stale definition with the same id.
    ids = [m["id"] for m in munito["models"]]
    assert ids == [
        "deepseek-ai/deepseek-v4-flash",
        "local-only-model",
        "deepseek-ai/deepseek-v4.1-flash-1017",
    ]
    assert munito["models"][0]["name"] == "MunitoAI DeepSeek V4 Flash (paid)"
    # Local-only model preserved.
    assert munito["models"][1]["name"] == "keep me"
    assert summary["MunitoAI"] == "updated"


def test_merge_providers_is_idempotent() -> None:
    once, _ = merge_providers(
        TRACKED_MODELS["providers"], DEST_MODELS["providers"]
    )
    twice, summary = merge_providers(TRACKED_MODELS["providers"], once)

    assert twice == once
    assert all(status == "present" for status in summary.values())


def test_merge_providers_does_not_mutate_inputs() -> None:
    tracked_providers = json.loads(json.dumps(TRACKED_MODELS["providers"]))
    dest_providers = json.loads(json.dumps(DEST_MODELS["providers"]))
    merge_providers(tracked_providers, dest_providers)
    assert tracked_providers == TRACKED_MODELS["providers"]
    assert dest_providers == DEST_MODELS["providers"]


# ---------------------------------------------------------------------------
# modelOverrides union
# ---------------------------------------------------------------------------


def test_merge_model_overrides_union() -> None:
    merged, _ = merge_providers(
        TRACKED_MODELS["providers"], DEST_MODELS["providers"]
    )

    overrides = merged["opencode-go"]["modelOverrides"]
    # Tracked override key refreshed to the tracked value.
    assert overrides["qwen3.6-plus"] == {"thinkingLevelMap": {"off": None, "high": "high"}}
    # Local-only override preserved.
    assert overrides["local-only-model"] == {"thinkingLevelMap": {"high": "max"}}


# ---------------------------------------------------------------------------
# enabledModels union
# ---------------------------------------------------------------------------


def test_merge_enabled_models_union_dedup() -> None:
    merged, added = merge_enabled_models(
        TRACKED_SETTINGS["enabledModels"], DEST_SETTINGS["enabledModels"]
    )

    assert merged == [
        "Local Proxy/plan",
        "my/local/*",
        "Local Proxy/code",
        "MunitoAI/*",
        "deepseek/deepseek*",
    ]
    assert added == ["Local Proxy/code", "MunitoAI/*", "deepseek/deepseek*"]
    # No duplicates.
    assert len(merged) == len(set(merged))


def test_merge_enabled_models_when_dest_missing() -> None:
    merged, added = merge_enabled_models(TRACKED_SETTINGS["enabledModels"], [])
    assert merged == TRACKED_SETTINGS["enabledModels"]
    assert added == TRACKED_SETTINGS["enabledModels"]


# ---------------------------------------------------------------------------
# Document-level merges preserve unrelated keys
# ---------------------------------------------------------------------------


def test_merge_models_document_preserves_top_level_keys() -> None:
    tracked = dict(TRACKED_MODELS)
    tracked["someTopLevel"] = {"tracked": True}
    dest = dict(DEST_MODELS)
    dest["localTopLevel"] = 42

    merged, report = merge_models_document(tracked, dest)

    assert merged["localTopLevel"] == 42
    assert merged["someTopLevel"] == {"tracked": True}
    assert "MunitoAI" in merged["providers"]
    assert report["providers"]["MunitoAI"] == "added"


def test_merge_settings_document_preserves_local_keys() -> None:
    merged, report = merge_settings_document(TRACKED_SETTINGS, DEST_SETTINGS)

    assert merged["someLocalOnlyKey"] == {"nested": True}
    assert merged["enabledModels"] == [
        "Local Proxy/plan",
        "my/local/*",
        "Local Proxy/code",
        "MunitoAI/*",
        "deepseek/deepseek*",
    ]
    assert report["added"] == ["Local Proxy/code", "MunitoAI/*", "deepseek/deepseek*"]


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_write_json_atomic_writes_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "models.json"
    write_json_atomic(target, {"providers": {"x": {"models": [{"id": "m"}]}}})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "providers": {"x": {"models": [{"id": "m"}]}}
    }
    # No leftover temp file: the temp file was renamed into place.
    assert not any(
        p.name.endswith(".tmp") for p in tmp_path.iterdir()
    ), [p.name for p in tmp_path.iterdir()]


def test_write_json_atomic_preserves_newline_terminated_output(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    write_json_atomic(target, {"a": 1})
    assert target.read_text(encoding="utf-8").endswith("\n")


# ---------------------------------------------------------------------------
# File-level merge: backup + write + idempotency
# ---------------------------------------------------------------------------


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_merge_models_file_writes_backup_and_merges(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked_models.json"
    dest = tmp_path / "models.json"
    _write(tracked, TRACKED_MODELS)
    _write(dest, DEST_MODELS)

    report = merge_models_file(
        tracked, dest, backup_stamp="20260101T000000Z"
    )

    assert report["status"] == "ok"
    assert report["changed"] is True
    assert report["providers"]["MunitoAI"] == "added"

    backup = tmp_path / "models.json.bak.20260101T000000Z"
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == DEST_MODELS

    merged = json.loads(dest.read_text(encoding="utf-8"))
    assert "MunitoAI" in merged["providers"]
    assert merged["providers"]["LocalProxy"] == DEST_MODELS["providers"]["LocalProxy"]


def test_merge_models_file_writes_backup_only_once_on_rerun(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked_models.json"
    dest = tmp_path / "models.json"
    _write(tracked, TRACKED_MODELS)
    _write(dest, DEST_MODELS)

    first = merge_models_file(tracked, dest, backup_stamp="20260101T000000Z")
    assert first["changed"] is True
    first_bytes = dest.read_bytes()

    second = merge_models_file(tracked, dest, backup_stamp="20260102T000000Z")

    assert second["status"] == "unchanged"
    assert second["changed"] is False
    assert second["backup"] is None
    # No second backup written on the idempotent re-run.
    backups = list(tmp_path.glob("models.json.bak.*"))
    assert [b.name for b in backups] == ["models.json.bak.20260101T000000Z"]
    # File is byte-for-byte unchanged.
    assert dest.read_bytes() == first_bytes


def test_merge_models_file_invalid_dest_json_fail_open(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked_models.json"
    dest = tmp_path / "models.json"
    _write(tracked, TRACKED_MODELS)
    dest.write_text("{ this is not valid json", encoding="utf-8")
    original = dest.read_bytes()

    report = merge_models_file(
        tracked, dest, backup_stamp="20260101T000000Z"
    )

    assert report["status"] == "invalid_json"
    assert report["changed"] is False
    # File left byte-for-byte intact and no backup created.
    assert dest.read_bytes() == original
    assert list(tmp_path.glob("models.json.bak.*")) == []


def test_merge_models_file_missing_dest_copies_tracked(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked_models.json"
    dest = tmp_path / "models.json"
    _write(tracked, TRACKED_MODELS)

    report = merge_models_file(tracked, dest)

    # A missing destination is installed whole from the tracked config
    # (existing copy-if-missing behaviour, delegated to the helper).
    assert report["status"] == "copied"
    assert report["changed"] is True
    assert json.loads(dest.read_text(encoding="utf-8")) == TRACKED_MODELS


def test_merge_settings_file_writes_backup_and_unions(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked_settings.json"
    dest = tmp_path / "settings.json"
    _write(tracked, TRACKED_SETTINGS)
    _write(dest, DEST_SETTINGS)

    report = merge_settings_file(
        tracked, dest, backup_stamp="20260101T000000Z"
    )

    assert report["status"] == "ok"
    assert report["changed"] is True
    assert report["added"] == ["Local Proxy/code", "MunitoAI/*", "deepseek/deepseek*"]

    backup = tmp_path / "settings.json.bak.20260101T000000Z"
    assert json.loads(backup.read_text(encoding="utf-8")) == DEST_SETTINGS

    merged = json.loads(dest.read_text(encoding="utf-8"))
    assert merged["someLocalOnlyKey"] == {"nested": True}
    assert "MunitoAI/*" in merged["enabledModels"]


# ---------------------------------------------------------------------------
# Top-level end-to-end merge
# ---------------------------------------------------------------------------


def test_merge_agent_config_end_to_end(tmp_path: Path) -> None:
    tracked_dir = tmp_path / "tracked"
    dest_dir = tmp_path / "dest"
    tracked_dir.mkdir()
    dest_dir.mkdir()
    _write(tracked_dir / "models.json", TRACKED_MODELS)
    _write(tracked_dir / "settings.json", TRACKED_SETTINGS)
    _write(dest_dir / "models.json", DEST_MODELS)
    _write(dest_dir / "settings.json", DEST_SETTINGS)

    report = merge_agent_config(
        tracked_dir, dest_dir, backup_stamp="20260101T000000Z"
    )

    assert report["models"]["changed"] is True
    assert report["settings"]["changed"] is True

    merged_models = json.loads((dest_dir / "models.json").read_text(encoding="utf-8"))
    merged_settings = json.loads((dest_dir / "settings.json").read_text(encoding="utf-8"))
    assert "MunitoAI" in merged_models["providers"]
    assert "MunitoAI/*" in merged_settings["enabledModels"]
    # Enabled patterns cover both new MunitoAI DeepSeek models.
    assert "MunitoAI/*" in merged_settings["enabledModels"]

    # Re-run is idempotent and non-destructive.
    before_models = (dest_dir / "models.json").read_bytes()
    before_settings = (dest_dir / "settings.json").read_bytes()
    rerun = merge_agent_config(
        tracked_dir, dest_dir, backup_stamp="20260102T000000Z"
    )
    assert rerun["models"]["changed"] is False
    assert rerun["settings"]["changed"] is False
    assert (dest_dir / "models.json").read_bytes() == before_models
    assert (dest_dir / "settings.json").read_bytes() == before_settings


# ---------------------------------------------------------------------------
# CLI: non-interactive-safe, positional args, exit codes
# ---------------------------------------------------------------------------


def _run_cli(tracked_dir: Path, dest_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HELPER), str(tracked_dir), str(dest_dir)],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_merges_non_interactively_and_reports(tmp_path: Path) -> None:
    tracked_dir = tmp_path / "tracked"
    dest_dir = tmp_path / "dest"
    tracked_dir.mkdir()
    dest_dir.mkdir()
    _write(tracked_dir / "models.json", TRACKED_MODELS)
    _write(tracked_dir / "settings.json", TRACKED_SETTINGS)
    _write(dest_dir / "models.json", DEST_MODELS)
    _write(dest_dir / "settings.json", DEST_SETTINGS)

    result = _run_cli(tracked_dir, dest_dir)

    assert result.returncode == 0, result.stderr
    assert "Added" in result.stdout
    assert "MunitoAI" in result.stdout
    merged_models = json.loads((dest_dir / "models.json").read_text(encoding="utf-8"))
    assert "MunitoAI" in merged_models["providers"]


def test_cli_second_run_reports_already_present(tmp_path: Path) -> None:
    tracked_dir = tmp_path / "tracked"
    dest_dir = tmp_path / "dest"
    tracked_dir.mkdir()
    dest_dir.mkdir()
    _write(tracked_dir / "models.json", TRACKED_MODELS)
    _write(tracked_dir / "settings.json", TRACKED_SETTINGS)
    _write(dest_dir / "models.json", DEST_MODELS)
    _write(dest_dir / "settings.json", DEST_SETTINGS)

    assert _run_cli(tracked_dir, dest_dir).returncode == 0
    second = _run_cli(tracked_dir, dest_dir)

    assert second.returncode == 0, second.stderr
    assert "OK: already present" in second.stdout


def test_cli_invalid_dest_json_exits_non_zero(tmp_path: Path) -> None:
    tracked_dir = tmp_path / "tracked"
    dest_dir = tmp_path / "dest"
    tracked_dir.mkdir()
    dest_dir.mkdir()
    _write(tracked_dir / "models.json", TRACKED_MODELS)
    _write(tracked_dir / "settings.json", TRACKED_SETTINGS)
    (dest_dir / "models.json").write_text("{ nope", encoding="utf-8")
    original = (dest_dir / "models.json").read_bytes()

    result = _run_cli(tracked_dir, dest_dir)

    assert result.returncode != 0
    assert (dest_dir / "models.json").read_bytes() == original


def test_cli_missing_dest_files_copies_tracked(tmp_path: Path) -> None:
    tracked_dir = tmp_path / "tracked"
    dest_dir = tmp_path / "dest"
    tracked_dir.mkdir()
    dest_dir.mkdir()
    _write(tracked_dir / "models.json", TRACKED_MODELS)
    _write(tracked_dir / "settings.json", TRACKED_SETTINGS)

    result = _run_cli(tracked_dir, dest_dir)

    assert result.returncode == 0, result.stderr
    # Fresh destination: tracked config installed whole.
    installed = json.loads((dest_dir / "models.json").read_text(encoding="utf-8"))
    assert installed["providers"]["MunitoAI"] == TRACKED_MODELS["providers"]["MunitoAI"]
    installed_settings = json.loads(
        (dest_dir / "settings.json").read_text(encoding="utf-8")
    )
    assert installed_settings["enabledModels"] == TRACKED_SETTINGS["enabledModels"]


def test_helper_is_stdlib_only() -> None:
    source = HELPER.read_text(encoding="utf-8")
    forbidden = ["import requests", "import yaml", "import toml"]
    for needle in forbidden:
        assert needle not in source
