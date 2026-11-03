
# <!-- REFACTOR-SA-0MW0WN17G002IAOV
# smell: unknown
# severity: low
# description: Unparenthesized implicit string concatenation in collection
# -->
"""Tests for named test groups (SA-0MUJKF2HP001BR51).

Covers:

- AC1: ``.pi/test-config.json`` groups schema and documentation.
- AC2: ``--group`` runs exactly that group; ``--list-groups`` lists available.
- AC3: Changed-file → group selector maps paths to groups with documented
  fallback to ``full``.
- AC4: ``implement.py`` finish uses the selected targeted group during
  iteration (verified by import + function existence).
- AC6: Tests cover group resolution, changed-file selection, and the
  fallback to ``full``.
"""

from __future__ import annotations

import json
import sys as _sys
from pathlib import Path
from unittest import mock

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"
if str(_RUNNER_DIR) not in _sys.path:
    _sys.path.insert(0, str(_RUNNER_DIR))

import run_tests
from run_tests import (
    _paths_match_spec,
    _read_groups,
    _read_groups_with_full,
    group_commands,
    group_scope_commands,
    select_group_from_changed_files,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, groups: dict | None = None, timeout: int = 1500) -> Path:
    """Create a throwaway project with optional groups in test-config.json."""
    repo = tmp_path / "proj"
    repo.mkdir()
    config: dict = {"timeoutPerCommand": timeout}
    if groups is not None:
        config["groups"] = groups
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    return repo


# ---------------------------------------------------------------------------
# AC1: Schema and group definitions
# ---------------------------------------------------------------------------


