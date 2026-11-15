"""Tests for scope-aware test execution in run_tests.py.

Tests cover:
- `compute_changed_files()` with merge-base resolution and fallbacks
- `map_changed_to_tests()` heuristic mapping
- `map_changed_to_tests()` import-graph expansion
- `changed_scope_commands()` integration with full_suite_commands
- Scope carried in run_suite/run_all results
- Custom suiteCommands no longer disable changed-scope selection
- TypeScript/vitest .test.ts / .spec.ts convention mapping
- Named test groups honoured by the changed-file selector
- Specific (not generic) fallback reasons
- CLI flag parsing
"""

from __future__ import annotations

import json
import shutil

# Resolve the module under test — same pattern as run_tests.py itself.
# The runner lives at <skills>/test/scripts/run_tests.py; inserting that
# scripts dir makes `import run_tests` work from any cwd WITHOUT depending
# on conftest collection order (the stdlib `test` package would otherwise
# shadow <skills>/test when only the skills root is on sys.path).
import sys as _sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"  # <skills>/test/scripts
if str(_RUNNER_DIR) not in _sys.path:
    _sys.path.insert(0, str(_RUNNER_DIR))

# Repository root of the checkout under test (…/skill/test/tests → root).
_REPO_ROOT = _SCRIPT_DIR.parents[2]

import run_tests
from run_tests import (
    CHANGED_SCOPE_GROUP_INCOMPLETE,
    CHANGED_SCOPE_NO_CHANGES,
    CHANGED_SCOPE_NO_GROUP,
    CHANGED_SCOPE_NO_SOURCE,
    CHANGED_SCOPE_NO_TOOLING,
    build_parser,
    changed_scope_commands,
    changed_scope_selection,
    compute_changed_files,
    map_changed_to_tests,
    run_all,
    run_suite,
    run_summary,
)
from shared.git_sandbox import commit_all, init_repo, run_git
from test_cache import FINGERPRINT_KIND_TREE, lookup

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _hermetic_git_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop an inherited ``GIT_INDEX_FILE`` for a fixture-repo test.

    The finish gate exports a *temporary* index for the whole suite
    (SA-0MVQHVGFR006W8LG): every ``git`` command the suite spawns then reads
    that index, which is how ``git ls-files`` guards observe the
    would-be-commit tree. ``GIT_INDEX_FILE`` deliberately survives the test
    skill's repository-override scrub for exactly that reason
    (SA-0MVQHV9Y3001RNNW).

    A test that builds its **own** repository must therefore neutralise it
    (``shared.git_sandbox.fixture_git_env()`` documents the rule). Here it is
    not only the fixture's own ``git`` calls that matter: the production
    helpers under test (``compute_changed_files``, ``changed_scope_commands``,
    ``run_suite``) spawn ``git`` themselves and inherit the ambient
    environment. With a foreign index in ``GIT_INDEX_FILE`` — an index from
    the gate's repository, which lists none of the fixture's paths —
    ``git diff <base>`` reports **every** file in the fixture as changed, so
    the baseline assertions below fail only when the suite runs under the
    gate.
    """
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)


# ``monkeypatch.delenv`` is undone by pytest at test teardown, so the
# neutralisation never leaks into another test.
@pytest.fixture(autouse=True)
def _gate_index_neutralised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test in this module hermetic under the finish gate."""
    _hermetic_git_index(monkeypatch)

