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
    full_suite_commands,
    hidden_skill_test_command,
    hidden_skill_test_dirs,
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
