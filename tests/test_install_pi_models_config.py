"""Integration tests for the pi config merge wiring in ``scripts/install_pi.sh``.

Exercises the install script together with ``scripts/merge_pi_agent_config.py``
against a sandboxed ``$HOME`` so the real installation is never touched. These
cover the behaviour the merge wiring replaced (a consent-only whole-file copy
that skipped any machine which already had a ``models.json``):

- a fresh install installs the tracked MunitoAI provider whole;
- a pre-existing ``models.json`` that lacks ``MunitoAI`` gains it (today the
  old copy-only path would have skipped the file and installed nothing);
- local-only providers/settings are preserved;
- a second run is idempotent (no duplicated providers/models);
- malformed destination JSON is warned about and left byte-for-byte intact;
- retired model ids and the unservable ``MunitoAI/*`` glob are pruned from a
  pre-existing destination while the explicit named MunitoAI entry survives
  (SA-0MWL5AL4B004X0FN);
- the tracked patterns are installed and local patterns preserved.

Related work item: SA-0MW6S4NAQ001MAM1 (parent SA-0MW0GL4590046BTK).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_pi.sh"
TRACKED_MODELS = json.loads(
    (REPO_ROOT / ".pi-config" / "agent" / "models.json").read_text(encoding="utf-8")
)
TRACKED_SETTINGS = json.loads(
    (REPO_ROOT / ".pi-config" / "agent" / "settings.json").read_text(encoding="utf-8")
)

MUNITOAI_MODEL_IDS = [
    model["id"] for model in TRACKED_MODELS["providers"]["MunitoAI"]["models"]
]


@pytest.fixture()
def sandbox_home():
    home = Path(tempfile.mkdtemp(prefix="test_install_models_"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _run_install(home: Path) -> subprocess.CompletedProcess:
    """Run ``install_pi.sh`` with ``HOME`` sandboxed and stdin closed."""
    src_link = home / "projects" / "SorraAgents"
    src_link.parent.mkdir(parents=True, exist_ok=True)
    if not src_link.exists():
        src_link.symlink_to(REPO_ROOT)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PI_SKIP_HOOK_INSTALL"] = "1"
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _dest(home: Path) -> Path:
    return home / ".pi" / "agent"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fresh_install_installs_tracked_munitoai_provider(sandbox_home: Path) -> None:
    result = _run_install(sandbox_home)
    assert result.returncode == 0, result.stderr

    models = _load(_dest(sandbox_home) / "models.json")
    assert models["providers"]["MunitoAI"] == TRACKED_MODELS["providers"]["MunitoAI"]

    settings = _load(_dest(sandbox_home) / "settings.json")
    # The unservable glob is no longer tracked, so a fresh install omits it.
    assert "MunitoAI/*" not in settings["enabledModels"]
    assert "deepseek/deepseek*" in settings["enabledModels"]


def test_preexisting_drifted_models_gains_munitoai(sandbox_home: Path) -> None:
    dest = _dest(sandbox_home)
    dest.mkdir(parents=True)
    local_only_provider = {
        "api": "openai-completions",
        "baseUrl": "http://localhost:9999/v1",
        "models": [{"id": "plan"}, {"id": "my-local-model"}],
    }
    (dest / "models.json").write_text(
        json.dumps({"providers": {"MyLocal": local_only_provider}}),
        encoding="utf-8",
    )
    (dest / "settings.json").write_text(
        json.dumps(
            {
                "theme": "light",
                "enabledModels": ["Local Proxy/plan", "my/local/*"],
                "someLocalKey": {"nested": True},
            }
        ),
        encoding="utf-8",
    )

    result = _run_install(sandbox_home)
    assert result.returncode == 0, result.stderr
    assert "MunitoAI" in result.stdout

    models = _load(dest / "models.json")
    assert "MunitoAI" in models["providers"]
    # Local-only provider preserved untouched.
    assert models["providers"]["MyLocal"] == local_only_provider
    # A timestamped backup was written before the first modification.
    assert list(dest.glob("models.json.bak.*"))

    settings = _load(dest / "settings.json")
    # Local settings preserved; tracked patterns unioned in.
    assert settings["theme"] == "light"
    assert settings["someLocalKey"] == {"nested": True}
    assert "my/local/*" in settings["enabledModels"]
    assert "MunitoAI/*" not in settings["enabledModels"]


def test_idempotent_rerun_creates_no_duplicates(sandbox_home: Path) -> None:
    dest = _dest(sandbox_home)
    dest.mkdir(parents=True)
    (dest / "models.json").write_text(
        json.dumps({"providers": {"MyLocal": {"models": [{"id": "plan"}]}}}),
        encoding="utf-8",
    )
    (dest / "settings.json").write_text(
        json.dumps({"enabledModels": ["my/local/*"]}),
        encoding="utf-8",
    )

    first = _run_install(sandbox_home)
    assert first.returncode == 0, first.stderr

    second = _run_install(sandbox_home)
    assert second.returncode == 0, second.stderr
    assert "OK: already present" in second.stdout

    models = _load(dest / "models.json")
    ids = [m["id"] for m in models["providers"]["MunitoAI"]["models"]]
    assert ids == MUNITOAI_MODEL_IDS
    assert len(ids) == len(set(ids)), "duplicate models after re-run"

    settings = _load(dest / "settings.json")
    enabled = settings["enabledModels"]
    assert len(enabled) == len(set(enabled)), "duplicate enabledModels after re-run"
    assert enabled.count("MunitoAI/*") == 0
    # Only one backup was written (the first modification).
    assert len(list(dest.glob("models.json.bak.*"))) == 1


def test_malformed_destination_json_is_warned_and_left_intact(
    sandbox_home: Path,
) -> None:
    dest = _dest(sandbox_home)
    dest.mkdir(parents=True)
    (dest / "models.json").write_text("{ this is not valid json", encoding="utf-8")
    original = (dest / "models.json").read_bytes()
    # settings.json is absent so a fresh install of it still succeeds.
    (dest / "settings.json").unlink(missing_ok=True)

    result = _run_install(sandbox_home)

    # The install must not abort on an unparseable destination file.
    assert result.returncode == 0, result.stderr
    assert "WARNING" in (result.stdout + result.stderr)
    assert (dest / "models.json").read_bytes() == original
    assert not list(dest.glob("models.json.bak.*"))


def test_preexisting_munitoai_glob_is_pruned_but_named_entry_retained(
    sandbox_home: Path,
) -> None:
    """AC 10/AC 9: an already-installed glob is pruned; the named id survives."""
    dest = _dest(sandbox_home)
    dest.mkdir(parents=True)
    (dest / "settings.json").write_text(
        json.dumps(
            {
                "enabledModels": [
                    "Local Proxy/plan",
                    "MunitoAI/deepseek-ai/deepseek-v4-flash",
                    "MunitoAI/*",
                    "deepseek/deepseek*",
                ],
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )

    result = _run_install(sandbox_home)
    assert result.returncode == 0, result.stderr

    settings = _load(dest / "settings.json")
    enabled = settings["enabledModels"]
    # The wildcard is gone, even though it was present before the merge.
    assert "MunitoAI/*" not in enabled
    # The explicit named entry is retained unchanged, and local keys kept.
    assert "MunitoAI/deepseek-ai/deepseek-v4-flash" in enabled
    assert settings["theme"] == "dark"


def test_preexisting_retired_model_ids_are_pruned(sandbox_home: Path) -> None:
    """AC 7: ids retired in the tracked config are pruned from a live file."""
    dest = _dest(sandbox_home)
    dest.mkdir(parents=True)
    (dest / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "Local Proxy": {
                        "api": "openai-completions",
                        "baseUrl": "http://192.168.0.199:8000/v1",
                        "apiKey": "not_needed",
                        "models": [
                            {"id": "plan"},
                            {"id": "qwen3"},
                            {"id": "gpt120"},
                            {"id": "local-keep"},
                        ],
                    },
                    "deepseek": {
                        "models": [
                            {"id": "deepseek-v4-flash"},
                            {"id": "local-only-deepseek"},
                        ]
                    },
                    "opencode-go": {
                        "modelOverrides": {
                            "qwen3.6-plus": {},
                            "qwen3.7-max": {},
                            "qwen3.7-plus": {},
                            "local-override": {},
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run_install(sandbox_home)
    assert result.returncode == 0, result.stderr

    models = _load(dest / "models.json")["providers"]
    local_ids = [m["id"] for m in models["Local Proxy"]["models"]]
    assert "qwen3" not in local_ids
    assert "gpt120" not in local_ids
    # Served and local-only ids survive.
    assert "plan" in local_ids
    assert "local-keep" in local_ids

    deepseek_ids = [m["id"] for m in models["deepseek"]["models"]]
    assert "deepseek-v4-flash" not in deepseek_ids
    assert "deepseek-flash" in deepseek_ids
    assert "local-only-deepseek" in deepseek_ids

    overrides = models["opencode-go"]["modelOverrides"]
    assert "qwen3.6-plus" not in overrides
    assert "qwen3.7-max" not in overrides
    assert "local-override" in overrides
