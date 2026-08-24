"""Tests for scope-aware test execution in run_tests.py.

Tests cover:
- `compute_changed_files()` with merge-base resolution and fallbacks
- `map_changed_to_tests()` heuristic mapping
- `map_changed_to_tests()` import-graph expansion
- `changed_scope_commands()` integration with full_suite_commands
- Scope carried in run_suite/run_all results
- Custom suiteCommands fallback to full scope
- CLI flag parsing
"""

from __future__ import annotations

import json
import subprocess

# Resolve the module under test — same pattern as run_tests.py itself.
# The runner lives at <skills>/test/scripts/run_tests.py; inserting that
# scripts dir makes `import run_tests` work from any cwd WITHOUT depending
# on conftest collection order (the stdlib `test` package would otherwise
# shadow <skills>/test when only the skills root is on sys.path).
import sys as _sys
from pathlib import Path
from unittest import mock

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _SCRIPT_DIR.parent / "scripts"  # <skills>/test/scripts
if str(_RUNNER_DIR) not in _sys.path:
    _sys.path.insert(0, str(_RUNNER_DIR))

from run_tests import (
    build_parser,
    changed_scope_commands,
    compute_changed_files,
    map_changed_to_tests,
    run_all,
    run_suite,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with test and source files."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
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

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "dev"], cwd=repo, check=True, capture_output=True)

    # Second commit on dev so HEAD~1 always exists for fallback tests
    (repo / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "docs: add readme"], cwd=repo, check=True, capture_output=True)

    # Create a remote-like branch for merge-base resolution
    subprocess.run(["git", "branch", "origin/dev"], cwd=repo, check=True, capture_output=True)

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
        subprocess.run(["git", "branch", "-D", "origin/dev"], cwd=repo, check=True, capture_output=True)

        (repo / "src" / "bar.py").write_text("# bar — modified\n")

        result = compute_changed_files(repo, base_ref="origin/dev")
        assert "src/bar.py" in result

    def test_falls_back_to_head_parent(self, tmp_path: Path) -> None:
        """When both remotes and local are absent, falls back to HEAD~1."""
        repo = _make_repo(tmp_path)

        (repo / "src" / "utils.py").write_text("# utils — modified\n")

        # Move to a feature branch so we can delete the dev branch
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-D", "origin/dev"], cwd=repo, check=True, capture_output=True)
        # Delete local dev branch; HEAD~1 is now the only base available
        subprocess.run(["git", "branch", "-D", "dev"], cwd=repo, check=True, capture_output=True)

        result = compute_changed_files(repo, base_ref="origin/dev")
        assert "src/utils.py" in result

    def test_no_changes(self, tmp_path: Path) -> None:
        """Returns empty set when no files changed."""
        repo = _make_repo(tmp_path)
        result = compute_changed_files(repo, base_ref="dev")
        assert result == set()

    def test_ignores_untracked_files(self, tmp_path: Path) -> None:
        """Untracked files are not included in changed files."""
        repo = _make_repo(tmp_path)
        (repo / "src" / "utils.py").write_text("# utils — modified\n")
        (repo / "src" / "untracked.py").write_text("# new file\n")

        result = compute_changed_files(repo, base_ref="dev")
        assert "src/untracked.py" not in result
        assert "src/utils.py" in result


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

        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "dev"], cwd=repo, check=True, capture_output=True)

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

    def test_falls_back_to_full_for_custom_suite_commands(self, tmp_path: Path) -> None:
        """When .pi/test-config.json has suiteCommands, changed scope returns None."""
        repo = _make_repo(tmp_path)
        (repo / ".pi").mkdir()
        (repo / ".pi" / "test-config.json").write_text(
            json.dumps({"suiteCommands": ["npm test"]})
        )

        (repo / "src" / "utils.py").write_text("# modified\n")

        cmds = changed_scope_commands(repo, base_ref="dev")
        assert cmds is None  # custom commands → fall back to full scope

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

        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "dev"], cwd=repo, check=True, capture_output=True)

        (repo / "src" / "utils.mjs").write_text("export const helper = 2;\n")

        cmds = changed_scope_commands(repo, base_ref="dev")
        assert cmds is not None
        # Only the affected node test is selected
        assert any("utils.test.mjs" in c for c in cmds)
        assert all("other.test.mjs" not in c for c in cmds)


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
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "dev"], cwd=repo, check=True, capture_output=True)
        (repo / "src" / "foo.py").write_text("from src import utils\nchanged\n")

        captured: list[str] = []
        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        with mock.patch("run_tests._run_cmd", side_effect=fake_run):
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
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "dev"], cwd=repo, check=True, capture_output=True)
        # change only a doc — no test files selectable
        (repo / "README.md").write_text("# changed\n")

        captured: list[str] = []
        def fake_run(cmd_list, **kwargs):
            captured.append(" ".join(cmd_list))
            return SimpleNamespace(returncode=0, stdout="passed", stderr="")

        with mock.patch("run_tests._run_cmd", side_effect=fake_run):
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
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "dev"], cwd=repo, check=True, capture_output=True)
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


class TestScopePropagated:
    """Tests that scope is carried through run_suite and run_all results."""

    def test_run_suite_carries_scope(self) -> None:
        """run_suite result dict carries scope field."""
        result = run_suite(
            "pytest",
            cwd="/tmp",  # won't execute — just checking structure
            use_cache=False,
            force=True,
        )
        assert "scope" in result
        assert result["scope"] == "full"

    def test_run_all_carries_scope_per_suite(self) -> None:
        """run_all results carry scope for each suite."""
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
