"""Tests for the project-local skill-extension loader (SA-0MSQ7MQEJ0064ZB0).

Contract under test:

- AC1: discovery is ``<project_root>/.pi/skills_extensions/<skill>/``, with the
  project root resolved from the invoking cwd (git root, cwd fallback).
- AC2: optional ``SKILL_PREFIX.md`` / ``SKILL_POSTFIX.md`` prose hooks load
  additively; absence is a no-op.
- AC3: optional ``extension.json`` parses to a machine-readable mapping;
  malformed JSON or a non-object top level raises a clear
  :class:`SkillExtensionError` naming the file.
- AC4: an absent directory is a no-op (no error, no behaviour change).
- AC5: project-root resolution from a nested cwd, present/absent/partial files,
  malformed JSON, unknown extra keys and one end-to-end consumer demonstration.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = REPO_ROOT / "skill"
for _path in (str(REPO_ROOT), str(_SKILLS_ROOT)):
    if _path not in sys.path:
        sys.path.append(_path)

from shared.skill_extensions import (
    DATA_FILENAME,
    EXTENSIONS_DIR,
    POSTFIX_FILENAME,
    PREFIX_FILENAME,
    SkillExtensionError,
    extension_dir,
    load_extension,
    resolve_project_root,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _write_extension(root: Path, skill: str, **files: str) -> Path:
    directory = root / EXTENSIONS_DIR / skill
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    return directory


# ---------------------------------------------------------------------------
# Project-root resolution (AC1)
# ---------------------------------------------------------------------------


class TestResolveProjectRoot:
    def test_resolves_git_root_from_nested_cwd(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        nested = repo / "a" / "b"
        nested.mkdir(parents=True)
        assert resolve_project_root(nested) == repo.resolve()

    def test_falls_back_to_cwd_when_not_a_git_repo(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert resolve_project_root(plain) == plain.resolve()

    def test_accepts_a_file_and_uses_its_parent(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        a_file = repo / "module.py"
        a_file.write_text("# module\n", encoding="utf-8")
        assert resolve_project_root(a_file) == repo.resolve()


# ---------------------------------------------------------------------------
# Extension directory (AC1)
# ---------------------------------------------------------------------------


class TestExtensionDir:
    def test_directory_shape(self, tmp_path: Path) -> None:
        assert extension_dir("test", tmp_path) == tmp_path / EXTENSIONS_DIR / "test"

    @pytest.mark.parametrize("bad", ["", "..", "../evil", "a/b", "a\\b"])
    def test_rejects_unsafe_skill_names(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError):
            extension_dir(bad, tmp_path)


# ---------------------------------------------------------------------------
# Loader behaviour (AC2, AC3, AC4)
# ---------------------------------------------------------------------------


class TestLoadExtension:
    def test_absent_extension_is_a_noop(self, tmp_path: Path) -> None:
        ext = load_extension("test", tmp_path)
        assert ext.present is False
        assert ext.prefix is None
        assert ext.postfix is None
        assert ext.data == {}
        assert ext.data_path is None

    def test_prefix_only(self, tmp_path: Path) -> None:
        _write_extension(tmp_path, "test", **{PREFIX_FILENAME: "Project intro."})
        ext = load_extension("test", tmp_path)
        assert ext.present is True
        assert ext.prefix == "Project intro."
        assert ext.postfix is None
        assert ext.prefix_path is not None

    def test_postfix_only(self, tmp_path: Path) -> None:
        _write_extension(tmp_path, "test", **{POSTFIX_FILENAME: "Project outro."})
        ext = load_extension("test", tmp_path)
        assert ext.present is True
        assert ext.postfix == "Project outro."
        assert ext.prefix is None
        assert ext.postfix_path is not None

    def test_empty_prefix_file_still_counts_as_present(self, tmp_path: Path) -> None:
        _write_extension(tmp_path, "test", **{PREFIX_FILENAME: ""})
        ext = load_extension("test", tmp_path)
        assert ext.present is True
        assert ext.prefix == ""

    def test_machine_readable_data_loads(self, tmp_path: Path) -> None:
        payload = {"types": {"unit": ["pytest tests/unit -q"]}}
        _write_extension(tmp_path, "test", **{DATA_FILENAME: json.dumps(payload)})
        ext = load_extension("test", tmp_path)
        assert ext.present is True
        assert ext.data == payload
        assert ext.data_path is not None

    def test_unknown_extra_keys_are_preserved(self, tmp_path: Path) -> None:
        payload = {"types": {}, "futureKey": {"anything": 1}}
        _write_extension(tmp_path, "test", **{DATA_FILENAME: json.dumps(payload)})
        assert load_extension("test", tmp_path).data == payload

    def test_malformed_json_names_the_file(self, tmp_path: Path) -> None:
        _write_extension(tmp_path, "test", **{DATA_FILENAME: "{not json"})
        with pytest.raises(SkillExtensionError) as excinfo:
            load_extension("test", tmp_path)
        assert DATA_FILENAME in str(excinfo.value)

    def test_non_object_json_names_the_file(self, tmp_path: Path) -> None:
        _write_extension(tmp_path, "test", **{DATA_FILENAME: json.dumps(["a", "b"])})
        with pytest.raises(SkillExtensionError) as excinfo:
            load_extension("test", tmp_path)
        assert DATA_FILENAME in str(excinfo.value)

    def test_project_root_supplied_explicitly(self, tmp_path: Path) -> None:
        _write_extension(tmp_path, "test", **{PREFIX_FILENAME: "hi"})
        ext = load_extension("test", tmp_path)
        assert ext.project_root == tmp_path.resolve()
        assert ext.directory == tmp_path / EXTENSIONS_DIR / "test"


# ---------------------------------------------------------------------------
# End-to-end consumer demonstration (AC5)
# ---------------------------------------------------------------------------


class TestConsumerDemonstration:
    def test_test_skill_reads_type_map_through_the_loader(self, tmp_path: Path) -> None:
        """A skill script can resolve its machine-readable map from the project.

        This mirrors how the test skill consumes the convention
        (SA-0MTJQB2MA008HMO6): discovery via the loader, mapping read from
        ``extension.json`` — never parsed from prose.
        """
        types = {
            "unit": ["pytest tests/unit -q"],
            "dev": ["pytest tests/unit tests/integration -q"],
            "full": ["pytest -q"],
        }
        _write_extension(tmp_path, "test", **{DATA_FILENAME: json.dumps({"types": types})})

        ext = load_extension("test", tmp_path)
        commands = ext.data.get("types", {}).get("unit")
        assert commands == ["pytest tests/unit -q"]
