"""Hidden project-local skill-test detection and emission.

SA-0MVQQ9KZO0010XXS (F1 of SA-0MVQJSGLY0098DVB): a project's skill tests live
under ``.pi/skills/<name>/tests``, where pytest's default ``norecursedirs``
(``.*``) never reaches them from a recursive root run. These tests pin:

- AC1 — candidate detection (both directory names, sorted/deduplicated, ``*.py``
  required, other hidden roots ignored).
- AC2 — emission (exactly one extra quiet pytest command, stable ordering,
  emitted without a root pytest suite, quoting, and its position between the
  canonical pytest command and the node commands).

Precedence rules (pinned ``suiteCommands``, no-double-execution) belong to
SA-0MVQQ9NV4005UKYZ (F2) and are tested there.
"""

from __future__ import annotations

import json

# Resolve the module under test the same way ``test_run_tests_scope.py`` does:
# put the runner's own directory on ``sys.path`` and import it by module name.
# A bare ``from test.scripts.run_tests import …`` only resolves when the repo
# root *and* the skills root are already on ``sys.path`` — true inside a full
# run (``tests/conftest.py``) but NOT when the finish gate selects a changed
# subset that contains only this file (the gate collects explicit paths).
import sys as _sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"  # <skills>/test/scripts
if str(_RUNNER_DIR) not in _sys.path:
    _sys.path.insert(0, str(_RUNNER_DIR))

from run_tests import (
    NOTICE_HIDDEN_SKILL_TESTS_UNCOVERED,
    full_suite_commands,
    full_suite_notices,
    hidden_skill_test_command,
    hidden_skill_test_dirs,
    hidden_skill_test_notice,
)

PYTEST_CMD = "pytest -q -r a --disable-warnings"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pytest_ini(root: Path) -> None:
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")


def _write_package_json(root: Path, scripts: dict[str, str] | None = None) -> None:
    (root / "package.json").write_text(
        json.dumps({"scripts": scripts or {"test": "vitest run"}}),
        encoding="utf-8",
    )


def _skill_test_dir(
    root: Path, skill: str, dirname: str = "tests", *, py_file: str | None = "test_demo.py",
) -> Path:
    """Create ``.pi/skills/<skill>/<dirname>`` with an optional ``*.py`` file."""
    directory = root / ".pi" / "skills" / skill / dirname
    directory.mkdir(parents=True, exist_ok=True)
    if py_file is not None:
        (directory / py_file).write_text(
            "def test_demo() -> None:\n    assert True\n", encoding="utf-8",
        )
    return directory


# ---------------------------------------------------------------------------
# AC1 — detection
# ---------------------------------------------------------------------------


class TestHiddenSkillTestDirs:
    def test_single_skill_with_tests_dir(self, tmp_path: Path) -> None:
        _skill_test_dir(tmp_path, "alpha")

        assert hidden_skill_test_dirs(tmp_path) == [".pi/skills/alpha/tests"]

    def test_single_skill_with_singular_test_dir(self, tmp_path: Path) -> None:
        _skill_test_dir(tmp_path, "beta", "test")

        assert hidden_skill_test_dirs(tmp_path) == [".pi/skills/beta/test"]

    def test_several_skills_are_sorted_and_deduplicated(self, tmp_path: Path) -> None:
        _skill_test_dir(tmp_path, "zeta")
        _skill_test_dir(tmp_path, "alpha", "test")
        _skill_test_dir(tmp_path, "mid")

        assert hidden_skill_test_dirs(tmp_path) == [
            ".pi/skills/alpha/test",
            ".pi/skills/mid/tests",
            ".pi/skills/zeta/tests",
        ]

    def test_skill_with_both_dirnames_reports_both_stably(self, tmp_path: Path) -> None:
        """A skill with ``tests/`` *and* ``test/`` yields both, in one order."""
        _skill_test_dir(tmp_path, "dual", "tests")
        _skill_test_dir(tmp_path, "dual", "test")

        assert hidden_skill_test_dirs(tmp_path) == [
            ".pi/skills/dual/test",
            ".pi/skills/dual/tests",
        ]

    def test_skill_without_test_dirs_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / ".pi" / "skills" / "no-tests" / "scripts").mkdir(parents=True)

        assert hidden_skill_test_dirs(tmp_path) == []

    def test_test_dir_without_python_files_is_ignored(self, tmp_path: Path) -> None:
        directory = _skill_test_dir(tmp_path, "empty", py_file=None)
        (directory / "notes.md").write_text("# not a test\n", encoding="utf-8")

        assert hidden_skill_test_dirs(tmp_path) == []

    def test_non_pi_hidden_roots_are_never_scanned(self, tmp_path: Path) -> None:
        """``.claude/``, ``.git/`` and ``.worklog/`` skill-shaped trees do not
        produce candidates — only ``.pi/skills`` is defined as a test surface."""
        for root_name in (".claude", ".git", ".worklog"):
            directory = tmp_path / root_name / "skills" / "x" / "tests"
            directory.mkdir(parents=True)
            (directory / "test_x.py").write_text("def test_x():\n    pass\n")

        assert hidden_skill_test_dirs(tmp_path) == []

    def test_project_without_pi_skills_resolves_to_nothing(self, tmp_path: Path) -> None:
        _write_pytest_ini(tmp_path)

        assert hidden_skill_test_dirs(tmp_path) == []