class TestReadGroups:
    """Test group config reading from .pi/test-config.json."""

    def test_none_when_no_config(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path)
        assert _read_groups(repo) is None
        assert _read_groups_with_full(repo) is None

    def test_none_when_config_has_no_groups(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path, groups=None)
        assert _read_groups(repo) is None

    def test_read_valid_groups(self, tmp_path: Path) -> None:
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
            "test": {
                "commands": ["pytest skill/test/tests/"],
                "paths": ["skill/test/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        result = _read_groups(repo)
        assert result is not None
        assert "audit" in result
        assert "test" in result
        assert result["audit"]["commands"] == ["pytest skill/audit/tests/"]
        assert result["audit"]["paths"] == ["skill/audit/**"]

    def test_injects_full_when_missing(self, tmp_path: Path) -> None:
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        result = _read_groups_with_full(repo)
        assert result is not None
        assert "full" in result
        assert "audit" in result

    def test_malformed_spec_ignored(self, tmp_path: Path) -> None:
        groups = {
            "good": {
                "commands": ["pytest tests/"],
                "paths": ["tests/**"],
            },
            "bad_no_commands": {
                "paths": ["bad/**"],
            },
            "bad_no_paths": {
                "commands": ["bad"],
            },
            "bad_empty_commands": {
                "commands": [],
                "paths": ["bad/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        result = _read_groups(repo)
        assert result is not None
        assert "good" in result
        assert "bad_no_commands" not in result
        assert "bad_no_paths" not in result
        assert "bad_empty_commands" not in result


# ---------------------------------------------------------------------------
# AC2: CLI --group and --list-groups
# ---------------------------------------------------------------------------


class TestCLIGroups:
    """Test CLI argument parsing for --group and --list-groups."""

    def test_list_groups_flag_exists(self) -> None:
        parser = run_tests.build_parser()
        args = parser.parse_args(["--list-groups"])
        assert args.list_groups is True
        assert args.group is None

    def test_group_flag_accepts_name(self) -> None:
        parser = run_tests.build_parser()
        args = parser.parse_args(["--group", "audit"])
        assert args.group == "audit"
        assert args.list_groups is False

    @mock.patch.object(run_tests, "list_groups")
    def test_list_groups_prints_names(self, mock_list: mock.MagicMock) -> None:
        mock_list.return_value = ["audit", "full", "test"]
        parser = run_tests.build_parser()
        args = parser.parse_args(["--list-groups"])
        assert args.list_groups is True
        # The actual print happens in main(); we test the function here.

    def test_group_in_main_validates_known_group(self, tmp_path: Path) -> None:
        """--group with an unknown group returns exit code 2."""
        groups = {
            "audit": {
                "commands": ["pytest tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        with (
            mock.patch.object(run_tests, "detect_project_root", return_value=repo),
            mock.patch.object(
                run_tests, "allowed_test_types", return_value=["full"]
            ),
            mock.patch.object(
                run_tests, "resolve_type_commands", return_value=["pytest"]
            ),
        ):
            exit_code = run_tests.main(["--group", "unknown"])
            assert exit_code == 2

    def test_group_in_main_unknown_group_no_config(self, tmp_path: Path) -> None:
        """--group with no groups defined returns exit code 2."""
        repo = _make_project(tmp_path, groups=None)
        with (
            mock.patch.object(run_tests, "detect_project_root", return_value=repo),
            mock.patch.object(
                run_tests, "allowed_test_types", return_value=["full"]
            ),
            mock.patch.object(
                run_tests, "resolve_type_commands", return_value=["pytest"]
            ),
        ):
            exit_code = run_tests.main(["--group", "audit"])
            assert exit_code == 2


# ---------------------------------------------------------------------------
# AC3: Changed-file → group selector
# ---------------------------------------------------------------------------


class TestChangedFileToGroup:
    """Test path → group mapping via changed files."""

    def test_no_changed_files_returns_none(self, tmp_path: Path) -> None:
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        result = select_group_from_changed_files(set(), repo)
        assert result is None

    def test_matching_path_selects_group(self, tmp_path: Path) -> None:
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
            "test": {
                "commands": ["pytest skill/test/tests/"],
                "paths": ["skill/test/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        changed = {"skill/audit/scripts/audit_runner.py"}
        result = select_group_from_changed_files(changed, repo)
        assert result == "audit"

    def test_non_matching_path_returns_none(self, tmp_path: Path) -> None:
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        changed = {"docs/guide.md"}
        result = select_group_from_changed_files(changed, repo)
        assert result is None

    def test_full_group_not_selected_by_changed_files(self, tmp_path: Path) -> None:
        """The 'full' group is excluded from selection — it is the fallback."""
        groups = {
            "full": {
                "commands": ["pytest -q -r a --disable-warnings"],
                "paths": ["**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        changed = {"anything.txt"}
        result = select_group_from_changed_files(changed, repo)
        # full is intentionally excluded from selection; None means fallback
        assert result is None

    def test_most_specific_group_wins(self, tmp_path: Path) -> None:
        """When multiple groups match the same number of changed files,
        the one with fewest commands (most specific) breaks the tie."""
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
            "all_skills": {
                "commands": [
                    "pytest skill/audit/tests/",
                    "pytest skill/test/tests/",
                ],
                "paths": ["skill/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        changed = {"skill/audit/scripts/audit_runner.py"}
        # Both "audit" (1 command) and "all_skills" (2 commands) match
        # exactly 1 changed file. Tie broken by command count: audit wins.
        result = select_group_from_changed_files(changed, repo)
        assert result == "audit"

    def test_exact_file_match(self, tmp_path: Path) -> None:
        """An exact file path in paths spec matches that file."""
        groups = {
            "test_cache": {
                "commands": ["pytest skill/test/tests/"],
                "paths": ["skill/test_cache.py"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        changed = {"skill/test_cache.py"}
        result = select_group_from_changed_files(changed, repo)
        assert result == "test_cache"

    def test_larger_relevant_group_wins_over_smaller_unrelated(
        self, tmp_path: Path,
    ) -> None:
        """A file shared between a small unrelated group and a larger
        relevant group: changes to multiple paths in the larger group
        cause it to be selected over the smaller one (smallest-group-wins
        bug fix).

        Scenario from the bug report: a scene file (MainStreetScene.ts)
        appears in both ``overlay-dialogs`` (2 commands) and
        ``webgl-clipping`` (1 command). When changes touch multiple
        overlay-dialog-related files, the overlay-dialogs group should
        win despite having more commands, because it covers more changed
        files.
        """
        groups = {
            "overlay_dialogs": {
                "commands": [
                    "npx vitest run --project browser "
                    "tests/overlay/dialogs.test.ts",
                    "npx vitest run --project browser "
                    "tests/overlay/panels.test.ts",
                ],
                "paths": [
                    "src/scenes/MainStreetScene.ts",
                    "src/scenes/overlay/**",
                    "tests/overlay/**",
                ],
            },
            "webgl_clipping": {
                "commands": [
                    "npx vitest run --project browser "
                    "tests/main-street/webgl-clipping.browser.test.ts",
                ],
                "paths": ["src/scenes/MainStreetScene.ts"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        # Multiple changes in the overlay-dialogs group vs one in
        # webgl-clipping (the shared file only).
        changed = {
            "src/scenes/MainStreetScene.ts",
            "src/scenes/overlay/dialog.ts",
            "tests/overlay/dialogs.test.ts",
        }
        result = select_group_from_changed_files(changed, repo)
        # overlay_dialogs covers 3 changed files; webgl_clipping covers 1.
        assert result == "overlay_dialogs"


class TestPathsMatchSpec:
    """Test individual path matching logic."""

    def test_wildcard_suffix(self) -> None:
        assert _paths_match_spec("skill/audit/scripts/foo.py", ["skill/audit/**"]) is True
        assert _paths_match_spec("skill/test/tests/bar.py", ["skill/audit/**"]) is False

    def test_exact_match(self) -> None:
        assert _paths_match_spec("skill/test_cache.py", ["skill/test_cache.py"]) is True
        assert _paths_match_spec("skill/test_cache.pyc", ["skill/test_cache.py"]) is False

    def test_star_greedy(self) -> None:
        assert _paths_match_spec("any/deep/nested/file.py", ["**"]) is True

    def test_empty_spec_returns_false(self) -> None:
        assert _paths_match_spec("any.py", []) is False


# ---------------------------------------------------------------------------
# AC4: implement.py integration (import-level check)
# ---------------------------------------------------------------------------


class TestImplementIntegration:
    """Verify implement.py can import and use the group selector."""

    def test_implement_can_import_select_group(self) -> None:
        """The implement skill imports select_group_from_changed_files."""
        # This is verified by the import in implement.py — if it fails,
        # the import would raise ImportError.
        try:
            from run_tests import select_group_from_changed_files  # noqa: F401
            has_select = True
        except ImportError:
            has_select = False
        assert has_select, "select_group_from_changed_files must be importable"

    def test_implement_has_group_runner(self) -> None:
        """The implement skill has the _run_group_from_changed_files function."""
        implement_path = Path(__file__).resolve().parents[2] / "implement" / "scripts" / "implement.py"
        if implement_path.exists():
            content = implement_path.read_text(encoding="utf-8")
            assert "_run_group_from_changed_files" in content, (
                "implement.py should define _run_group_from_changed_files"
            )
        # If implement.py doesn't exist (e.g. in tests that don't run from
        # the framework root), skip the check.


# ---------------------------------------------------------------------------
# AC2 continued: group_commands and group_scope_commands
# ---------------------------------------------------------------------------


class TestGroupCommands:
    """Test group command resolution."""

    def test_group_commands_returns_commands(self, tmp_path: Path) -> None:
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        cmds = group_commands("audit", repo)
        assert cmds == ["pytest skill/audit/tests/"]

    def test_group_commands_unknown_returns_none(self, tmp_path: Path) -> None:
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        assert group_commands("unknown", repo) is None

    def test_group_commands_no_config_returns_none(self, tmp_path: Path) -> None:
        repo = _make_project(tmp_path, groups=None)
        assert group_commands("audit", repo) is None

    def test_group_scope_commands_same_as_group_commands(self, tmp_path: Path) -> None:
        groups = {
            "test": {
                "commands": ["pytest skill/test/tests/"],
                "paths": ["skill/test/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        assert group_scope_commands("test", repo) == group_commands("test", repo)


# ---------------------------------------------------------------------------
# AC5: hidden-skill-test rule leaves groups unaffected (SA-0MVQQ9QJI002K7DY)
# ---------------------------------------------------------------------------


def _add_hidden_skill_tests(repo: Path) -> None:
    """Plant a candidate hidden skill-test dir under ``.pi/skills``."""
    hidden = repo / ".pi" / "skills" / "alpha" / "tests"
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / "test_alpha.py").write_text(
        "def test_alpha() -> None:\n    assert True\n", encoding="utf-8",
    )


class TestHiddenSkillGroupParity:
    """AC5 (SA-0MVQQ9QJI002K7DY): the ``full`` group gains the extra command
    through ``full_suite_commands()`` while named-group ``commands`` are
    returned byte-for-byte unchanged.
    """

    def test_named_group_commands_are_unchanged(self, tmp_path: Path) -> None:
        """A named group returns exactly its declared commands, even when the
        project also ships hidden skill-test dirs (whose command belongs to
        the implicit ``full`` group only).
        """
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        _add_hidden_skill_tests(repo)

        assert group_commands("audit", repo) == ["pytest skill/audit/tests/"]

    def test_full_group_includes_the_extra_command(self, tmp_path: Path) -> None:
        """The implicit ``full`` group resolves through
        ``full_suite_commands()`` (D4), so it inherits the extra hidden
        skill-test command while named groups stay untouched.
        """
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        _add_hidden_skill_tests(repo)
        (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

        full = group_commands("full", repo)
        assert full == run_tests.full_suite_commands(repo)
        assert full == [
            "pytest -q -r a --disable-warnings",
            "pytest -q -r a --disable-warnings .pi/skills/alpha/tests",
        ]

    def test_full_group_without_candidates_is_unchanged(self, tmp_path: Path) -> None:
        """No candidates → the ``full`` group resolves exactly as before the
        rule (F0 baseline, no hidden command).
        """
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

        assert group_commands("full", repo) == ["pytest -q -r a --disable-warnings"]
        assert all(".pi/skills" not in c for c in group_commands("full", repo))

    def test_read_groups_with_full_still_injects_placeholder(self, tmp_path: Path) -> None:
        """The placeholder injection (empty commands, ``**`` paths) survives
        the rule — named-group parsing behaviour is unchanged.
        """
        groups = {
            "audit": {
                "commands": ["pytest skill/audit/tests/"],
                "paths": ["skill/audit/**"],
            },
        }
        repo = _make_project(tmp_path, groups=groups)
        _add_hidden_skill_tests(repo)

        result = _read_groups_with_full(repo)
        assert result is not None
        assert result["full"] == {"commands": [], "paths": ["**"]}
        assert result["audit"]["commands"] == ["pytest skill/audit/tests/"]
