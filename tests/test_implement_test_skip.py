"""Tests for implement.py run_tests() test-tooling skip behaviour.

Contract (per work item SA-0MSN4AXIQ007IZG2 ACs, umbrella SA-0MSI8YLTN0007KN8):

- AC1: run_tests() reports a skipped/no-op success when no test tooling is
  detected (no pytest suite, no ``scripts.test`` in package.json, no
  repo-local runner), so ``implement.py finish`` proceeds to commit → push
  instead of aborting on ENOENT / ``Missing script: "test"``.
- AC2: repos WITH a pytest suite are unaffected — pytest still runs and a
  real test failure still blocks finish.
- AC3: repos WITH a ``scripts.test`` entry are unaffected — ``npm test``
  still runs and a failure still blocks finish.
- AC4: the test step is gracefully skipped (with an informative message)
  when no applicable tooling is detected, and Unity projects get a
  Unity-specific message rather than a generic one.
- AC5: unit tests cover both paths (tooling absent → skipped; tooling
  present → runs, failure blocks).

Background: ``run_tests()`` unconditionally ran ``python3 -m pytest -x --tb=short -q``
and fell back to ``npm test``, so bash-only / Unity repos without either tool
aborted the finish phase. The fix detects the repo's test tooling first and
reports the test step as a skipped no-op when none exists. The per-repo
``IMPLEMENT_TEST_COMMAND`` override and repo-local runner scripts
(``run_tests.sh`` / ``run_unity_tests.sh`` / ``run_unity_tests.bat``) are
also honoured. Since SA-0MSN6FBFS006Z5QP the pytest/npm commands routed
through the cache are the canonical quiet forms (``pytest -q -r a
--disable-warnings`` / ``npm --silent test``) so cached runs share the test
skill's cache keys.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py).

    Loaded via importlib so the module's own ``_REPO_ROOT`` computation and
    ``skill.*`` imports resolve against the real repo.
    """
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_test_skip", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_test_skip"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    """Scratch directory standing in for the worktree root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture
def stub_run_cached(monkeypatch, implement_mod):
    """Replace module-level run_cached with a recording stub.

    ``results`` may be: None (always success), a single dict (returned for
    every call), or a callable receiving the call index and returning the
    dict for that call. Returns the list of recorded command strings.
    """

    def make(results=None):
        calls: list[str] = []

        def fake(command: str, **kwargs):
            calls.append(command)
            if results is None:
                return {"stdout": "ok", "stderr": "", "exit_code": 0}
            if isinstance(results, dict):
                return results
            return results(len(calls) - 1)

        monkeypatch.setattr(implement_mod, "run_cached", fake)
        return calls

    return make


def _no_tooling(monkeypatch, implement_mod) -> None:
    """Force detection to report 'no test tooling'."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: None)


# ---------------------------------------------------------------------------
# AC1/AC4: no tooling → test step is a skipped no-op (never a failure)
# ---------------------------------------------------------------------------


def test_tests_skipped_when_no_tooling_detected(implement_mod, repo_dir):
    """No pytest suite, no npm test script, no runner → skipped no-op."""
    result = implement_mod.run_tests(str(repo_dir))

    assert result["success"] is True, (
        f"finish must not abort when no test tooling exists: {result}"
    )
    assert result["exit_code"] == 0
    assert result["skipped"] is True
    assert result["failures"] == []
    assert "no test tooling" in result["stdout"].lower(), (
        "skipped test step must be reported as an informative no-op in stdout"
    )


def test_tests_skipped_when_only_unity_project(implement_mod, repo_dir, monkeypatch):
    """Unity project without a runner → Unity-specific skip message."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "unity")
    result = implement_mod.run_tests(str(repo_dir))

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["tooling"] == "unity"
    assert "unity" in result["stdout"].lower(), (
        "Unity projects must get a Unity-specific message, not a generic one"
    )


# ---------------------------------------------------------------------------
# AC2: pytest suite present → pytest runs; failures still block
# ---------------------------------------------------------------------------


def test_pytest_runs_when_suite_detected(
    implement_mod, repo_dir, stub_run_cached, monkeypatch
):
    """pytest detected → canonical pytest command is executed (through cache)."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "pytest")
    calls = stub_run_cached()

    result = implement_mod.run_tests(str(repo_dir))

    assert calls == [implement_mod.PYTEST_CMD], (
        f"expected exactly one pytest run, got {calls}"
    )
    assert implement_mod.PYTEST_CMD == "pytest -q -r a --disable-warnings"
    assert result["success"] is True
    assert result["skipped"] is False
    assert result["tooling"] == "pytest"
    assert result["failures"] == []


