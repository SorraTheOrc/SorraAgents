"""Unit tests for skill/test/scripts/run_tests.py — the test skill runner.

Covers quiet-command canonicalization reuse, suite selection (pytest / Node),
and failure-parsing output shape compatible with the triage skill's
check_or_create.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skill.test.scripts.run_tests import (
    full_suite_commands,
    node_suite_commands,
    parse_node_failures,
    parse_pytest_failures,
    pytest_command,
    run_suite,
)

PYTEST_FAILURE_OUTPUT = """\
.FF                                                                      [100%]
=================================== FAILURES ===================================
__________________________________ test_fail ___________________________________

    def test_fail():
>       assert 1 == 2
E       assert 1 == 2

test_demo.py:5: AssertionError
__________________________________ test_error __________________________________

    def test_error():
>       raise RuntimeError("boom")
E       RuntimeError: boom

test_demo.py:8: RuntimeError
=========================== short test summary info ============================
FAILED test_demo.py::test_fail - assert 1 == 2
FAILED test_demo.py::test_error - RuntimeError: boom
2 failed, 1 passed in 0.03s
"""

NODE_FAILURE_OUTPUT = """\
TAP version 13
# Subtest: failing test
not ok 2 - failing test
  ---
  duration_ms: 3.578769
  type: 'test'
  failureType: 'testCodeFailure'
  error: |-
    Expected values to be strictly equal:

    1 !== 2

  code: 'ERR_ASSERTION'
  name: 'AssertionError'
  stack: |-
    TestContext.<anonymous> (file:///tmp/node-format-test/demo.test.mjs:4:37)
    Test.runInAsyncScope (node:async_hooks:214:14)
  ...
1..2
# tests 2
# pass 1
# fail 1
"""


# ---------------------------------------------------------------------------
# Quiet command canonicalization reuse
# ---------------------------------------------------------------------------


def test_pytest_command_reuses_quiet_canonicalization() -> None:
    """The pytest invocation must reuse canonicalize_quiet_test_command."""
    cmd = pytest_command()
    assert cmd == "pytest -q -r a --disable-warnings"


def test_node_suite_commands_cover_suite_dirs() -> None:
    cmds = node_suite_commands()
    assert len(cmds) == 3
    joined = " | ".join(cmds)
    assert "tests/node" in joined
    assert "tests/cli" in joined
    assert "tests/unit" in joined
    # Each command must use a glob pattern, not a bare directory (which node
    # v22 tries to load as a module).
    for cmd in cmds:
        assert cmd.startswith("node --test ")
        assert "/**/*.mjs" in cmd, f"expected glob pattern in: {cmd}"


def test_full_suite_commands_cover_pytest_and_node() -> None:
    """The canonical full-suite command set = quiet pytest + each node suite dir.

    Read-only consumers (e.g. the audit skill's automatic full-suite
    verification) query the per-repo cache with exactly these commands so
    cache entries written by run_tests.py are reused.
    """
    cmds = full_suite_commands()
    assert cmds[0] == "pytest -q -r a --disable-warnings"
    assert len(cmds) == 4  # 1 pytest + 3 node suite dirs
    joined = " | ".join(cmds)
    assert "tests/node" in joined
    assert "tests/cli" in joined
    assert "tests/unit" in joined


def _make_repo(tmp_path: Path, npm_test_script: bool = False, dirs: tuple[str, ...] = ("tests/node", "tests/cli", "tests/unit")) -> Path:
    """Create a throwaway project root with the given npm test script and suite dirs."""
    repo = tmp_path / "proj"
    repo.mkdir()
    for d in dirs:
        (repo / d).mkdir(parents=True, exist_ok=True)
    if npm_test_script:
        (repo / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest"}})
        )
    return repo


def test_node_suite_commands_respect_custom_project_root(
    tmp_path: Path,
) -> None:
    """node_suite_commands must read package.json from the given root.

    A project with an npm test script uses `npm --silent test -- <dir>` per
    suite directory; the framework repo (no npm test script) keeps the
    `node --test "<dir>/**/*.mjs"` glob form.
    """
    repo = _make_repo(tmp_path, npm_test_script=True)
    cmds = node_suite_commands(repo)
    assert len(cmds) == 3
    assert all(c.startswith("npm --silent test -- ") for c in cmds)
    assert all("/**/*.mjs" not in c for c in cmds)

    framework_cmds = node_suite_commands()
    assert all(c.startswith("node --test ") for c in framework_cmds)


# ---------------------------------------------------------------------------
# Skip-missing-dir behavior (SA-0MSJELL44009XYIL)
# ---------------------------------------------------------------------------


def test_node_suite_commands_skip_missing_dirs(tmp_path: Path) -> None:
    """A suite dir that does not exist in the project must be skipped.

    A repo without tests/node must NOT receive a `tests/node` command: the
    guaranteed-failing run (vitest 'No test files found') would defeat the
    audit skill's fail-closed auto-verification for repos whose layout
    diverges from NODE_SUITE_DIRS (SA-0MSJELL44009XYIL).
    """
    repo = _make_repo(tmp_path, dirs=("tests/cli", "tests/unit"))
    cmds = node_suite_commands(repo)
    assert len(cmds) == 2
    joined = " | ".join(cmds)
    assert "tests/node" not in joined
    assert "tests/cli" in joined
    assert "tests/unit" in joined


def test_full_suite_commands_skip_missing_dirs(tmp_path: Path) -> None:
    """full_suite_commands emits only commands that can pass for the repo."""
    # A repo with a pytest config + tests/cli only: pytest + cli, no node/unit.
    repo = _make_repo(tmp_path, dirs=("tests/cli",))
    (repo / "pytest.ini").write_text("[pytest]\n")
    cmds = full_suite_commands(repo)
    assert cmds[0] == "pytest -q -r a --disable-warnings"
    assert len(cmds) == 2  # pytest + tests/cli only
    assert "tests/node" not in " | ".join(cmds)
    assert "tests/unit" not in " | ".join(cmds)


def test_node_suite_commands_no_dirs_yields_empty(tmp_path: Path) -> None:
    """A repo with no node suite dirs and no pytest config yields no commands.

    F2 AC3 (no phantom pytest): without a pytest config and without node suite
    dirs, no command can pass — the effective set is empty (the audit's
    never-block path reports a documented reason instead of gating).
    """
    repo = _make_repo(tmp_path, dirs=())
    assert node_suite_commands(repo) == []
    cmds = full_suite_commands(repo)
    assert cmds == []  # no phantom pytest for a repo without a pytest config


def test_node_suite_commands_skip_missing_dirs_with_npm_script(
    tmp_path: Path,
) -> None:
    """The skip applies to the npm test-script form too."""
    repo = _make_repo(tmp_path, npm_test_script=True, dirs=("tests/unit",))
    cmds = node_suite_commands(repo)
    assert len(cmds) == 1
    assert cmds[0] == "npm --silent test -- tests/unit"


def test_full_suite_commands_framework_regression_unchanged(tmp_path: Path) -> None:
    """The framework repo (pytest.ini + all three suite dirs) keeps the set."""
    # The framework itself has pytest.ini plus tests/node, tests/cli and
    # tests/unit: a repo with all of them must produce the exact pre-fix
    # command set (pytest + 3 node dirs), preserving read-only consumers.
    repo = _make_repo(tmp_path)
    (repo / "pytest.ini").write_text("[pytest]\n")
    cmds = full_suite_commands(repo)
    assert len(cmds) == 4  # pytest + 3 node suite dirs
    joined = " | ".join(cmds)
    assert "tests/node" in joined
    assert "tests/cli" in joined
    assert "tests/unit" in joined


# ---------------------------------------------------------------------------
# Failure-parsing output shape
# ---------------------------------------------------------------------------


def test_parse_pytest_failures_shape() -> None:
    """Pytest failures parse into records with test_name/excerpt/stack."""
    records = parse_pytest_failures(PYTEST_FAILURE_OUTPUT)
    assert len(records) == 2
    names = [r["test_name"] for r in records]
    assert "test_demo.py::test_fail" in names
    assert "test_demo.py::test_error" in names
    for record in records:
        assert record["test_name"]
        assert record["stdout_excerpt"]
        assert record["stack_trace"]


def test_parse_pytest_failures_include_stack_trace() -> None:
    """The parsed stack trace must contain the failing assertion line."""
    records = parse_pytest_failures(PYTEST_FAILURE_OUTPUT)
    fail_record = next(
        r for r in records if r["test_name"] == "test_demo.py::test_fail"
    )
    assert "AssertionError" in fail_record["stack_trace"]
    assert "test_demo.py:5" in fail_record["stack_trace"]


def test_parse_node_failures_shape() -> None:
    """Node TAP failures parse into records with test_name/excerpt/stack."""
    records = parse_node_failures(NODE_FAILURE_OUTPUT)
    assert len(records) >= 1
    record = records[0]
    assert record["test_name"] == "failing test"
    assert "1 !== 2" in record["stdout_excerpt"]
    assert "demo.test.mjs" in record["stack_trace"]


def test_failure_records_compatible_with_triage_input() -> None:
    """Records must carry the keys check_or_create.py consumes."""
    records = parse_pytest_failures(PYTEST_FAILURE_OUTPUT)
    payload = json.loads(json.dumps(records[0]))
    assert "test_name" in payload
    assert "stdout_excerpt" in payload
    assert "stack_trace" in payload


# ---------------------------------------------------------------------------
# Suite execution: non-zero exits and missing suites surfaced
# ---------------------------------------------------------------------------


def test_run_suite_surfaces_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing suite must surface a non-zero exit, not be swallowed."""

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout=PYTEST_FAILURE_OUTPUT, stderr="")

    monkeypatch.setattr("skill.test.scripts.run_tests._run_cmd", fake_run)
    # use_cache=False: this test exercises execution/parsing, not caching
    # (cache behaviour is covered in tests/test_run_tests_cache.py).
    result = run_suite("pytest", use_cache=False)
    assert result["returncode"] == 1
    assert len(result["failures"]) == 2
    assert result["success"] is False


def test_run_suite_reports_passing_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    """A passing suite must report success with no failures."""

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="5 passed in 0.03s", stderr="")

    monkeypatch.setattr("skill.test.scripts.run_tests._run_cmd", fake_run)
    result = run_suite("pytest", use_cache=False)
    assert result["returncode"] == 0
    assert result["success"] is True
    assert result["failures"] == []


def test_run_suite_node_runs_each_directory_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node suite must run each directory command separately (no && join)."""
    commands_run: list[str] = []

    def fake_run(cmd, **kwargs):
        commands_run.append(" ".join(cmd))
        return SimpleNamespace(returncode=0, stdout="# tests 2\n# pass 2\n# fail 0", stderr="")

    monkeypatch.setattr("skill.test.scripts.run_tests._run_cmd", fake_run)
    result = run_suite("node", use_cache=False)
    assert result["success"] is True
    assert len(commands_run) == 3
    assert all("&&" not in c for c in commands_run)
    assert any("tests/node" in c for c in commands_run)
    assert any("tests/cli" in c for c in commands_run)
    assert any("tests/unit" in c for c in commands_run)
    # Regression (SA-0MSF8KNE3003JDVD): commands must glob, not pass bare dirs.
    assert all("/**/*.mjs" in c for c in commands_run)