# ---------------------------------------------------------------------------
# AC2 — emission
# ---------------------------------------------------------------------------


class TestHiddenSkillTestCommand:
    def test_one_command_lists_every_candidate(self) -> None:
        command = hidden_skill_test_command(
            [".pi/skills/alpha/tests", ".pi/skills/beta/test"],
        )

        assert command == (
            f"{PYTEST_CMD} .pi/skills/alpha/tests .pi/skills/beta/test"
        )

    def test_paths_with_spaces_are_quoted(self) -> None:
        command = hidden_skill_test_command([".pi/skills/my skill/tests"])

        assert command == f"{PYTEST_CMD} '.pi/skills/my skill/tests'"


class TestFullSuiteCommandsWithHiddenSkillTests:
    def test_emits_exactly_one_extra_command_after_pytest(self, tmp_path: Path) -> None:
        _write_pytest_ini(tmp_path)
        _skill_test_dir(tmp_path, "alpha")
        _skill_test_dir(tmp_path, "beta")

        assert full_suite_commands(tmp_path) == [
            PYTEST_CMD,
            f"{PYTEST_CMD} .pi/skills/alpha/tests .pi/skills/beta/tests",
        ]

    def test_extra_command_precedes_node_commands(self, tmp_path: Path) -> None:
        _write_pytest_ini(tmp_path)
        (tmp_path / "tests" / "unit").mkdir(parents=True)
        _skill_test_dir(tmp_path, "alpha")

        assert full_suite_commands(tmp_path) == [
            PYTEST_CMD,
            f"{PYTEST_CMD} .pi/skills/alpha/tests",
            'node --test "tests/unit/**/*.mjs"',
        ]

    def test_emitted_without_a_root_pytest_suite(self, tmp_path: Path) -> None:
        """Skill tests alone justify the command: ``repo_has_pytest_suite()``
        is false (no config marker, no root test dirs) yet the tests exist."""
        _skill_test_dir(tmp_path, "alpha")

        assert full_suite_commands(tmp_path) == [
            f"{PYTEST_CMD} .pi/skills/alpha/tests",
        ]

    def test_no_candidates_leaves_resolution_unchanged(self, tmp_path: Path) -> None:
        _write_pytest_ini(tmp_path)
        _write_package_json(tmp_path)
        (tmp_path / "tests" / "unit").mkdir(parents=True)

        assert full_suite_commands(tmp_path) == [
            PYTEST_CMD,
            "npm --silent test -- tests/unit",
        ]


# ---------------------------------------------------------------------------
# AC3/AC4 — pinned precedence + no double execution
# (SA-0MVQQ9NV4005UKYZ, elaborating the parent's D3)
# ---------------------------------------------------------------------------


def _write_test_config(root: Path, suite_commands: list[str]) -> None:
    (root / ".pi").mkdir(parents=True, exist_ok=True)
    (root / ".pi" / "test-config.json").write_text(
        json.dumps({"suiteCommands": suite_commands}), encoding="utf-8",
    )


