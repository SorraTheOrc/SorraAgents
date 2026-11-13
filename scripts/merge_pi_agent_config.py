#!/usr/bin/env python3
# <!-- REFACTOR-SA-0MWE7LK3Y005Q3R6
# smell: formatting
# severity: low
# description: Shebang is present but file is not executable
# -->
"""Idempotent, non-destructive merge helper for the tracked pi agent config.

Used by ``scripts/install_pi.sh`` to apply the repository's tracked
``.pi-config/agent`` providers to an operator's existing
``~/.pi/agent/models.json`` and to union the tracked ``enabledModels`` into
``~/.pi/agent/settings.json``. It replaces the previous consent-only
whole-file copy so that every machine and every project receives the tracked
model set (including the paid ``MunitoAI`` DeepSeek provider) without
hand-editing JSON.

Usage::

    python3 scripts/merge_pi_agent_config.py TRACKED_DIR DEST_DIR

For example ``python3 scripts/merge_pi_agent_config.py .pi-config/agent ~/.pi/agent``.

Behaviour contract
------------------
* **Non-destructive merge.** Tracked providers are merged into the
  destination: a provider absent locally is added, and a provider present on
  both sides is refreshed (tracked scalar fields win, models are unioned by
  ``id`` with the tracked definition replacing the same-``id`` entry, and
  ``modelOverrides`` are merged key-by-key). Every *local-only* provider,
  model, ``modelOverrides`` entry and top-level key is preserved verbatim.
* **``enabledModels`` union.** Tracked patterns are appended to the
  destination list, preserving local patterns and de-duplicating.
* **Idempotent.** A second run is semantically a no-op: no duplicated models,
  no lost local entries, and no second backup.
* **Single timestamped backup** (``<file>.bak.<UTC stamp>``) is written before
  the first modification of each file, and only when a change is actually
  made.
* **Atomic writes** (temp file in the same directory + ``os.replace``).
* **Fail-open on unparseable destination JSON.** The file is reported and left
  byte-for-byte intact; the CLI then exits non-zero so the install script can
  warn without aborting.
* **Stdlib only** — no third-party imports.
* **Non-interactive-safe** — never prompts; safe under ``stdin=/dev/null``.

Unverified model metadata
-------------------------
MunitoAI's ``GET /inference/v1/models`` returns model ids only (no context or
pricing metadata). The ``contextWindow``/``maxTokens`` values for the newer
``deepseek-ai/deepseek-v4.1-flash-1017`` entry mirror the existing
``deepseek-ai/deepseek-v4-flash`` entry (400000 / 16384) and are therefore
**unverified** — this helper does not invent values; they live in the tracked
``.pi-config/agent/models.json``. A wrong value only mis-sizes pi's context
accounting. See ``docs/dev/skills-script-paths.md``.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKUP_FORMAT = "%Y%m%dT%H%M%SZ"

# Report status values returned by the file-level merge functions.
STATUS_OK = "ok"
STATUS_UNCHANGED = "unchanged"
STATUS_COPIED = "copied"
STATUS_INVALID_JSON = "invalid_json"


# ---------------------------------------------------------------------------
# Deep merge primitives
# ---------------------------------------------------------------------------


def _deep_merge(dest: Any, tracked: Any) -> Any:
    """Recursively merge ``tracked`` into ``dest``; tracked wins on conflict.

    Dictionaries are merged key-by-key; every other value (including lists) is
    replaced by a deep copy of the tracked value. Neither input is mutated.
    """
    if isinstance(dest, dict) and isinstance(tracked, dict):
        merged = copy.deepcopy(dest)
        for key, tracked_value in tracked.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], tracked_value)
            else:
                merged[key] = copy.deepcopy(tracked_value)
        return merged
    return copy.deepcopy(tracked)


def _merge_models_list(dest_models: Any, tracked_models: Any) -> list:
    """Union model lists by ``id``.

    Destination order is preserved. A tracked model whose ``id`` already
    exists replaces that entry in place (so the tracked definition wins);
    models with an ``id`` not seen locally are appended; models without an
    ``id`` are unioned by position. Local-only models are preserved.
    """
    merged: list = []
    index_by_id: dict[str, int] = {}
    for model in dest_models or []:
        merged.append(copy.deepcopy(model))
        if isinstance(model, dict) and model.get("id") is not None:
            index_by_id[model["id"]] = len(merged) - 1
    for tracked_model in tracked_models or []:
        tracked_id = (
            tracked_model.get("id") if isinstance(tracked_model, dict) else None
        )
        if tracked_id is not None and tracked_id in index_by_id:
            merged[index_by_id[tracked_id]] = copy.deepcopy(tracked_model)
        else:
            merged.append(copy.deepcopy(tracked_model))
            if tracked_id is not None:
                index_by_id[tracked_id] = len(merged) - 1
    return merged


def _merge_provider(dest_provider: Any, tracked_provider: Any) -> dict:
    """Merge one tracked provider into its destination counterpart.

    Tracked scalar/``compat`` fields win (deep-merged for nested dicts),
    ``models`` are unioned by ``id`` and ``modelOverrides`` are merged
    key-by-key. Local-only provider fields are preserved.
    """
    merged: dict = copy.deepcopy(dest_provider) if isinstance(dest_provider, dict) else {}
    if not isinstance(tracked_provider, dict):
        return merged

    for key, tracked_value in tracked_provider.items():
        if key in ("models", "modelOverrides"):
            continue
        if key in merged:
            merged[key] = _deep_merge(merged[key], tracked_value)
        else:
            merged[key] = copy.deepcopy(tracked_value)

    if "models" in tracked_provider:
        merged["models"] = _merge_models_list(
            merged.get("models", []), tracked_provider["models"]
        )
    if "modelOverrides" in tracked_provider:
        merged["modelOverrides"] = _deep_merge(
            merged.get("modelOverrides", {}), tracked_provider["modelOverrides"]
        )
    return merged


# ---------------------------------------------------------------------------
# Document-level merges
# ---------------------------------------------------------------------------


def merge_providers(
    tracked_providers: Any, dest_providers: Any
) -> tuple[dict, dict[str, str]]:
    """Merge tracked providers into the destination provider map.

    Returns ``(merged_providers, summary)`` where ``summary`` maps each tracked
    provider name to ``"added"``, ``"updated"`` or ``"present"``. Local-only
    providers are preserved untouched.
    """
    merged: dict = copy.deepcopy(dest_providers) if isinstance(dest_providers, dict) else {}
    summary: dict[str, str] = {}
    for name, tracked_provider in (tracked_providers or {}).items():
        if name not in merged:
            merged[name] = copy.deepcopy(tracked_provider)
            summary[name] = "added"
            continue
        before = copy.deepcopy(merged[name])
        merged[name] = _merge_provider(merged[name], tracked_provider)
        summary[name] = "present" if merged[name] == before else "updated"
    return merged, summary


def merge_enabled_models(
    tracked_patterns: Any, dest_patterns: Any
) -> tuple[list, list[str]]:
    """Union tracked ``enabledModels`` patterns into the destination list.

    Local patterns keep their order and position; tracked patterns not already
    present are appended. No duplicates are produced. Returns
    ``(merged_patterns, added_patterns)``.
    """
    merged: list = list(dest_patterns or [])
    seen = set(merged)
    added: list[str] = []
    for pattern in tracked_patterns or []:
        if pattern not in seen:
            merged.append(pattern)
            seen.add(pattern)
            added.append(pattern)
    return merged, added


def merge_models_document(
    tracked_doc: Any, dest_doc: Any
) -> tuple[dict, dict]:
    """Merge a tracked ``models.json`` document into a destination document."""
    merged: dict = copy.deepcopy(dest_doc) if isinstance(dest_doc, dict) else {}
    merged_providers, summary = merge_providers(
        (tracked_doc or {}).get("providers", {}), merged.get("providers", {})
    )
    merged["providers"] = merged_providers
    # Preserve any other tracked top-level keys, without dropping local ones.
    for key, value in (tracked_doc or {}).items():
        if key == "providers":
            continue
        if key not in merged:
            merged[key] = copy.deepcopy(value)
    return merged, {"providers": summary}


def merge_settings_document(
    tracked_doc: Any, dest_doc: Any
) -> tuple[dict, dict]:
    """Merge a tracked ``settings.json`` document into a destination document.

    Only ``enabledModels`` is unioned; every other local key is preserved
    (tracked keys are only filled in when the destination lacks them), so an
    operator's theme/defaults are never clobbered.
    """
    merged: dict = copy.deepcopy(dest_doc) if isinstance(dest_doc, dict) else {}
    added: list[str] = []
    if "enabledModels" in (tracked_doc or {}):
        merged_enabled, added = merge_enabled_models(
            tracked_doc["enabledModels"], merged.get("enabledModels", [])
        )
        merged["enabledModels"] = merged_enabled
    for key, value in (tracked_doc or {}).items():
        if key == "enabledModels":
            continue
        if key not in merged:
            merged[key] = copy.deepcopy(value)
    return merged, {
        "added": added,
        "enabledModels": merged.get("enabledModels", []),
    }


# ---------------------------------------------------------------------------
# File I/O: atomic writes and backups
# ---------------------------------------------------------------------------


def _backup_stamp() -> str:
    return datetime.now(timezone.utc).strftime(BACKUP_FORMAT)


def write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` as pretty JSON to ``path`` atomically.

    A temp file is created in the *same* directory (so ``os.replace`` stays on
    one filesystem) and renamed into place. On any failure the temp file is
    removed and the original file is left untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_backup(dest_path: Path, stamp: str | None) -> Path:
    """Write a timestamped byte-copy backup of ``dest_path`` and return it."""
    stamp = stamp or _backup_stamp()
    backup = dest_path.with_name(dest_path.name + ".bak." + stamp)
    backup.write_bytes(dest_path.read_bytes())
    return backup


def _load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# File-level merges
# ---------------------------------------------------------------------------


def merge_models_file(
    tracked_path: Path, dest_path: Path, *, backup_stamp: str | None = None
) -> dict:
    """Merge tracked ``models.json`` into ``dest_path``.

    A missing destination is installed whole from the tracked file (status
    ``copied``). Unparseable destination JSON is reported as ``invalid_json``
    and left untouched (fail-open). Returns a report dictionary.
    """
    tracked_path = Path(tracked_path)
    dest_path = Path(dest_path)

    if not dest_path.exists():
        tracked_doc = _load_json(tracked_path)
        write_json_atomic(dest_path, tracked_doc)
        return {
            "status": STATUS_COPIED,
            "changed": True,
            "backup": None,
            "providers": {name: "added" for name in tracked_doc.get("providers", {})},
        }

    try:
        dest_doc = _load_json(dest_path)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {
            "status": STATUS_INVALID_JSON,
            "changed": False,
            "backup": None,
            "providers": {},
        }

    tracked_doc = _load_json(tracked_path)
    merged, report = merge_models_document(tracked_doc, dest_doc)
    if merged == dest_doc:
        return {
            "status": STATUS_UNCHANGED,
            "changed": False,
            "backup": None,
            "providers": {name: "present" for name in report["providers"]},
        }

    backup = _write_backup(dest_path, backup_stamp)
    write_json_atomic(dest_path, merged)
    return {
        "status": STATUS_OK,
        "changed": True,
        "backup": str(backup),
        "providers": report["providers"],
    }


def merge_settings_file(
    tracked_path: Path, dest_path: Path, *, backup_stamp: str | None = None
) -> dict:
    """Merge tracked ``settings.json`` into ``dest_path`` (``enabledModels`` union)."""
    tracked_path = Path(tracked_path)
    dest_path = Path(dest_path)

    if not dest_path.exists():
        tracked_doc = _load_json(tracked_path)
        write_json_atomic(dest_path, tracked_doc)
        return {
            "status": STATUS_COPIED,
            "changed": True,
            "backup": None,
            "added": list(tracked_doc.get("enabledModels", [])),
        }

    try:
        dest_doc = _load_json(dest_path)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {
            "status": STATUS_INVALID_JSON,
            "changed": False,
            "backup": None,
            "added": [],
        }

    tracked_doc = _load_json(tracked_path)
    merged, report = merge_settings_document(tracked_doc, dest_doc)
    if merged == dest_doc:
        return {
            "status": STATUS_UNCHANGED,
            "changed": False,
            "backup": None,
            "added": [],
        }

    backup = _write_backup(dest_path, backup_stamp)
    write_json_atomic(dest_path, merged)
    return {
        "status": STATUS_OK,
        "changed": True,
        "backup": str(backup),
        "added": report["added"],
    }


def merge_agent_config(
    tracked_dir: Path, dest_dir: Path, *, backup_stamp: str | None = None
) -> dict:
    """Merge both ``models.json`` and ``settings.json`` from tracked to dest."""
    tracked_dir = Path(tracked_dir)
    dest_dir = Path(dest_dir)
    stamp = backup_stamp or _backup_stamp()
    models = merge_models_file(
        tracked_dir / "models.json",
        dest_dir / "models.json",
        backup_stamp=stamp,
    )
    settings = merge_settings_file(
        tracked_dir / "settings.json",
        dest_dir / "settings.json",
        backup_stamp=stamp,
    )
    return {"models": models, "settings": settings}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_models_report(report: dict) -> None:
    print(f"  models.json: {report['status']}")
    for name, status in report.get("providers", {}).items():
        if status == "added":
            print(f"    Added provider: {name}")
        elif status == "updated":
            print(f"    Updated provider: {name}")
        else:
            print(f"    OK: already present provider: {name}")


def _print_settings_report(report: dict) -> None:
    print(f"  settings.json: {report['status']}")
    for pattern in report.get("added", []):
        print(f"    Added enabledModels: {pattern}")
    if report["status"] == "unchanged":
        print("    OK: already present enabledModels")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print(
            "usage: merge_pi_agent_config.py TRACKED_DIR DEST_DIR",
            file=sys.stderr,
        )
        return 2

    tracked_dir, dest_dir = Path(argv[0]), Path(argv[1])
    report = merge_agent_config(tracked_dir, dest_dir)
    _print_models_report(report["models"])
    _print_settings_report(report["settings"])

    invalid = [
        name
        for name, result in report.items()
        if result["status"] == STATUS_INVALID_JSON
    ]
    if invalid:
        print(
            "WARNING: could not parse existing config (left untouched): "
            + ", ".join(invalid),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
