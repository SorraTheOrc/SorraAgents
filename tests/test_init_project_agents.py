"""Behavioural tests for ``scripts/init_project_agents.py``.

The installer emits the canonical project ``AGENTS.md`` structure — a single
reference to the global agent guidance plus a project-specific placeholder —
instead of duplicating the global instruction set. These tests exercise the
public CLI (subprocess) and assert observable file behaviour, never the
script's source text.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "init_project_agents.py"
TEMPLATE = REPO_ROOT / "templates" / "project-AGENTS.md"

CANONICAL_HEADING = "## Global agent guidance"
PROJECT_HEADING = "## Project-specific guidance"
REFERENCE_LINE = "Read the global agent instructions at `~/.pi/agent/AGENTS.md`"
PLACEHOLDER = "(project-specific rules are added by the project owner here, never by copying the global file)"


def run_installer(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
        check=False,
    )


def read_agents(target: Path) -> str:
    return (target / "AGENTS.md").read_text(encoding="utf-8")


# ── AC1: the template is exactly the canonical structure ───────────────────


def test_template_is_the_canonical_structure():
    content = TEMPLATE.read_text(encoding="utf-8")
    assert CANONICAL_HEADING in content
    assert PROJECT_HEADING in content
    assert REFERENCE_LINE in content
    assert PLACEHOLDER in content
    # The canonical structure is a reference + placeholder, not a copy of the
    # global instruction set (which carries the CRITICAL RULES block).
    assert "CRITICAL RULES" not in content


# ── AC2: a fresh project (and re-running) emits the canonical structure ─────


def test_fresh_project_creates_canonical_agents_md(tmp_path: Path):
    result = run_installer("--target", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["action"] == "created"
    assert read_agents(tmp_path) == TEMPLATE.read_text(encoding="utf-8")


def test_rerun_is_idempotent(tmp_path: Path):
    run_installer("--target", str(tmp_path), "--json")
    first = read_agents(tmp_path)
    result = run_installer("--target", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "noop"
    assert read_agents(tmp_path) == first


# ── AC3: existing custom rules keep working, below the reference ───────────


def test_append_keeps_custom_rules_below_the_reference(tmp_path: Path):
    custom = "# My Project\n\nOnly my local rules.\n"
    (tmp_path / "AGENTS.md").write_text(custom, encoding="utf-8")

    result = run_installer("--target", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "appended"

    content = read_agents(tmp_path)
    assert content.startswith(CANONICAL_HEADING)
    assert REFERENCE_LINE in content
    assert custom in content
    # The project's own rules remain below the canonical reference.
    assert content.index(CANONICAL_HEADING) < content.index("# My Project")


def test_skip_leaves_existing_custom_rules_unchanged(tmp_path: Path):
    custom = "# My Project\n\nOnly my local rules.\n"
    (tmp_path / "AGENTS.md").write_text(custom, encoding="utf-8")

    result = run_installer("--target", str(tmp_path), "--mode", "skip", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "skipped"
    assert read_agents(tmp_path) == custom


def test_overwrite_replaces_existing_custom_rules(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("# My Project\n", encoding="utf-8")

    result = run_installer("--target", str(tmp_path), "--mode", "overwrite", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "overwritten"
    assert read_agents(tmp_path) == TEMPLATE.read_text(encoding="utf-8")


def test_legacy_reference_line_is_treated_as_present(tmp_path: Path):
    legacy = "Follow the global AGENTS.md in addition to the rules below.\n\n# My Project\n"
    (tmp_path / "AGENTS.md").write_text(legacy, encoding="utf-8")

    result = run_installer("--target", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["action"] == "noop"
    assert read_agents(tmp_path) == legacy


# ── AC bullet: copying the global file is an explicit opt-in ────────────────


def test_default_does_not_copy_the_global_file(tmp_path: Path):
    result = run_installer("--target", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    content = read_agents(tmp_path)
    assert content == TEMPLATE.read_text(encoding="utf-8")
    assert "CRITICAL RULES" not in content


def test_copy_global_is_opt_in(tmp_path: Path):
    global_source = tmp_path / "AGENTS_GLOBAL.md"
    global_source.write_text("# Global\n\nFull global instruction set.\n", encoding="utf-8")

    result = run_installer(
        "--target",
        str(tmp_path),
        "--copy-global",
        "--global-source",
        str(global_source),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "copied-global"
    assert read_agents(tmp_path) == global_source.read_text(encoding="utf-8")


def test_copy_global_without_source_reports_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Point HOME somewhere with no global file and use a repo without one.
    empty_repo = tmp_path / "empty"
    (empty_repo / "scripts").mkdir(parents=True)
    (empty_repo / "scripts" / "init_project_agents.py").write_text(
        SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    result = subprocess.run(
        [sys.executable, str(empty_repo / "scripts" / "init_project_agents.py"),
         "--target", str(tmp_path / "proj"), "--copy-global", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["success"] is False


# ── Dry run and JSON contract ──────────────────────────────────────────────


def test_dry_run_writes_nothing(tmp_path: Path):
    result = run_installer("--target", str(tmp_path), "--dry-run", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "created"
    assert payload["dry_run"] is True
    assert not (tmp_path / "AGENTS.md").exists()


def test_json_output_contract(tmp_path: Path):
    result = run_installer("--target", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) >= {"success", "path", "action", "dry_run", "message"}
    assert payload["path"] == str(tmp_path / "AGENTS.md")