def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with test and source files."""
    repo = init_repo(tmp_path / "test_repo")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()

    # Source files
    (repo / "src" / "utils.py").write_text("# utils\n")
    (repo / "src" / "foo.py").write_text("from .utils import helper\n")
    (repo / "src" / "bar.py").write_text("# bar\n")

    # Test files — naming follows convention
    (repo / "tests" / "test_utils.py").write_text("import src.utils\n")
    (repo / "tests" / "test_foo.py").write_text("import src.foo\n")
    (repo / "tests" / "test_bar.py").write_text("import src.bar\n")

    # Initial commit (identity + hermetic environment come from the sandbox)
    commit_all(repo, "initial")

    # Second commit on dev so HEAD~1 always exists for fallback tests
    (repo / "README.md").write_text("# test repo\n")
    commit_all(repo, "docs: add readme")

    # Create a remote-like branch for merge-base resolution
    run_git(repo, ["branch", "origin/dev"], check=True)

    return repo


def _staged_foreign_index(tmp_path: Path) -> Path:
    """An index belonging to a *different* repository with a staged change.

    Mirrors what the finish gate hands the suite it runs: a temporary index
    that names another repository's files (SA-0MVQHVGFR006W8LG).
    """
    gate_repo = init_repo(tmp_path / "gate_repo", default_branch="dev")
    (gate_repo / "gate_only.py").write_text("x = 1\n")
    commit_all(gate_repo, "initial")
    (gate_repo / "gate_only.py").write_text("x = 2\n")
    index = tmp_path / "gate.index"
    run_git(
        gate_repo,
        ["add", "-A"],
        env={"GIT_INDEX_FILE": str(index)},
        check=True,
    )
    return index


def _init_repo_with_dev(repo: Path, *, message: str = "initial") -> Path:
    """Create a hermetic repo at *repo* on branch ``dev`` with one commit.

    Every git invocation is routed through the shared sandbox helper so a
    leaked repository-overriding environment variable (``GIT_DIR``,
    ``GIT_WORK_TREE``, …) cannot redirect the fixture away from *repo*
    (SA-0MUG0WFP8008WN63 — parent AC1).
    """
    init_repo(repo, default_branch="dev")
    commit_all(repo, message)
    return repo


def _make_vitest_repo(tmp_path: Path, config: dict | None = None) -> Path:
    """Create a hermetic vitest/TypeScript repo on branch ``dev``.

    Carries ``tests/{cli,unit}/*.test.ts`` files, a co-located
    ``tests/unit/utils.test.ts`` for ``src/utils.ts`` and a package.json that
    declares vitest. An optional ``.pi/test-config.json`` *config* is written
    before the initial commit so it is part of the repo baseline.
    """
    repo = tmp_path / "vitest_repo"
    (repo / "tests" / "cli").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": "vitest run"},
                "devDependencies": {"vitest": "^1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (repo / "src" / "utils.ts").write_text("export const x = 1;\n")
    (repo / "tests" / "unit" / "utils.test.ts").write_text(
        "import { test } from 'vitest';\n"
    )
    (repo / "tests" / "cli" / "foo.test.ts").write_text(
        "import { test } from 'vitest';\n"
    )
    (repo / "tests" / "cli" / "foo.spec.ts").write_text(
        "import { test } from 'vitest';\n"
    )
    if config is not None:
        (repo / ".pi").mkdir()
        (repo / ".pi" / "test-config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
    _init_repo_with_dev(repo)
    return repo


class _CompletedProcess:
    """Minimal mock of subprocess.CompletedProcess."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# compute_changed_files tests
# ---------------------------------------------------------------------------


class TestComputeChangedFiles:
    """Tests for compute_changed_files() — merge-base resolution and fallback."""

    def test_resolves_from_origin_dev(self, tmp_path: Path) -> None:
        """Uses origin/dev as the base for git diff."""
        repo = _make_repo(tmp_path)

        # Make a change after the initial commit
        (repo / "src" / "utils.py").write_text("# utils — modified\n")
        (repo / "src" / "foo.py").write_text("# foo — modified\n")

        result = compute_changed_files(repo, base_ref="origin/dev")
        assert set(result) == {"src/utils.py", "src/foo.py"}

    def test_falls_back_to_local_dev(self, tmp_path: Path) -> None:
        """When origin/dev is absent, falls back to local dev."""
        repo = _make_repo(tmp_path)

        # Remove origin/dev
        run_git(repo, ["branch", "-D", "origin/dev"], check=True)

        (repo / "src" / "bar.py").write_text("# bar — modified\n")

        result = compute_changed_files(repo, base_ref="origin/dev")
        assert "src/bar.py" in result

    def test_falls_back_to_head_parent(self, tmp_path: Path) -> None:
        """When both remotes and local are absent, falls back to HEAD~1."""
        repo = _make_repo(tmp_path)

        (repo / "src" / "utils.py").write_text("# utils — modified\n")

        # Move to a feature branch so we can delete the dev branch
        run_git(repo, ["checkout", "-b", "feature"], check=True)
        run_git(repo, ["branch", "-D", "origin/dev"], check=True)
        # Delete local dev branch; HEAD~1 is now the only base available
        run_git(repo, ["branch", "-D", "dev"], check=True)

        result = compute_changed_files(repo, base_ref="origin/dev")
        assert "src/utils.py" in result

    def test_no_changes(self, tmp_path: Path) -> None:
        """Returns empty set when no files changed."""
        repo = _make_repo(tmp_path)
        result = compute_changed_files(repo, base_ref="dev")
        assert result == set()

    def test_changed_files_are_hermetic_under_the_finish_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A foreign ``GIT_INDEX_FILE`` (the finish gate's staged index) must
        not turn a fixture repo's own clean history into "everything changed".

        The gate exports its temporary index for the whole suite, so the
        ambient ``GIT_INDEX_FILE`` points at a file listing none of this
        fixture's paths. Changed-file detection must still read the fixture's
        own working tree: the only change is the one this test makes.
        """
        repo = _make_repo(tmp_path)
        (repo / "src" / "utils.py").write_text("# utils — modified\n")

        foreign_index = tmp_path / "gate.index"
        shutil.copyfile(repo / ".git" / "index", foreign_index)
        monkeypatch.setenv("GIT_INDEX_FILE", str(foreign_index))
        # The module-level hermetic fixture is what neutralises the gate's
        # exported index before any production helper spawns git.
        _hermetic_git_index(monkeypatch)

        assert compute_changed_files(repo, base_ref="origin/dev") == {
            "src/utils.py"
        }

    def test_includes_untracked_files(self, tmp_path: Path) -> None:
        """New (untracked) files are part of the working-tree change set.

        During the implement flow tests run before the first commit, so a
        newly created test file has no diff entry — it must still be
        selectable (SA-0MVL7YOMJ006UMDO).
        """
        repo = _make_repo(tmp_path)
        (repo / "src" / "utils.py").write_text("# utils — modified\n")
        (repo / "src" / "untracked.py").write_text("# new file\n")

        result = compute_changed_files(repo, base_ref="dev")
        assert "src/untracked.py" in result
        assert "src/utils.py" in result

    def test_fixture_repo_ignores_an_inherited_index_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A foreign ``GIT_INDEX_FILE`` never leaks into a fixture repo's diff.

        The finish gate exports a temporary would-be-commit index for the
        suite it runs (SA-0MVQHVGFR006W8LG). A fixture repository that
        inherits it reports every tracked file as changed (the foreign index
        has no stat entries for its paths) and lets git refresh — write — an
        index that is not its own, which invalidates the gate's evidence for
        the tree it tested (SA-0MVTET8KQ0031R6Z).
        """
        foreign_index = _staged_foreign_index(tmp_path)
        before = foreign_index.read_bytes()
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("GIT_INDEX_FILE", str(foreign_index))

        result = compute_changed_files(repo, base_ref="dev")

        assert result == set(), (
            "the foreign gate index leaked into the fixture's changed-file set: "
            f"{sorted(result)}"
        )
        assert foreign_index.read_bytes() == before, (
            "compute_changed_files refreshed (wrote) the inherited gate index"
        )


# ---------------------------------------------------------------------------
# map_changed_to_tests tests
# ---------------------------------------------------------------------------


class TestMapChangedToTests:
    """Tests for map_changed_to_tests() — convention + import-graph mapping."""

    def test_heuristic_mapping_by_convention(self, tmp_path: Path) -> None:
        """src/foo.py → tests/test_foo.py by naming convention."""
        repo = _make_repo(tmp_path)
        changed = {"src/foo.py"}
        tests = map_changed_to_tests(repo, changed)
        assert "tests/test_foo.py" in tests

    def test_heuristic_mapping_utils(self, tmp_path: Path) -> None:
        """src/utils.py → tests/test_utils.py by naming convention."""
        repo = _make_repo(tmp_path)
        changed = {"src/utils.py"}
        tests = map_changed_to_tests(repo, changed)
        assert "tests/test_utils.py" in tests

    def test_import_graph_expansion(self, tmp_path: Path) -> None:
        """Changed src/utils.py should also find test_foo.py which imports src.foo (which imports utils)."""
        repo = _make_repo(tmp_path)

        # src/foo.py imports src.utils — so changing utils should trigger test_foo.py too
        changed = {"src/utils.py"}
        tests = map_changed_to_tests(repo, changed)
        # test_foo.py imports src.foo which imports src.utils
        assert "tests/test_foo.py" in tests
        assert "tests/test_utils.py" in tests

    def test_import_graph_expansion_src_layout(self, tmp_path: Path) -> None:
        """src/ layout: tests import top-level names; changed src/utils.py still
        triggers test_foo.py via the foo → utils import chain."""
        repo = tmp_path / "src_layout_repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "tests").mkdir()

        # src-layout: src/ is a source root; tests import top-level names
        (repo / "src" / "utils.py").write_text("def helper(): return 1\n")
        (repo / "src" / "foo.py").write_text("from utils import helper\n")
        (repo / "tests" / "test_utils.py").write_text("from utils import helper\n")
        (repo / "tests" / "test_foo.py").write_text("import foo\n")
        (repo / "tests" / "test_bar.py").write_text("import bar2\n")

        _init_repo_with_dev(repo)

        changed = {"src/utils.py"}
        tests = map_changed_to_tests(repo, changed)
        assert "tests/test_utils.py" in tests
        # test_foo imports foo (src/foo.py) which imports utils — caught via
        # basename aliasing
        assert "tests/test_foo.py" in tests
        assert "tests/test_bar.py" not in tests

    def test_no_false_positives(self, tmp_path: Path) -> None:
        """Changing bar.py should not trigger unrelated tests."""
        repo = _make_repo(tmp_path)
        changed = {"src/bar.py"}
        tests = map_changed_to_tests(repo, changed)
        assert "tests/test_bar.py" in tests
        # bar doesn't import from utils, so test_utils shouldn't be triggered
        assert "tests/test_utils.py" not in tests
        assert "tests/test_foo.py" not in tests

    def test_test_file_changes_included(self, tmp_path: Path) -> None:
        """Changing a test file directly includes it."""
        repo = _make_repo(tmp_path)
        (repo / "tests" / "test_utils.py").write_text("# modified test\n")
        changed = {"tests/test_utils.py"}
        tests = map_changed_to_tests(repo, changed)
        assert "tests/test_utils.py" in tests

    def test_non_python_files_ignored(self, tmp_path: Path) -> None:
        """Non-.py files don't trigger test selection."""
        repo = _make_repo(tmp_path)
        (repo / "src" / "utils.py").write_text("# modified\n")
        changed = {"README.md", "src/config.yaml"}
        tests = map_changed_to_tests(repo, changed)
        # Only .py changes should produce test selections
        # README.md and config.yaml are not .py, so no tests should be selected
        assert tests == set()


# ---------------------------------------------------------------------------
# changed_scope_commands tests
# ---------------------------------------------------------------------------


class TestChangedScopeCommands:
    """Tests for changed_scope_commands() integration."""

    def test_returns_commands_for_pytest_repo(self, tmp_path: Path) -> None:
        """A pytest repo returns partial pytest command with selected files."""
        repo = _make_repo(tmp_path)

        # Add a pytest config marker
        (repo / "pytest.ini").write_text("[pytest]\n")

        (repo / "src" / "utils.py").write_text("# modified\n")

        cmds = changed_scope_commands(repo, base_ref="dev")
        assert cmds is not None
        assert len(cmds) == 1
        # The command should be a pytest command with specific test files
        cmd = cmds[0]
        assert "pytest" in cmd.lower()
        assert "tests/test_utils.py" in cmd

    def test_suite_commands_do_not_disable_file_selection(self, tmp_path: Path) -> None:
        """Declared suiteCommands define the full suite only — changed scope
        still selects the affected test file (SA-0MVL7YOMJ006UMDO)."""
        repo = _make_repo(tmp_path)
        (repo / ".pi").mkdir()
        (repo / ".pi" / "test-config.json").write_text(
            json.dumps({"suiteCommands": ["npm test"]})
        )

        (repo / "src" / "utils.py").write_text("# modified\n")

        cmds = changed_scope_commands(repo, base_ref="dev")
        assert cmds is not None
        assert "tests/test_utils.py" in cmds[0]

    def test_returns_none_for_no_pytest_repo(self, tmp_path: Path) -> None:
        """A repo with no pytest config returns None (full scope fallback)."""
        repo = _make_repo(tmp_path)
        # No pytest config, no node dirs, no package.json → full scope

        cmds = changed_scope_commands(repo, base_ref="dev")
        assert cmds is None

    def test_node_scope_selects_mjs_tests(self, tmp_path: Path) -> None:
        """A changed .mjs source selects its node test files explicitly."""
        repo = tmp_path / "node_repo"
        repo.mkdir()
        (repo / "tests" / "node").mkdir(parents=True)
        (repo / "src").mkdir()

        (repo / "src" / "utils.mjs").write_text("export const helper = 1;\n")
        (repo / "tests" / "node" / "utils.test.mjs").write_text('import test from "node:test";')
        (repo / "tests" / "node" / "other.test.mjs").write_text('import test from "node:test";')

        _init_repo_with_dev(repo)

        (repo / "src" / "utils.mjs").write_text("export const helper = 2;\n")

        cmds = changed_scope_commands(repo, base_ref="dev")
        assert cmds is not None
        # Only the affected node test is selected
        assert any("utils.test.mjs" in c for c in cmds)
        assert all("other.test.mjs" not in c for c in cmds)

    # -- F0 baseline (SA-0MVQQ9HXV000V6Z4 AC3) -----------------------------
    # Pins today's changed-scope selection before the hidden skill-test rule
    # (SA-0MVQJSGLY0098DVB) lands. That item's D5 keeps changed-scope
    # selection untouched: hidden project-local skill tests are enforced at
    # full scope only. These assertions must stay green after F1–F3.

    def test_skill_test_file_change_selects_the_test_group(self) -> None:
        """A changed *test* file under ``skill/test/`` matches this repo's
        declared ``test`` group, so the group's command is selected.

        Declared ``groups`` take precedence over file-level convention
        mapping for a repo that declares them (SA-0MVL7YOMJ006UMDO), so the
        selection is the group command rather than a single-file command.
        """
        cmds = changed_scope_commands(
            _REPO_ROOT,
            changed_files={"skill/test/tests/test_suite_resolution.py"},
        )
        assert cmds == ["pytest -q -r a --disable-warnings skill/test/tests/"]

    def test_skill_script_change_selects_the_test_group(self) -> None:
        """A non-test change under ``skill/test/`` matches the declared
        ``test`` group's ``paths``, so the group command runs instead of the
        full suite (SA-0MVL7YOMJ006UMDO).

        Before group honouring this change set selected nothing (skill
        sources are not test files) and fell back to the full suite.
        """
        cmds = changed_scope_commands(
            _REPO_ROOT,
            changed_files={"skill/test/scripts/run_tests.py"},
        )
        assert cmds == ["pytest -q -r a --disable-warnings skill/test/tests/"]

    def test_hidden_skill_test_file_change_selects_that_file(
        self, tmp_path: Path,
    ) -> None:
        """A changed test file under ``.pi/skills/<name>/tests`` is selected
        by changed scope today (it is itself a test file). The new full-suite
        rule adds a command at full scope only and must not alter this."""
        repo = tmp_path / "hidden_skill_repo"
        repo.mkdir()
        (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        hidden_tests = repo / ".pi" / "skills" / "demo" / "tests"
        hidden_tests.mkdir(parents=True)
        (hidden_tests / "test_demo.py").write_text(
            "def test_demo() -> None:\n    assert True\n", encoding="utf-8",
        )

        cmds = changed_scope_commands(
            repo, changed_files={".pi/skills/demo/tests/test_demo.py"},
        )
        assert cmds == [
            "pytest -q -r a --disable-warnings .pi/skills/demo/tests/test_demo.py"
        ]

    # -- AC7 regression (SA-0MVQQ9QJI002K7DY) ------------------------------
    # D5: changed-scope selection is untouched by the hidden skill-test rule
    # (SA-0MVQJSGLY0098DVB) — the extra command is enforced at FULL scope
    # only, never added to a changed-scope subset.

    def test_changed_scope_is_identical_with_and_without_candidates(
        self, tmp_path: Path,
    ) -> None:
        """A representative change resolves to the same subset command whether
        or not the repo ships hidden skill-test candidates — the extra
        command belongs to the full suite only (AC7)."""
        def _build(hidden: bool) -> Path:
            repo = tmp_path / ("with_candidates" if hidden else "no_candidates")
            repo.mkdir()
            (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "src").mkdir()
            (repo / "tests" / "test_utils.py").write_text(
                "def test_utils() -> None:\n    assert True\n", encoding="utf-8",
            )
            (repo / "src" / "utils.py").write_text("x = 1\n", encoding="utf-8")
            if hidden:
                hidden_tests = repo / ".pi" / "skills" / "alpha" / "tests"
                hidden_tests.mkdir(parents=True)
                (hidden_tests / "test_alpha.py").write_text(
                    "def test_alpha() -> None:\n    assert True\n",
                    encoding="utf-8",
                )
            _init_repo_with_dev(repo)
            return repo

        # Representative change: a source file whose convention test exists.
        with_repo = _build(hidden=True)
        without_repo = _build(hidden=False)
        changed = {"src/utils.py"}

        with_cmds = changed_scope_commands(with_repo, changed_files=changed)
        without_cmds = changed_scope_commands(without_repo, changed_files=changed)

        # Identical subset commands: the candidate dirs never leak in.
        assert with_cmds == without_cmds
        assert with_cmds == [
            "pytest -q -r a --disable-warnings tests/test_utils.py"
        ]


# ---------------------------------------------------------------------------
# TypeScript/vitest changed-scope selection (SA-0MVL7YOMJ006UMDO)
# ---------------------------------------------------------------------------


class TestTypeScriptChangedScope:
    """Vitest ``*.test.ts`` / ``*.spec.ts`` files are selectable."""

    def test_changed_test_ts_selected(self, tmp_path: Path) -> None:
        """A changed vitest .test.ts file is selected and runs via vitest."""
        repo = _make_vitest_repo(tmp_path)

        cmds = changed_scope_commands(
            repo, base_ref="dev", changed_files={"tests/cli/foo.test.ts"}
        )

        assert cmds is not None
        assert len(cmds) == 1
        assert "vitest run" in cmds[0]
        assert "tests/cli/foo.test.ts" in cmds[0]

    def test_changed_spec_ts_selected(self, tmp_path: Path) -> None:
        """A changed vitest .spec.ts file is selected conservatively too."""
        repo = _make_vitest_repo(tmp_path)

        cmds = changed_scope_commands(
            repo, base_ref="dev", changed_files={"tests/cli/foo.spec.ts"}
        )

        assert cmds is not None
        assert "vitest run" in cmds[0]
        assert "tests/cli/foo.spec.ts" in cmds[0]

    def test_changed_source_ts_selects_stem_test(self, tmp_path: Path) -> None:
        """src/utils.ts maps to tests/unit/utils.test.ts by stem convention."""
        repo = _make_vitest_repo(tmp_path)

        cmds = changed_scope_commands(
            repo, base_ref="dev", changed_files={"src/utils.ts"}
        )

        assert cmds is not None
        assert "vitest run" in cmds[0]
        assert "tests/unit/utils.test.ts" in cmds[0]

    def test_new_untracked_test_ts_is_computed(self, tmp_path: Path) -> None:
        """A newly created (untracked) test file is part of the change set."""
        repo = _make_vitest_repo(tmp_path)
        (repo / "tests" / "cli" / "new.test.ts").write_text(
            "import { test } from 'vitest';\n"
        )

        assert "tests/cli/new.test.ts" in compute_changed_files(repo, base_ref="dev")

        cmds = changed_scope_commands(repo, base_ref="dev")
        assert cmds is not None
        assert "tests/cli/new.test.ts" in cmds[0]

    def test_ts_without_vitest_reports_no_tooling(self, tmp_path: Path) -> None:
        """Without a known vitest runner the fallback names the reason."""
        repo = tmp_path / "plain_ts_repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "foo.test.ts").write_text("// test\n")
        _init_repo_with_dev(repo)

        selection = changed_scope_selection(
            repo, base_ref="dev", changed_files={"tests/foo.test.ts"}
        )

        assert selection.commands is None
        assert selection.reason == CHANGED_SCOPE_NO_TOOLING


# ---------------------------------------------------------------------------
# Named groups in changed-scope selection (SA-0MVL7YOMJ006UMDO)
# ---------------------------------------------------------------------------

_GROUP_CONFIG = {
    "suiteCommands": ["npm --silent test"],
    "groups": {
        "cli": {
            "commands": ["npm --silent test -- tests/cli"],
            "paths": ["tests/cli/**"],
        },
        "unit": {
            "commands": ["npm --silent test -- tests/unit"],
            "paths": ["tests/unit/**"],
        },
    },
}


#: Groups shaped like this repository's own ``implement`` group: a changed
#: source under the group's path must still select a changed test file that
#: lives outside it (SA-0MWA7OD8O001945I).
_PYTEST_GROUP_CONFIG = {
    "groups": {
        "implement": {
            "commands": ["pytest -q -r a --disable-warnings skill/implement/tests/"],
            "paths": ["skill/implement/**"],
        },
    },
}


def _make_pytest_group_repo(tmp_path: Path, config: dict | None = None) -> Path:
    """Create a hermetic pytest repo whose ``implement`` group maps to a path.

    Layout mirrors the observed bug: the group covers ``skill/implement/**``
    while the edit-authored test file lives in the root ``tests/`` tree that
    the group's command never collects.
    """
    repo = tmp_path / "pytest_group_repo"
    (repo / "skill" / "implement" / "scripts").mkdir(parents=True)
    (repo / "skill" / "implement" / "tests").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n")
    (repo / "skill" / "implement" / "scripts" / "implement.py").write_text(
        "x = 1\n"
    )
    (repo / "skill" / "implement" / "tests" / "test_implement.py").write_text(
        "\n"
    )
    (repo / "tests" / "test_implement_drive.py").write_text("\n")
    (repo / ".pi").mkdir()
    (repo / ".pi" / "test-config.json").write_text(
        json.dumps(config if config is not None else _PYTEST_GROUP_CONFIG),
        encoding="utf-8",
    )
    _init_repo_with_dev(repo)
    return repo


#: Only **one** group matches here on purpose: group tie-breaking between two
#: equally-matching groups is set-iteration order until
#: SA-0MVEJIHIG003WTH9 makes it deterministic, so a union test that relied on
#: a tie would flake under a different ``PYTHONHASHSEED``.
_UNION_GROUP_CONFIG = {
    "groups": {
        "cli": {
            "commands": ["npm --silent test -- tests/cli"],
            "paths": ["tests/cli/**"],
        },
    },
}


class TestChangedScopeGroups:
    """Declared groups are honoured even when suiteCommands is present."""

    def test_group_match_unions_changed_test_file_outside_group(
        self, tmp_path: Path
    ) -> None:
        """AC2: a group match unions the changed test file it does not cover.

        ``tests/cli/foo.test.ts`` matches the ``cli`` group; the changed
        ``tests/unit/utils.test.ts`` lies outside that group's ``paths`` and
        must be selected on its own merits, never dropped
        (SA-0MWA7OD8O001945I).
        """
        repo = _make_vitest_repo(tmp_path, config=_UNION_GROUP_CONFIG)

        selection = changed_scope_selection(
            repo,
            base_ref="dev",
            changed_files={"tests/cli/foo.test.ts", "tests/unit/utils.test.ts"},
        )

        assert selection.commands == [
            "npm --silent test -- tests/cli",
            "npx --no-install vitest run tests/unit/utils.test.ts",
        ], selection.commands
        assert selection.group == "cli"

    def test_group_match_unions_changed_pytest_file_outside_group(
        self, tmp_path: Path
    ) -> None:
        """AC2: the observed pytest case — group command + outside test file.

        Reproduces SA-0MWA7OD8O001945I: ``skill/implement/scripts/implement.py``
        matches the ``implement`` group, and the edit-authored
        ``tests/test_implement_drive.py`` must still be executed.
        """
        repo = _make_pytest_group_repo(tmp_path)

        selection = changed_scope_selection(
            repo,
            base_ref="dev",
            changed_files={
                "skill/implement/scripts/implement.py",
                "tests/test_implement_drive.py",
            },
        )

        assert selection.commands == [
            "pytest -q -r a --disable-warnings skill/implement/tests/",
            "pytest -q -r a --disable-warnings tests/test_implement_drive.py",
        ], selection.commands
        assert selection.group == "implement"

    def test_group_only_match_emits_exactly_the_group_command(
        self, tmp_path: Path
    ) -> None:
        """AC3: a group-only match is unchanged — the group command alone.

        The changed file outside the group is a document, not a test, so
        nothing is unioned and the single-match behaviour is byte-identical.
        """
        repo = _make_vitest_repo(tmp_path, config=_GROUP_CONFIG)

        selection = changed_scope_selection(
            repo, base_ref="dev", changed_files={"tests/cli/foo.test.ts", "README.md"}
        )

        assert selection.commands == ["npm --silent test -- tests/cli"]
        assert selection.group == "cli"

    def test_group_match_fails_closed_when_outside_change_has_no_selection(
        self, tmp_path: Path
    ) -> None:
        """AC1: never silently drop — an unselectable outside change fails closed.

        ``src/unrelated.ts`` matches no group, is not a test file and maps to
        no test, so the run cannot prove coverage: the selection returns no
        commands (the consumer then runs the full suite) rather than a group
        command that ignores the change.
        """
        repo = _make_vitest_repo(tmp_path, config=_GROUP_CONFIG)

        selection = changed_scope_selection(
            repo,
            base_ref="dev",
            changed_files={"tests/cli/foo.test.ts", "src/unrelated.ts"},
        )

        assert selection.commands is None
        assert selection.reason == CHANGED_SCOPE_GROUP_INCOMPLETE

    def test_group_union_reaches_the_run_suite_command_line(
        self, tmp_path: Path
    ) -> None:
        """AC4: ``--scope changed`` on the observed change set runs the file.

        End-to-end through :func:`run_suite`: the changed-file set is computed
        from git (``skill/implement/scripts/implement.py`` +
        ``tests/test_implement_drive.py``) exactly as the CLI does.
        """
        repo = _make_pytest_group_repo(tmp_path)
        (repo / "skill" / "implement" / "scripts" / "implement.py").write_text(
            "x = 2\n"
        )
        (repo / "tests" / "test_implement_drive.py").write_text("# changed\n")

        captured: list[str] = []

        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite(
                "all",
                cwd=repo,
                scope="changed",
                base_ref="dev",
                use_cache=False,
                force=True,
            )

        assert result["scope"] == "changed"
        joined = " | ".join(captured)
        assert "skill/implement/tests/" in joined, captured
        assert "tests/test_implement_drive.py" in joined, captured

    def test_suite_commands_present_with_matching_group(self, tmp_path: Path) -> None:
        """A matching group's commands are selected despite suiteCommands."""
        repo = _make_vitest_repo(tmp_path, config=_GROUP_CONFIG)

        selection = changed_scope_selection(
            repo, base_ref="dev", changed_files={"tests/cli/foo.test.ts"}
        )

        assert selection.commands == ["npm --silent test -- tests/cli"]
        assert selection.group == "cli"
        assert "cli" in selection.reason

    def test_suite_commands_present_no_group_reports_group_reason(
        self, tmp_path: Path
    ) -> None:
        """No group and no convention match → the named group fallback reason."""
        repo = _make_vitest_repo(tmp_path, config=_GROUP_CONFIG)

        selection = changed_scope_selection(
            repo, base_ref="dev", changed_files={"src/unrelated.ts"}
        )

        assert selection.commands is None
        assert selection.reason == CHANGED_SCOPE_NO_GROUP

    def test_docs_only_change_reports_no_source_reason(self, tmp_path: Path) -> None:
        """A docs-only change names the no-source reason, not a generic list."""
        repo = _make_vitest_repo(tmp_path, config=_GROUP_CONFIG)

        selection = changed_scope_selection(
            repo, base_ref="dev", changed_files={"docs/guide.md"}
        )

        assert selection.commands is None
        assert selection.reason == CHANGED_SCOPE_NO_SOURCE

    def test_no_changed_files_reports_no_changes_reason(
        self, tmp_path: Path
    ) -> None:
        repo = _make_vitest_repo(tmp_path, config=_GROUP_CONFIG)

        selection = changed_scope_selection(
            repo, base_ref="dev", changed_files=set()
        )

        assert selection.commands is None
        assert selection.reason == CHANGED_SCOPE_NO_CHANGES

    def test_run_suite_reports_changed_scope_for_group(self, tmp_path: Path) -> None:
        """run_suite reports scope 'changed' with the selected group commands."""
        from types import SimpleNamespace

        repo = _make_vitest_repo(tmp_path, config=_GROUP_CONFIG)
        (repo / "tests" / "cli" / "foo.test.ts").write_text(
            "import { test } from 'vitest';\n// changed\n"
        )

        captured: list[str] = []

        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite(
                "all",
                cwd=repo,
                scope="changed",
                base_ref="dev",
                use_cache=False,
                force=True,
            )

        assert result["scope"] == "changed"
        assert captured == ["npm --silent test -- tests/cli"], captured


# ---------------------------------------------------------------------------
# run_suite / run_all scope propagation tests
# ---------------------------------------------------------------------------


class TestPerSuiteChangedScope:
    """Tests for scope='changed' with specific suite names (not 'all')."""

    def test_pytest_scope_filters_to_pytest_only(self, tmp_path: Path) -> None:
        """--suite pytest --scope changed runs pytest only, not node tests."""
        from types import SimpleNamespace

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "tests").mkdir()
        (repo / "tests" / "node").mkdir()
        (repo / "src").mkdir()
        (repo / "pytest.ini").write_text("[pytest]\n")
        (repo / "tests" / "test_foo.py").write_text("\n")
        (repo / "tests" / "node" / "utils.test.mjs").write_text("import test from \"node:test\";\n")
        (repo / "src" / "foo.py").write_text("from src import utils\n")
        _init_repo_with_dev(repo, message="init")
        (repo / "src" / "foo.py").write_text("from src import utils\nchanged\n")

        captured: list[str] = []
        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        # Bypass the shared concurrency semaphore so the test is not
        # flaky when the host-wide TEST_MAX_CONCURRENCY slot is saturated
        # (SA-0MUF9TXFN009I6Q2).
        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite(
                "pytest",
                cwd=repo,
                scope="changed",
                base_ref="dev",
            )
        assert result["scope"] == "changed"
        assert any("pytest" in c for c in captured)
        assert not any("node" in c for c in captured)

    def test_changed_scope_no_selection_falls_back_to_full(self, tmp_path: Path) -> None:
        """When changed scope produces no tests, fall back to full pytest command."""
        from types import SimpleNamespace

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "tests").mkdir()
        (repo / "src").mkdir()
        (repo / "pytest.ini").write_text("[pytest]\n")
        (repo / "tests" / "test_foo.py").write_text("\n")
        _init_repo_with_dev(repo, message="init")
        # change only a doc — no test files selectable
        (repo / "README.md").write_text("# changed\n")

        captured: list[str] = []
        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        # Bypass the shared "test" semaphore: a saturated host must not turn
        # this unit test into a TestConcurrencyTimeout flake
        # (SA-0MUF9TXFN009I6Q2, SA-0MUGXZGVZ001UFPG).
        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite(
                "pytest",
                cwd=repo,
                scope="changed",
                base_ref="dev",
            )
        assert result["scope"] == "full"
        assert any("pytest" in c for c in captured)

    def test_scope_passed_to_run_cached(self, tmp_path: Path) -> None:
        """run_suite passes the resolved scope to run_cached."""
        from types import SimpleNamespace

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "tests").mkdir()
        (repo / "src").mkdir()
        (repo / "pytest.ini").write_text("[pytest]\n")
        (repo / "tests" / "test_utils.py").write_text("\n")
        (repo / "src" / "utils.py").write_text("x = 1\n")
        _init_repo_with_dev(repo, message="init")
        (repo / "src" / "utils.py").write_text("x = 2\n")

        def fake_cached(command, cwd, **kwargs):
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        # Patch run_cached (called by run_suite through use_cache) to capture scope
        def mock_run_cached(command, *, scope="full", **kwargs):
            cached["scope"] = scope
            return {
                "stdout": "passed",
                "stderr": "",
                "exit_code": 0,
                "cached": False,
                "scope": scope,
            }
        cached: dict[str, str] = {}
        with mock.patch("run_tests.run_cached", side_effect=mock_run_cached):
            result = run_suite("pytest", cwd=repo, use_cache=True, force=True, scope="changed", base_ref="dev")
        assert cached["scope"] == "changed"
        assert result["scope"] == "changed"


# Pinned pytest command used by the explicit-suite fallback regression tests.
_PINNED_PYTEST_COMMANDS: list[str] = [
    "python3 -m pytest -q -r a --disable-warnings",
]
# The pinned flags, without the interpreter prefix: ``_run_cmd`` receives argv
# after ``executable_test_command()`` resolves the pytest executable prefix to
# an absolute interpreter path (SA-0MVK154IE007OS3D), so execution-level
# assertions compare the flag suffix. The *command source* discrimination
# (pin vs legacy token) is asserted separately via ``result["command"]`` —
# see ``_assert_command_source``.
_PINNED_PYTEST_FLAGS = "-m pytest -q -r a --disable-warnings"


def _assert_pinned_pytest_only(captured: list[str]) -> None:
    """Assert exactly one executed command, and that it is the pinned form."""
    assert len(captured) == 1, captured
    assert captured[0].endswith(_PINNED_PYTEST_FLAGS), captured
    # Never the legacy bare ``pytest`` token (the bug being fixed).
    assert not captured[0].startswith("pytest "), captured


def _assert_command_source(result: dict, expected: str) -> None:
    """Assert the *selected* command, before execution-time resolution.

    ``run_suite`` returns ``command`` as the raw selected command (the pin
    verbatim), while ``_run_cmd`` receives argv after
    ``executable_test_command()`` rewrites the pytest prefix to the probed
    interpreter (SA-0MVK154IE007OS3D). Because that rewrite makes the legacy
    bare ``pytest`` token and a ``python3 -m pytest`` pin look identical at
    execution time, the executed argv alone cannot prove *which* command
    source won. ``command`` is therefore the discriminating assertion: it is
    the pinned string when the fallback resolves through
    ``full_suite_commands()``, and the legacy ``pytest`` token when it does
    not.
    """
    assert result["command"] == expected, (
        f"expected the pinned command source {expected!r}, "
        f"got {result['command']!r}"
    )


class TestExplicitSuiteFallbackHonoursPins:
    """Changed-scope fallback for an explicit suite honours pinned suiteCommands.

    Regression tests for the cross-repo interpreter-pin bug
    (OSL-0MVNWYRQB0095WXQ / SA-0MVO0EB1U007L7DS): when changed-scope
    selection falls back to full scope for an explicit suite name, the
    fallback must resolve through ``full_suite_commands(cwd)`` filtered by
    suite name — never the legacy bare ``pytest`` token — so a repo's pinned
    ``suiteCommands`` (custom interpreter) are honoured for every suite name.
    """

    @staticmethod
    def _pin_repo_with_change(tmp_path: Path, suite_commands: list[str]) -> Path:
        """A hermetic repo on ``dev`` with a ``.pi/test-config.json`` pin and a
        subsequent change (so changed scope has something to fall back from)."""
        repo = tmp_path / "pinned_repo"
        repo.mkdir()
        (repo / ".pi").mkdir()
        (repo / ".pi" / "test-config.json").write_text(
            json.dumps({"suiteCommands": suite_commands}), encoding="utf-8"
        )
        (repo / "tests").mkdir()
        (repo / "src").mkdir()
        (repo / "pytest.ini").write_text("[pytest]\n")
        (repo / "tests" / "test_foo.py").write_text("def test_foo(): pass\n")
        _init_repo_with_dev(repo, message="init")
        # A docs-only change maps to no test, so changed scope must fall back
        # to the full suite — the fallback whose pinned-command handling these
        # tests cover. (Source changes are now selectable, SA-0MVL7YOMJ006UMDO.)
        (repo / "README.md").write_text("# changed\n")
        return repo

    def test_changed_scope_fallback_pytest_uses_pinned_command(self, tmp_path: Path) -> None:
        """AC1/AC2: --suite pytest --scope changed runs the pinned command, never
        the bare ``pytest`` token; the result still reports scope "full"."""
        from types import SimpleNamespace

        repo = self._pin_repo_with_change(tmp_path, _PINNED_PYTEST_COMMANDS)

        captured: list[str] = []

        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite(
                "pytest", cwd=repo, scope="changed", base_ref="dev",
            )

        # Fallback reports full scope, as before.
        assert result["scope"] == "full"
        # The pinned command is the selected source (AC1/AC2) — the legacy
        # bare ``pytest`` token is never substituted.
        _assert_command_source(result, _PINNED_PYTEST_COMMANDS[0])
        # The pinned form runs — never the legacy bare ``pytest`` token.
        _assert_pinned_pytest_only(captured)

    def test_changed_scope_fallback_multi_command_pin_maps_suites(
        self, tmp_path: Path,
    ) -> None:
        """AC5: a multi-command pin maps each command to its own suite name."""
        from types import SimpleNamespace

        pin = [
            "python3 -m pytest -q -r a --disable-warnings",
            'node --test "tests/node/**/*.mjs"',
        ]
        repo = tmp_path / "multi_repo"
        repo.mkdir()
        (repo / ".pi").mkdir()
        (repo / ".pi" / "test-config.json").write_text(
            json.dumps({"suiteCommands": pin}), encoding="utf-8"
        )
        (repo / "tests").mkdir()
        (repo / "src").mkdir()
        (repo / "pytest.ini").write_text("[pytest]\n")
        (repo / "tests" / "test_foo.py").write_text("def test_foo(): pass\n")
        (repo / "tests" / "node").mkdir()
        (repo / "tests" / "node" / "utils.test.mjs").write_text("import test from \"node:test\";\n")
        _init_repo_with_dev(repo, message="init")
        # Docs-only change → nothing selectable → the full-suite fallback.
        (repo / "README.md").write_text("# changed\n")

        captured: list[str] = []

        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            pytest_result = run_suite(
                "pytest", cwd=repo, scope="changed", base_ref="dev",
            )
            pytest_captured = list(captured)
            captured.clear()
            node_result = run_suite(
                "node", cwd=repo, scope="changed", base_ref="dev",
            )

        assert pytest_result["scope"] == "full"
        assert node_result["scope"] == "full"
        # Each suite name is mapped to its own pinned command (AC5): the
        # selected command source is the pin entry, verbatim.
        _assert_command_source(pytest_result, pin[0])
        _assert_command_source(node_result, pin[1])
        # Each suite name is mapped to its own pinned command. The node pin is
        # compared on its parts (shlex re-joins argv, dropping the quotes).
        _assert_pinned_pytest_only(pytest_captured)
        assert len(captured) == 1, captured
        assert captured[0].startswith("node --test "), captured
        assert "tests/node/**/*.mjs" in captured[0], captured
        # The pytest run filtered out the node pin (and vice versa).
        assert not any("node --test" in c for c in pytest_captured)
        assert not any("pytest" in c for c in captured)

    def test_changed_scope_fallback_all_uses_pinned_commands(
        self, tmp_path: Path,
    ) -> None:
        """--suite all --scope changed keeps resolving the full pinned set."""
        from types import SimpleNamespace

        repo = self._pin_repo_with_change(tmp_path, _PINNED_PYTEST_COMMANDS)

        captured: list[str] = []

        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite(
                "all", cwd=repo, scope="changed", base_ref="dev",
            )

        assert result["scope"] == "full"
        # ``--suite all`` keeps resolving the full pinned set (AC4/AC5).
        _assert_command_source(result, _PINNED_PYTEST_COMMANDS[0])
        _assert_pinned_pytest_only(captured)

    def test_changed_scope_fallback_empty_filtered_pin_raises(
        self, tmp_path: Path,
    ) -> None:
        """AC7/AC6: a pin with no matching command raises the known-suite-style
        error instead of silently running nothing."""
        repo = self._pin_repo_with_change(
            tmp_path, ['node --test "tests/node/**/*.mjs"'],
        )

        with pytest.raises(ValueError) as excinfo, \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            run_suite("pytest", cwd=repo, scope="changed", base_ref="dev")
        assert "unknown suite: pytest" in str(excinfo.value)

    def test_run_summary_looks_up_pinned_command(self, tmp_path: Path) -> None:
        """AC3: read-only run_summary resolves the pinned command, so a cache
        summary of --suite pytest finds the pinned entry."""
        repo = self._pin_repo_with_change(tmp_path, _PINNED_PYTEST_COMMANDS)

        looked_up: list[str] = []

        def fake_query(cmd, **kwargs):
            looked_up.append(cmd)
            return {
                "stdout": "passed",
                "stderr": "",
                "scope": "full",
                "test_type": "full",
                "interpreter": "python3 -m pytest",
            }

        with mock.patch.object(run_tests, "query_cached", side_effect=fake_query):
            summary = run_summary(("pytest",), cwd=repo)

        # The pinned command is what gets looked up (never the bare token).
        assert looked_up == ["python3 -m pytest -q -r a --disable-warnings"]
        assert summary["success"] is True
        assert "pytest" not in summary["missing"]

    def test_run_summary_reports_miss_when_pin_has_no_matching_command(
        self, tmp_path: Path,
    ) -> None:
        """AC3/AC6: a non-matching pin is a reported miss, never an empty
        success (nothing looked up, nothing claimed green)."""
        repo = self._pin_repo_with_change(
            tmp_path, ['node --test "tests/node/**/*.mjs"'],
        )

        looked_up: list[str] = []

        def fake_query(cmd, **kwargs):
            looked_up.append(cmd)

        with mock.patch.object(run_tests, "query_cached", side_effect=fake_query):
            summary = run_summary(("pytest",), cwd=repo)

        assert looked_up == []
        assert summary["success"] is False
        assert summary["missing"] == ["pytest"]