def test_pytest_failure_still_blocks_when_suite_detected(
    implement_mod, repo_dir, stub_run_cached, monkeypatch
):
    """pytest suite present + failing run → finish must be blocked."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "pytest")
    stub_run_cached({"stdout": "test_x.py:12 FAILED", "stderr": "", "exit_code": 1})

    result = implement_mod.run_tests(str(repo_dir))

    assert result["success"] is False
    assert result["skipped"] is False
    assert result["exit_code"] != 0
    assert any("FAILED" in f for f in result["failures"]), (
        "failures must be parsed from the failing run output"
    )


def test_pytest_failure_falls_back_to_npm_test_when_test_script_exists(
    implement_mod, repo_dir, stub_run_cached, monkeypatch
):
    """pytest fails + repo also defines scripts.test → npm test fallback runs."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "pytest")
    monkeypatch.setattr(implement_mod, "_has_test_script", lambda cwd: True)
    calls = stub_run_cached(
        lambda i: (
            {"stdout": "FAILED", "stderr": "", "exit_code": 1}
            if i == 0
            else {"stdout": "Test Files  2 passed (2)", "stderr": "", "exit_code": 0}
        )
    )

    result = implement_mod.run_tests(str(repo_dir))

    assert calls == [implement_mod.PYTEST_CMD, implement_mod.NPM_TEST_CMD]
    assert result["success"] is True
    assert result["tooling"] == "npm"


def test_pytest_without_test_script_does_not_try_npm(
    implement_mod, repo_dir, stub_run_cached, monkeypatch
):
    """pytest fails + no scripts.test → no pointless npm test fallback."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "pytest")
    calls = stub_run_cached({"stdout": "FAILED", "stderr": "", "exit_code": 1})

    result = implement_mod.run_tests(str(repo_dir))

    assert calls == [implement_mod.PYTEST_CMD]
    assert result["success"] is False


# ---------------------------------------------------------------------------
# AC3: npm test script present (no pytest) → npm test runs; failures block
# ---------------------------------------------------------------------------


def test_npm_test_runs_when_no_pytest_suite(
    implement_mod, repo_dir, stub_run_cached, monkeypatch
):
    """No pytest, but scripts.test exists → canonical npm test is executed."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "npm")
    calls = stub_run_cached()

    result = implement_mod.run_tests(str(repo_dir))

    assert calls == [implement_mod.NPM_TEST_CMD]
    assert implement_mod.NPM_TEST_CMD == "npm --silent test"
    assert result["success"] is True
    assert result["skipped"] is False
    assert result["tooling"] == "npm"


def test_npm_test_failure_still_blocks(implement_mod, repo_dir, stub_run_cached, monkeypatch):
    """scripts.test present + failing npm test → finish must be blocked."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "npm")
    stub_run_cached({"stdout": "FAIL", "stderr": "npm error", "exit_code": 1})

    result = implement_mod.run_tests(str(repo_dir))

    assert result["success"] is False
    assert result["skipped"] is False


# ---------------------------------------------------------------------------
# repo-local runner + env override
# ---------------------------------------------------------------------------


def test_repo_runner_script_runs_via_bash(
    implement_mod, repo_dir, stub_run_cached, monkeypatch
):
    """Repo-local .sh runner → bash <script> is executed."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "repo-script")
    monkeypatch.setattr(
        implement_mod, "_find_repo_test_runner", lambda cwd: "run_tests.sh"
    )
    calls = stub_run_cached()

    result = implement_mod.run_tests(str(repo_dir))

    assert calls == ["bash run_tests.sh"]
    assert result["success"] is True
    assert result["tooling"] == "repo-script"


def test_repo_runner_script_failure_blocks(
    implement_mod, repo_dir, stub_run_cached, monkeypatch
):
    """Repo-local runner present + failing run → finish must be blocked."""
    monkeypatch.setattr(implement_mod, "_detect_test_tooling", lambda cwd: "repo-script")
    monkeypatch.setattr(
        implement_mod, "_find_repo_test_runner", lambda cwd: "run_tests.sh"
    )
    stub_run_cached({"stdout": "", "stderr": "boom", "exit_code": 2})

    result = implement_mod.run_tests(str(repo_dir))

    assert result["success"] is False
    assert result["skipped"] is False