class TestPinnedSuiteCommandsPrecedence:
    """AC3: a pinned list is authoritative; the gap is reported, not patched."""

    def test_pinned_list_is_returned_verbatim(self, tmp_path: Path) -> None:
        pinned = ["pytest -q tests", "npm --silent run lint"]
        _write_pytest_ini(tmp_path)
        _write_test_config(tmp_path, pinned)
        _skill_test_dir(tmp_path, "alpha")

        assert full_suite_commands(tmp_path) == pinned

    def test_notice_names_uncovered_hidden_dirs(self, tmp_path: Path) -> None:
        _write_test_config(tmp_path, ["pytest -q tests"])
        _skill_test_dir(tmp_path, "alpha")
        _skill_test_dir(tmp_path, "beta", "test")

        notice = hidden_skill_test_notice(tmp_path)

        assert notice is not None
        assert notice["code"] == NOTICE_HIDDEN_SKILL_TESTS_UNCOVERED
        assert notice["directories"] == [
            ".pi/skills/alpha/tests",
            ".pi/skills/beta/test",
        ]
        assert notice["source"] == ".pi/test-config.json"

    def test_no_notice_when_the_pin_covers_every_candidate(
        self, tmp_path: Path,
    ) -> None:
        """A pin that names the skill test dir leaves nothing to report."""
        _write_test_config(tmp_path, ["pytest -q .pi/skills/alpha/tests"])
        _skill_test_dir(tmp_path, "alpha")

        assert hidden_skill_test_notice(tmp_path) is None
        assert full_suite_notices(tmp_path) == []

    def test_empty_pinned_list_is_verbatim_with_a_notice(self, tmp_path: Path) -> None:
        """An explicitly-empty pin means "no runnable suite" and is honoured."""
        _write_test_config(tmp_path, [])
        _skill_test_dir(tmp_path, "alpha")

        assert full_suite_commands(tmp_path) == []
        notice = hidden_skill_test_notice(tmp_path)
        assert notice is not None
        assert notice["directories"] == [".pi/skills/alpha/tests"]

    def test_no_notice_without_a_pinned_list(self, tmp_path: Path) -> None:
        _write_pytest_ini(tmp_path)
        _skill_test_dir(tmp_path, "alpha")

        assert hidden_skill_test_notice(tmp_path) is None
        assert full_suite_notices(tmp_path) == []


class TestNoDoubleExecution:
    """AC4: a candidate the project's pytest config already collects is skipped."""

    def test_ini_testpaths_cover_of_the_candidate_skips_it(self, tmp_path: Path) -> None:
        (tmp_path / "pytest.ini").write_text(
            "[pytest]\ntestpaths = .pi/skills/alpha/tests\n", encoding="utf-8",
        )
        _skill_test_dir(tmp_path, "alpha")

        assert full_suite_commands(tmp_path) == [PYTEST_CMD]

    def test_ini_addopts_path_covers_the_candidate(self, tmp_path: Path) -> None:
        (tmp_path / "pytest.ini").write_text(
            "[pytest]\naddopts = -ra .pi/skills\n", encoding="utf-8",
        )
        _skill_test_dir(tmp_path, "alpha")

        assert full_suite_commands(tmp_path) == [PYTEST_CMD]

    def test_pyproject_testpaths_cover_skips_the_candidate(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n"
            'testpaths = [".pi/skills/alpha/tests"]\n',
            encoding="utf-8",
        )
        _skill_test_dir(tmp_path, "alpha")

        assert full_suite_commands(tmp_path) == [PYTEST_CMD]

    def test_root_level_token_does_not_count_as_coverage(self, tmp_path: Path) -> None:
        """``testpaths = .``/``tests`` never reach ``.pi`` (default
        ``norecursedirs`` prunes ``.*``), so the command is still emitted."""
        (tmp_path / "pytest.ini").write_text(
            "[pytest]\ntestpaths = . tests\n", encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_root.py").write_text("def test_x():\n    pass\n")
        _skill_test_dir(tmp_path, "alpha")

        assert full_suite_commands(tmp_path) == [
            PYTEST_CMD,
            f"{PYTEST_CMD} .pi/skills/alpha/tests",
        ]

    def test_uncovered_candidate_is_still_emitted(self, tmp_path: Path) -> None:
        (tmp_path / "pytest.ini").write_text(
            "[pytest]\ntestpaths = .pi/skills/beta/tests\n", encoding="utf-8",
        )
        _skill_test_dir(tmp_path, "alpha")
        _skill_test_dir(tmp_path, "beta")

        assert full_suite_commands(tmp_path) == [
            PYTEST_CMD,
            f"{PYTEST_CMD} .pi/skills/alpha/tests",
        ]


class TestNoticeIsNonFatal:
    """AC4b: a notice never changes resolution or the exit status."""

    def test_resolution_is_identical_with_and_without_a_notice(
        self, tmp_path: Path,
    ) -> None:
        pinned = ["pytest -q tests"]
        _write_test_config(tmp_path, pinned)
        _skill_test_dir(tmp_path, "alpha")

        assert hidden_skill_test_notice(tmp_path) is not None
        assert full_suite_commands(tmp_path) == pinned

    def test_notice_carries_a_stable_machine_readable_shape(
        self, tmp_path: Path,
    ) -> None:
        _write_test_config(tmp_path, ["pytest -q tests"])
        _skill_test_dir(tmp_path, "alpha")

        notice = hidden_skill_test_notice(tmp_path)
        assert set(notice) == {"code", "source", "directories", "message"}
        assert isinstance(notice["directories"], list)
        assert notice["message"]