class TestScopePropagated:
    """Tests that scope is carried through run_suite and run_all results."""

    def test_run_suite_carries_scope(self) -> None:
        """run_suite result dict carries scope field."""
        from types import SimpleNamespace

        captured: list[str] = []
        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        # Bypass the shared "test" semaphore (SA-0MUF9TXFN009I6Q2,
        # SA-0MUGXZGVZ001UFPG).
        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_suite(
                "pytest",
                cwd="/tmp",
                use_cache=False,
                force=True,
            )
        assert "scope" in result
        assert result["scope"] == "full"
        assert captured  # verify command was "executed" (mocked)

    def test_run_all_carries_scope_per_suite(self) -> None:
        """run_all results carry scope for each suite."""
        from types import SimpleNamespace

        captured: list[str] = []
        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        # Bypass the shared "test" semaphore (SA-0MUF9TXFN009I6Q2,
        # SA-0MUGXZGVZ001UFPG).
        with mock.patch.object(run_tests, "_run_cmd", side_effect=fake_run), \
             mock.patch.object(run_tests, "_test_concurrency_slot"):
            result = run_all(
                suites=("pytest",),
                cwd="/tmp",
                use_cache=False,
                force=True,
            )
        assert "scope" in result
        assert result["scope"] == "full"
        for suite_result in result.get("suites", {}).values():
            assert "scope" in suite_result
            assert suite_result["scope"] == "full"
        assert captured  # verify command was "executed" (mocked)

    def test_file_not_found_carries_scope(self) -> None:
        """Exception paths in run_suite still carry scope in the result."""
        result = run_suite(
            "pytest",
            cwd="/nonexistent-path-that-will-not-exist-xyz",
            use_cache=True,
            force=True,
            scope="changed",
        )
        assert result.get("scope") in ("full", "changed")


