"""Unit tests for skill/test/scripts/run_tests.py — the test skill runner.

Covers quiet-command canonicalization reuse, suite selection (pytest / Node /
bats), and failure-parsing output shape compatible with the triage skill's
check_or_create.py.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skill.test.scripts.run_tests import (
    bats_command,
    node_suite_commands,
    parse_bats_failures,
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

BATS_FAILURE_OUTPUT = """\
1..3
not ok 1 test_name_alpha in 12ms
# (from function `test_name_alpha` in file tests/install-worklog-plugin.bats, line 5)
#   `assert_equal 1 2' failed
ok 2 - test_name_beta
not ok 3 test_name_gamma in 3ms
"""


# ---------------------------------------------------------------------------
# Quiet command canonicalization reuse
# ---------------------------------------------------------------------------


def test_pytest_command_reuses_quiet_canonicalization() -> None:
    """The pytest invocation must reuse canonicalize_quiet_test_command."""
    cmd = pytest_command()
    assert cmd == "pytest -q -r a --disable-warnings"


def test_node_suite_commands_cover_suite_dirs() -> None:
    """Node suite commands must cover tests/node, tests/cli, tests/unit via globs.

    Regression (SA-0MSF8KNE3003JDVD): ``node --test <dir>`` fails on node
    v22.22.1 with MODULE_NOT_FOUND because a bare directory argument is
    treated as a module entry point, not a scan target. Commands must use
    glob patterns (e.g. ``node --test "tests/node/**/*.mjs"``).
    """
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


def test_bats_command_targets_install_worklog_plugin() -> None:
    """The bats invocation must target the install-worklog-plugin suite."""
    assert bats_command() == "bats tests/install-worklog-plugin.bats"


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


def test_parse_bats_failures_shape() -> None:
    """bats failures parse into records with test_name and excerpt."""
    records = parse_bats_failures(BATS_FAILURE_OUTPUT)
    names = [r["test_name"] for r in records]
    assert "test_name_alpha" in names
    assert "test_name_gamma" in names
    assert "test_name_beta" not in names  # passing test not reported
    for record in records:
        assert record["stdout_excerpt"]


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


def test_run_suite_missing_binary_surfaces_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing suite binary (e.g. bats) must produce a notice, not a silent drop."""

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("bats not installed")

    monkeypatch.setattr("skill.test.scripts.run_tests._run_cmd", fake_run)
    result = run_suite("bats", use_cache=False)
    assert result["success"] is False
    assert result["notice"]
    assert "bats" in result["notice"].lower() or "not installed" in result["notice"].lower()
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
