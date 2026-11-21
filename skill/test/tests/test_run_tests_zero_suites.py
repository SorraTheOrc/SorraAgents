"""Zero-suite edge case: the runner on a repo with no detected test suites.

SA-0MWNZLB07007S8QU (code half of ``CUR-0MWIBGLIL00774EU``). When
``full_suite_commands(cwd)`` returns ``[]`` — a greenfield checkout with no
pytest config, no node package ``test`` script and no ``.pi/test-config.json``
— ``run_suite`` must return a clean, machine-readable verdict instead of
crashing with ``UnboundLocalError`` on ``state_changed_flag`` (the local was
bound only inside ``for cmd in commands:``, which never iterates when the
command set is empty).

Acceptance criteria exercised here:

* AC1 — a zero-suite run reports ``success=True`` with the dedicated
  ``no_suites_detected`` notice code (CLI exits 0).
* AC2 — the verdict is reachable through the guarded dev-push path
  (``guarded_run_all``), so a greenfield repo needs no ``TEST_SCOPE_SKIP=1``.
* AC3 — ``run_suite`` (per-suite) and ``run_all`` (aggregate) both carry the
  signal, and the explicit-empty-command path behaves the same way.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap sys.path — mirror test_run_tests_tmpdir.py so `import run_tests`
# works regardless of collection order.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

import run_tests


@pytest.fixture()
def greenfield(tmp_path: Path) -> Path:
    """A repo skeleton with nothing for suite detection to latch onto.

    ``.githooks`` is the only directory present, mirroring the greenfield
    bootstrap that surfaced the defect: no ``tests/``, no ``pyproject.toml``,
    no ``package.json``, no ``.pi/test-config.json``.
    """
    (tmp_path / ".githooks").mkdir()
    return tmp_path


def _assert_clean_zero_suite_verdict(result: dict, *, suite: str = "all") -> None:
    """Assert *result* is the defined no-suites verdict, not a crash or pass."""
    assert result["success"] is True
    assert result["returncode"] == 0
    assert result["notice_code"] == run_tests.NOTICE_NO_SUITES_DETECTED
    assert "no test suites detected" in result["notice"]
    assert suite in result["notice"]
    # Nothing executed: no failures, no cached verdict, no moved checkout.
    assert result["failures"] == []
    assert result["discarded_failures"] == []
    assert result["cached"] is False
    assert result["state_changed_during_run"] is None
    assert result["scope"] == "full"


class TestRunSuiteZeroSuites:
    """AC1/AC3: ``run_suite`` returns a clean verdict when zero suites exist."""

    def test_resolved_full_suite_with_zero_commands(self, greenfield: Path):
        """``full_suite_commands`` yields nothing → clean verdict, no crash."""
        assert run_tests.full_suite_commands(greenfield) == []

        result = run_tests.run_suite("all", cwd=greenfield, no_cache=True)

        _assert_clean_zero_suite_verdict(result)

    def test_explicit_empty_command_list(self, greenfield: Path):
        """An explicit empty override is the same verdict as zero resolution."""
        result = run_tests.run_suite("all", cwd=greenfield, commands=[], no_cache=True)

        _assert_clean_zero_suite_verdict(result)


class TestRunAllZeroSuites:
    """AC2/AC3: the aggregate and the guarded dev-push path tolerate zero suites."""

    def test_aggregate_zero_suites_is_green(self, greenfield: Path):
        result = run_tests.run_all(("all",), cwd=greenfield, no_cache=True)

        assert result["success"] is True
        assert result["failures"] == []
        assert result["notice_codes"] == [run_tests.NOTICE_NO_SUITES_DETECTED]
        _assert_clean_zero_suite_verdict(result["suites"]["all"])

    def test_guarded_run_all_zero_suites_is_green(self, greenfield: Path):
        """The dev-push gate entry point returns green without skipping."""
        result = run_tests.guarded_run_all(greenfield, suites=("all",), no_cache=True)

        assert result["success"] is True
        assert not result.get("live_repo_mutation")
        _assert_clean_zero_suite_verdict(result["suites"]["all"])


class TestCliZeroSuites:
    """AC1: the CLI exits 0 and reports the no-suites notice in JSON."""

    def test_json_scope_full_exits_zero(
        self, greenfield: Path, capsys: pytest.CaptureFixture[str]
    ):
        """``run_tests.py --scope full --json`` returns 0 on zero suites.

        Driven in-process (``main`` is the CLI entry point): a nested
        ``run_tests.py`` *subprocess* would contend for the host test slot its
        own outer suite holds, which is the documented nested-run deadlock
        (:func:`nested_semaphore_dir`) and not what this AC is about.
        """
        returncode = run_tests.main(
            [
                "--scope",
                "full",
                "--json",
                "--no-cache",
                "--project-root",
                str(greenfield),
            ]
        )

        assert returncode == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        _assert_clean_zero_suite_verdict(payload["suites"]["all"])