def test_env_override_takes_precedence_over_detection(
    implement_mod, repo_dir, stub_run_cached, monkeypatch
):
    """IMPLEMENT_TEST_COMMAND overrides detection entirely."""
    monkeypatch.setenv("IMPLEMENT_TEST_COMMAND", "bash run_custom_tests.sh")
    calls = stub_run_cached()

    result = implement_mod.run_tests(str(repo_dir))

    assert calls == ["bash run_custom_tests.sh"]
    assert result["success"] is True
    assert result["tooling"] == "override"


def test_env_override_failure_blocks(implement_mod, repo_dir, stub_run_cached, monkeypatch):
    """IMPLEMENT_TEST_COMMAND set + failing run → finish must be blocked."""
    monkeypatch.setenv("IMPLEMENT_TEST_COMMAND", "false")
    stub_run_cached({"stdout": "", "stderr": "exit 1", "exit_code": 1})

    result = implement_mod.run_tests(str(repo_dir))

    assert result["success"] is False


# ---------------------------------------------------------------------------
# AC5: detection logic (tooling present/absent)
# ---------------------------------------------------------------------------


def test_detect_pytest_suite_via_pytest_ini(implement_mod, repo_dir, monkeypatch):
    """pytest.ini marker → pytest tooling detected."""
    monkeypatch.setattr(implement_mod, "_pytest_importable", lambda cwd: True)
    (repo_dir / "pytest.ini").write_text("[pytest]\n")

    assert implement_mod._detect_test_tooling(str(repo_dir)) == "pytest"


def test_detect_pytest_suite_via_test_files(implement_mod, repo_dir, monkeypatch):
    """tests/ dir with python files (no config marker) → pytest tooling."""
    monkeypatch.setattr(implement_mod, "_pytest_importable", lambda cwd: True)
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_thing.py").write_text("def test_x(): pass\n")

    assert implement_mod._detect_test_tooling(str(repo_dir)) == "pytest"


def test_detect_pytest_markers_require_importable_pytest(
    implement_mod, repo_dir, monkeypatch
):
    """pytest config exists but pytest not importable → no pytest tooling."""
    monkeypatch.setattr(implement_mod, "_pytest_importable", lambda cwd: False)
    (repo_dir / "pytest.ini").write_text("[pytest]\n")

    assert implement_mod._detect_test_tooling(str(repo_dir)) is None


def test_detect_npm_via_test_script(implement_mod, repo_dir):
    """package.json with scripts.test (no pytest suite) → npm tooling."""
    (repo_dir / "package.json").write_text(
        json.dumps({"scripts": {"test": "echo hi"}})
    )

    assert implement_mod._detect_test_tooling(str(repo_dir)) == "npm"


def test_detect_repo_runner_script(implement_mod, repo_dir):
    """run_tests.sh at repo root → repo-script tooling."""
    (repo_dir / "run_tests.sh").write_text("#!/usr/bin/env bash\necho hi\n")

    assert implement_mod._detect_test_tooling(str(repo_dir)) == "repo-script"


def test_detect_unity_project_via_assets_dir(implement_mod, repo_dir):
    """Assets/ dir → unity tooling (specific skip, not generic)."""
    (repo_dir / "Assets").mkdir()

    assert implement_mod._detect_test_tooling(str(repo_dir)) == "unity"


def test_detect_unity_project_via_project_version(implement_mod, repo_dir):
    """ProjectSettings/ProjectVersion.txt → unity tooling."""
    settings = repo_dir / "ProjectSettings"
    settings.mkdir()
    (settings / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.0f1\n")

    assert implement_mod._detect_test_tooling(str(repo_dir)) == "unity"


def test_detect_none_in_empty_repo(implement_mod, repo_dir):
    """Empty repo (no markers, no package.json, no runner) → no tooling."""
    assert implement_mod._detect_test_tooling(str(repo_dir)) is None


def test_detect_none_when_package_json_has_no_test_script(implement_mod, repo_dir):
    """package.json without scripts.test → no npm tooling."""
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"build": "echo"}}))

    assert implement_mod._detect_test_tooling(str(repo_dir)) is None