# ---------------------------------------------------------------------------
# CLI flag tests
# ---------------------------------------------------------------------------


class TestCLIFlags:
    """Tests for --scope and --target-branch CLI arguments."""

    def test_default_scope_is_full(self) -> None:
        """Default scope is 'full'."""
        parser = build_parser()
        args = parser.parse_args([])
        assert args.scope == "full"

    def test_scope_changed_accepted(self) -> None:
        """--scope changed is accepted."""
        parser = build_parser()
        args = parser.parse_args(["--scope", "changed"])
        assert args.scope == "changed"

    def test_scope_invalid_rejected(self) -> None:
        """--scope with invalid value is rejected by argparse."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--scope", "invalid"])

    def test_target_branch_flag(self) -> None:
        """--target-branch flag is accepted."""
        parser = build_parser()
        args = parser.parse_args(["--target-branch", "main"])
        assert args.target_branch == "main"

    def test_default_target_branch_is_none(self) -> None:
        """Default target-branch is None (uses origin/dev)."""
        parser = build_parser()
        args = parser.parse_args([])
        assert args.target_branch is None


# ---------------------------------------------------------------------------
# Explicit tree fingerprint --fingerprint (SA-0MW7C6AYO0062GJB, runner AC1-AC3)
# ---------------------------------------------------------------------------


class TestFingerprintCLI:
    """CLI tests for --fingerprint: bind, fail-closed validation, namespaces.

    The cache-layer semantics (store + immediate hit, foreign fingerprint
    miss, legacy entry miss) are pinned per-kind in tests/test_test_cache.py;
    these tests cover the runner CLI surface and its threading into
    run_all/run_suite (AC1), the two rejection paths (AC2), and the help/doc
    contract (AC6).
    """

    @staticmethod
    def _tree_sha(repo: Path) -> str:
        """The tree SHA of the fixture repo's HEAD (a usable fingerprint)."""
        return run_git(repo, ["rev-parse", "HEAD^{tree}"], check=True).stdout.strip()

    @staticmethod
    def _head_sha(repo: Path) -> str:
        """The HEAD commit SHA of the fixture repo (not a tree)."""
        return run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()

    def _capture_guarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> dict:
        """Replace guarded_run_all with a recorder; run never executes."""
        captured: dict = {}

        def fake_guarded_run_all(*args, **kwargs):
            captured.update(kwargs)
            return {
                "success": True,
                "suites": {},
                "failures": [],
                "notices": [],
                "scope": kwargs.get("scope", "full"),
                "timing": {},
            }

        monkeypatch.setattr(run_tests, "guarded_run_all", fake_guarded_run_all)
        return captured

    @staticmethod
    def _fake_run(calls: list[str]):
        """A green fake command runner recording each invocation."""

        def fake_run(cmd_list, **kwargs):
            calls.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        return fake_run

    # -- acceptance surface --------------------------------------------------

    def test_default_fingerprint_is_none(self) -> None:
        """Omitting the flag keeps historical behaviour: None forwarded."""
        args = build_parser().parse_args([])
        assert args.fingerprint is None

    def test_valid_tree_sha_is_forwarded_to_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A usable tree SHA binds: guarded_run_all receives it (AC1)."""
        repo = _make_repo(tmp_path)
        tree = self._tree_sha(repo)
        captured = self._capture_guarded(monkeypatch)
        rc = run_tests.main(
            ["--fingerprint", tree, "--json", "--project-root", str(repo)]
        )
        assert rc == 0
        assert captured["fingerprint"] == tree
        assert captured["scope"] == "full"

    def test_uppercase_hex_is_normalised_to_lowercase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The value keys the entry as git writes it: lower case."""
        repo = _make_repo(tmp_path)
        tree = self._tree_sha(repo).upper()
        captured = self._capture_guarded(monkeypatch)
        rc = run_tests.main(
            ["--fingerprint", tree, "--project-root", str(repo)]
        )
        assert rc == 0
        assert captured["fingerprint"] == tree.lower()

    def test_same_fingerprint_stores_then_hits_at_run_all(
        self, tmp_path: Path
    ) -> None:
        """AC3: store plus immediate hit for the same fingerprint.

        The first run executes and records the entry under the tree
        namespace; the second is served from cache — one spawn total.
        """
        repo = _make_repo(tmp_path)
        tree = self._tree_sha(repo)
        calls: list[str] = []
        with mock.patch.object(
            run_tests, "_run_cmd", side_effect=self._fake_run(calls)
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            first = run_all(suites=("pytest",), cwd=repo, fingerprint=tree)
            second = run_all(suites=("pytest",), cwd=repo, fingerprint=tree)
        assert first["suites"]["pytest"]["cached"] is False
        assert second["suites"]["pytest"]["cached"] is True, (
            "the tree-keystored verdict must be served to the same tree"
        )
        assert len(calls) == 1, "the second run must be served from cache"
        hit = lookup("pytest", tree, cwd=str(repo), fingerprint_kind="tree")
        assert hit is not None and hit["cached"] is True
        assert hit["fingerprint_kind"] == FINGERPRINT_KIND_TREE

    def test_different_fingerprint_misses_and_re_runs(
        self, tmp_path: Path
    ) -> None:
        """AC3: a stored verdict for one tree is a miss for another."""
        repo = _make_repo(tmp_path)
        tree_a = self._tree_sha(repo)
        (repo / "README.md").write_text("# test repo v2\n")
        commit_all(repo, "second")
        tree_b = self._tree_sha(repo)
        assert tree_a != tree_b
        calls: list[str] = []
        with mock.patch.object(
            run_tests, "_run_cmd", side_effect=self._fake_run(calls)
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            first = run_all(suites=("pytest",), cwd=repo, fingerprint=tree_a)
            second = run_all(suites=("pytest",), cwd=repo, fingerprint=tree_b)
        assert first["suites"]["pytest"]["cached"] is False
        assert second["suites"]["pytest"]["cached"] is False, (
            "a foreign tree's verdict must never satisfy a tree request"
        )
        assert len(calls) == 2

    def test_worktree_legacy_entry_misses_for_a_tree_request(
        self, tmp_path: Path
    ) -> None:
        """AC3: a legacy/foreign worktree entry never satisfies a tree request.

        A default (unfingerprinted) run records a loose-worktree entry, which
        is a different namespace: the tree request executes fresh and stores
        its own tree-keyed entry.
        """
        repo = _make_repo(tmp_path)
        tree = self._tree_sha(repo)
        calls: list[str] = []
        with mock.patch.object(
            run_tests, "_run_cmd", side_effect=self._fake_run(calls)
        ), mock.patch.object(run_tests, "_test_concurrency_slot"):
            worktree = run_all(suites=("pytest",), cwd=repo)
            tree_run = run_all(suites=("pytest",), cwd=repo, fingerprint=tree)
        assert worktree["suites"]["pytest"]["cached"] is False
        assert tree_run["suites"]["pytest"]["cached"] is False, (
            "a loose-worktree entry must never satisfy a tree request"
        )
        assert len(calls) == 2
        # The tree namespace holds its own entry, invisible to the worktree kind.
        hit = lookup("pytest", tree, cwd=str(repo), fingerprint_kind="tree")
        assert hit is not None and hit["cached"] is True

    # -- rejection paths (AC2: exit 2, actionable, never a silent fallback) --

    def test_malformed_value_rejected_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        repo = _make_repo(tmp_path)
        rc = run_tests.main(
            ["--fingerprint", "not-a-sha", "--project-root", str(repo)]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "not a usable tree SHA" in err
        assert "--fingerprint" in err

    def test_wrong_length_hex_rejected_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        repo = _make_repo(tmp_path)
        rc = run_tests.main(
            ["--fingerprint", "a" * 39, "--project-root", str(repo)]
        )
        assert rc == 2
        assert "not a usable tree SHA" in capsys.readouterr().err

    def test_unknown_object_rejected_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Format-valid but absent: the repository must really hold the tree."""
        repo = _make_repo(tmp_path)
        rc = run_tests.main(
            ["--fingerprint", "a" * 40, "--project-root", str(repo)]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "does not hold an object" in err

    def test_commit_sha_rejected_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A commit SHA is not a tree: the mode cannot bind it."""
        repo = _make_repo(tmp_path)
        rc = run_tests.main(
            ["--fingerprint", self._head_sha(repo), "--project-root", str(repo)]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "not a tree" in err
        assert "commit" in err

    def test_scope_changed_rejected_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """--scope changed cannot bind a tree fingerprint: rejected, not loose."""
        repo = _make_repo(tmp_path)
        tree = self._tree_sha(repo)
        rc = run_tests.main(
            ["--fingerprint", tree, "--scope", "changed",
             "--project-root", str(repo)]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "cannot bind" in err
        assert "--scope" in err

    def test_summary_rejected_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """--summary reads only worktree entries; the flag cannot bind it."""
        repo = _make_repo(tmp_path)
        tree = self._tree_sha(repo)
        rc = run_tests.main(
            ["--fingerprint", tree, "--summary", "--project-root", str(repo)]
        )
        assert rc == 2
        assert "--summary" in capsys.readouterr().err

    def test_no_cache_rejected_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """--no-cache neither stores nor looks up; the flag cannot bind it."""
        repo = _make_repo(tmp_path)
        tree = self._tree_sha(repo)
        rc = run_tests.main(
            ["--fingerprint", tree, "--no-cache", "--project-root", str(repo)]
        )
        assert rc == 2
        assert "--no-cache" in capsys.readouterr().err

    def test_rejection_never_dispatches_a_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected fingerprint stops before anything executes (fail closed)."""
        repo = _make_repo(tmp_path)
        captured = self._capture_guarded(monkeypatch)
        rc = run_tests.main(
            ["--fingerprint", "bogus", "--scope", "changed",
             "--project-root", str(repo)]
        )
        assert rc == 2
        assert not captured, "no dispatch may happen for a rejected combination"

    def test_help_describes_the_flag_as_fail_closed(self) -> None:
        """--help states the fail-closed contract (AC6)."""
        help_text = build_parser().format_help()
        assert "--fingerprint" in help_text
        assert "Fail-closed" in help_text
        assert "fingerprint_kind" in help_text
        assert "--scope full" in help_text


# ---------------------------------------------------------------------------
# Deleted/non-existent path regression tests (F1 — red baseline)
# These tests reproduce the bug where `map_changed_to_tests()` adds changed
# test-file paths without verifying they still exist on disk. They FAIL
# against the unfixed code and should PASS after the fix in F2.
# ---------------------------------------------------------------------------


class TestDeletedPathSelection:
    """AC1/AC3: deleted test files must be excluded from changed-scope selection."""

    def test_deleted_test_file_excluded_from_map(self, tmp_path: Path) -> None:
        """A test file that is tracked-in-git as changed but deleted from the
        worktree must NOT appear in map_changed_to_tests() results.

        This reproduces the original bug where a git-diff entry for a deleted
        test file was unconditionally added to the selection set.
        """
        repo = _make_repo(tmp_path)

        # Add and commit a test file, then delete it
        (repo / "tests" / "test_extra.py").write_text("import src.extra\n")
        commit_all(repo, "add test_extra")

        # Now delete the file (it will appear in git diff as a deletion)
        (repo / "tests" / "test_extra.py").unlink()

        # Simulate changed files: the deleted file appears in git diff
        changed = {"tests/test_extra.py"}
        tests = map_changed_to_tests(repo, changed)

        # BUG: currently this assertion FAILS because the deleted file is
        # still included in the selection.
        assert "tests/test_extra.py" not in tests, (
            "deleted test file tests/test_extra.py was included in "
            "map_changed_to_tests() selection even though the file no longer "
            "exists on disk"
        )

    def test_deleted_test_file_excluded_from_changed_scope_commands(
        self, tmp_path: Path
    ) -> None:
        """A deletion-only change must cause changed_scope_commands() to return
        None (full-scope fallback), not emit a command referencing the
        non-existent file.

        This is AC2: changed_scope_commands() must never emit a command that
        references a non-existent path.
        """
        repo = _make_repo(tmp_path)
        (repo / "pytest.ini").write_text("[pytest]\n")

        # Add and commit a test file, then delete it
        (repo / "tests" / "test_extra.py").write_text("import src.extra\n")
        commit_all(repo, "add test_extra")

        # Now delete the file
        (repo / "tests" / "test_extra.py").unlink()

        # The changed file is the deleted test
        changed = {"tests/test_extra.py"}
        cmds = changed_scope_commands(repo, base_ref="dev", changed_files=changed)

        # BUG: currently this assertion FAILS because the command still
        # references the non-existent file.
        assert cmds is None, (
            f"changed_scope_commands() returned a command for a deleted test file: {cmds}. "
            "Expected None (full-scope fallback) when the only changed test is deleted."
        )

    def test_mixed_deleted_and_modified_selection_excludes_deleted(
        self, tmp_path: Path
    ) -> None:
        """A mixed change (one deleted test file + one modified source file)
        must select only the test corresponding to the modified source, not
        the deleted test.

        Regression for AC1/AC3: deleted paths must not appear in the emitted command.
        """
        repo = _make_repo(tmp_path)
        (repo / "pytest.ini").write_text("[pytest]\n")

        # Add and commit a test file, then delete it
        (repo / "tests" / "test_extra.py").write_text("import src.extra\n")
        commit_all(repo, "add test_extra")

        # Modify src/utils.py AND delete test_extra.py
        (repo / "src" / "utils.py").write_text("# utils — modified\n")
        (repo / "tests" / "test_extra.py").unlink()

        changed = {"src/utils.py", "tests/test_extra.py"}
        tests = map_changed_to_tests(repo, changed)

        # test_utils.py should be selected (from modified src/utils.py)
        assert "tests/test_utils.py" in tests
        # test_extra.py should NOT be selected (it was deleted)
        assert "tests/test_extra.py" not in tests, (
            "deleted test file tests/test_extra.py was included in selection "
            "alongside an existing change"
        )

    def test_deleted_test_file_in_changed_scope_commands_mixed(
        self, tmp_path: Path
    ) -> None:
        """For a mixed change (modified source + deleted test), the emitted
        command must contain only existing paths.

        Regression for AC3: the pytest command should not reference the
        deleted file.
        """
        repo = _make_repo(tmp_path)
        (repo / "pytest.ini").write_text("[pytest]\n")

        # Add and commit a test file, then delete it
        (repo / "tests" / "test_extra.py").write_text("import src.extra\n")
        commit_all(repo, "add test_extra")

        # Modify src/utils.py AND delete test_extra.py
        (repo / "src" / "utils.py").write_text("# utils — modified\n")
        (repo / "tests" / "test_extra.py").unlink()

        changed = {"src/utils.py", "tests/test_extra.py"}
        cmds = changed_scope_commands(repo, base_ref="dev", changed_files=changed)

        # BUG: currently this assertion FAILS because the command still
        # references the non-existent file.
        assert cmds is not None
        assert len(cmds) == 1
        cmd = cmds[0]
        assert "pytest" in cmd.lower()
        assert "tests/test_utils.py" in cmd
        assert "tests/test_extra.py" not in cmd, (
            f"deleted test file tests/test_extra.py appears in changed-scope "
            f"command: {cmd}"
        )


# ---------------------------------------------------------------------------
# Hermetic sandbox invariant (SA-0MUG0WFP8008WN63 — parent AC1)
# ---------------------------------------------------------------------------


class TestRepoHelpersAreHermetic:
    """Every repo-building helper in this module must be sandbox-hermetic.

    Regression guard for parent AC1: a leaked repository-overriding
    environment variable (``GIT_DIR``) must not redirect the helpers away from
    their ``tmp_path`` sandbox, and each helper's repository must resolve
    inside ``tmp_path``.
    """

    def test_leaked_git_dir_cannot_redirect_repo_helpers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        victim = init_repo(tmp_path / "victim")
        (victim / "real.txt").write_text("real", encoding="utf-8")
        commit_all(victim, "victim base")
        victim_head = run_git(victim, ["rev-parse", "HEAD"], check=True).stdout.strip()

        direct_dir = tmp_path / "sandbox_repo"
        direct_dir.mkdir()
        (direct_dir / "fixture.txt").write_text("fixture", encoding="utf-8")

        monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
        try:
            direct = _init_repo_with_dev(direct_dir)
            full = _make_repo(tmp_path / "sandbox_make")
        finally:
            monkeypatch.delenv("GIT_DIR", raising=False)

        # The leaked GIT_DIR must not have touched the victim repository.
        assert run_git(victim, ["rev-parse", "HEAD"], check=True).stdout.strip() == victim_head
        # ...and both helpers' repositories must resolve inside the tmp_path sandbox.
        for created in (direct, full):
            toplevel = run_git(
                created, ["rev-parse", "--show-toplevel"], check=True
            ).stdout.strip()
            assert Path(toplevel).resolve() == created.resolve()
