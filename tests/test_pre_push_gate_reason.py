"""Behavioural tests for the pre-push gate's failure classification.

Covers SA-0MVSJX5HS002DJHI AC3: when the full-suite gate fails, ``.githooks/
pre-push`` must consume the runner's machine-readable result and print a
stable ``gate_reason=<code>`` line plus a distinct human diagnostic — no free
test slot, timeout, killed/unparsed run, versus a genuine red suite — instead
of one generic "Full test suite failed". The hook never fails open: every
failure path exits non-zero.

The hook runs end-to-end inside a throwaway git repo with a fake
``skill/test/scripts/run_tests.py`` that emits a canned ``--json`` aggregate on
stdout and a configurable exit code, exactly as the real runner does.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from shared.git_sandbox import fixture_git_env

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, Path(__file__).resolve().parents[2]):
    if (candidate / ".githooks" / "pre-push").exists():
        REPO_ROOT = candidate
        break
HOOK_PATH = REPO_ROOT / ".githooks" / "pre-push"

_FAKE_RUNNER = """#!/usr/bin/env python3
import os
import sys

marker = os.environ.get("RUNNER_MARKER")
if marker:
    with open(marker, "a", encoding="utf-8") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
json_text = os.environ.get("RUNNER_JSON")
if json_text:
    sys.stdout.write(json_text)
stderr_text = os.environ.get("RUNNER_STDERR")
if stderr_text:
    sys.stderr.write(stderr_text)
sys.exit(int(os.environ.get("SUITE_EXIT", "0")))
"""


def _run_hook(
    tmp_path: Path,
    result: dict | None,
    *,
    suite_exit: int = 1,
    runner_stderr: str = "",
    stdin_text: str = "refs/heads/wl-SA-1-x abc123 refs/heads/dev 1111\n",
) -> subprocess.CompletedProcess:
    """Run the pre-push hook with a fake runner emitting *result* as JSON."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(repo)],
        check=True,
        capture_output=True,
        env=fixture_git_env(),
    )
    hook_dst = repo / ".git" / "hooks" / "pre-push"
    hook_dst.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    hook_dst.chmod(0o755)

    runner = repo / "skill" / "test" / "scripts" / "run_tests.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(_FAKE_RUNNER, encoding="utf-8")
    # The hook imports the shared classifier from the runner's directory, just
    # as the installed framework ships both files side by side.
    shutil.copy(
        REPO_ROOT / "skill" / "test" / "scripts" / "gate_reason.py",
        runner.parent / "gate_reason.py",
    )

    env = {
        **fixture_git_env(),
        "CONTEXT_BUDGET_SKIP": "1",
        "WORKLOG_SKIP_PRE_PUSH": "1",
        "BRANCH_POLICY_SKIP": "1",
        "SUITE_EXIT": str(suite_exit),
        "RUNNER_STDERR": runner_stderr,
    }
    if result is not None:
        env["RUNNER_JSON"] = json.dumps(result)

    return subprocess.run(
        ["sh", str(hook_dst)],
        cwd=str(repo),
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# ── Infrastructure conditions get their own reason ──────────────────────────


class TestInfrastructureReasons:
    def test_slot_contention_is_named_and_blocks_the_push(self, tmp_path: Path):
        result = _run_hook(
            tmp_path,
            {
                "success": False,
                "failures": [],
                "notices": [
                    "test concurrency slot busy: no free slot within 600s"
                ],
                "notice_codes": ["slot_busy"],
            },
        )
        assert result.returncode == 1, result.stdout
        assert "gate_reason=slot_contention" in result.stderr, result.stderr
        assert "no free test slot" in result.stderr.lower(), result.stderr
        assert "TEST_LOCK_TIMEOUT" in result.stderr, result.stderr

    def test_timeout_is_named(self, tmp_path: Path):
        result = _run_hook(
            tmp_path,
            {
                "success": False,
                "failures": [],
                "notices": ["suite timed out after 2700s: all"],
                "notice_codes": ["suite_timeout"],
                "suites": {"all": {"returncode": 1}},
            },
        )
        assert result.returncode == 1
        assert "gate_reason=timeout" in result.stderr, result.stderr
        assert "timeoutPerCommand" in result.stderr, result.stderr

    def test_killed_run_is_named(self, tmp_path: Path):
        result = _run_hook(
            tmp_path,
            {
                "success": False,
                "failures": [],
                "notices": ["suite killed by signal 9"],
                "notice_codes": ["killed"],
                "suites": {"node": {"returncode": -9}},
            },
        )
        assert result.returncode == 1
        assert "gate_reason=killed" in result.stderr, result.stderr
        assert "signal-terminated" in result.stderr, result.stderr

    def test_unparsed_run_is_named(self, tmp_path: Path):
        result = _run_hook(
            tmp_path,
            {
                "success": False,
                "failures": [],
                "notices": ["suite exited 3 with no parsable failures"],
                "notice_codes": ["unparsed_failure"],
                "suites": {"all": {"returncode": 3}},
            },
        )
        assert result.returncode == 1
        assert "gate_reason=unparsed" in result.stderr, result.stderr
        assert "no parsable test failures" in result.stderr, result.stderr

    def test_live_repo_mutation_is_infrastructure(self, tmp_path: Path):
        result = _run_hook(
            tmp_path,
            {
                "success": False,
                "failures": [],
                "live_repo_mutation": "live-repo mutation detected: refs/heads/x",
            },
        )
        assert result.returncode == 1
        assert "gate_reason=infrastructure" in result.stderr, result.stderr


# ── A red suite keeps its reason and its message substance ──────────────────


class TestRedSuite:
    def test_red_suite_is_named_test_failure(self, tmp_path: Path):
        result = _run_hook(
            tmp_path,
            {
                "success": False,
                "failures": [{"test_name": "tests/test_x.py::test_y"}],
                "notice_codes": [],
            },
        )
        assert result.returncode == 1
        assert "gate_reason=test_failure" in result.stderr, result.stderr
        # The red-suite message is unchanged in substance.
        assert "Full test suite failed" in result.stderr, result.stderr

    def test_structured_failures_win_over_an_infrastructure_notice(
        self, tmp_path: Path
    ):
        result = _run_hook(
            tmp_path,
            {
                "success": False,
                "failures": [{"test_name": "tests/test_x.py::test_y"}],
                "notices": ["test concurrency slot busy: ..."],
                "notice_codes": ["slot_busy"],
            },
        )
        assert result.returncode == 1
        assert "gate_reason=test_failure" in result.stderr, result.stderr


# ── Unclassifiable failures still fail closed and name a reason ─────────────


class TestUnclassifiable:
    def test_missing_json_still_names_a_reason_and_exits_non_zero(
        self, tmp_path: Path
    ):
        result = _run_hook(
            tmp_path,
            None,
            runner_stderr="something exploded\n",
        )
        assert result.returncode == 1
        assert "gate_reason=unknown" in result.stderr, result.stderr

    def test_gate_subject_refusal_is_infrastructure(self, tmp_path: Path):
        result = _run_hook(
            tmp_path,
            None,
            suite_exit=2,
            runner_stderr=(
                "run_tests: gate-refused pushed commit deadbeef is not present "
                "in this repository.\n"
            ),
        )
        assert result.returncode == 1
        assert "gate_reason=infrastructure" in result.stderr, result.stderr


class TestStateChangedDuringRunClassification:
    """A checkout that moved mid-run is infrastructure, never test_failure."""

    def _flagged_result(self, checkout: str = "/shared/checkout") -> dict:
        """A runner aggregate carrying the state-changed flag AND stale
        failure records — the flag must win (SA-0MW3FF18I003KGZE AC4)."""
        return {
            "success": False,
            "failures": [{"test_name": "mixed_tree_fiche.txt"}],
            "notice_codes": ["state_changed_during_run"],
            "notices": ["the checkout moved while the suite executed"],
            "state_changed_during_run": [
                {
                    "notice_code": "state_changed_during_run",
                    "checkout": checkout,
                    "before_commit": "a" * 40,
                    "after_commit": "b" * 40,
                    "message": "checkout moved mid-run",
                }
            ],
        }

    def test_flagged_result_is_infrastructure_and_names_the_movement(
        self, tmp_path: Path
    ) -> None:
        """The moved checkout is infrastructure with a re-run diagnostic."""
        proc = _run_hook(
            tmp_path,
            self._flagged_result(),
            suite_exit=75,
        )
        assert proc.returncode != 0, "the gate must still block the push"
        assert "gate_reason=infrastructure" in proc.stdout
        # The diagnostic names the checkout movement and the re-run.
        assert "/shared/checkout" in proc.stdout

    def test_stable_red_run_still_classifies_as_test_failure(
        self, tmp_path: Path
    ) -> None:
        """Control: a genuine red run without the flag is still a failure."""
        proc = _run_hook(
            tmp_path,
            {
                "success": False,
                "failures": [{"test_name": "test_real_failure.py::t"}],
                "notice_codes": [],
                "notices": [],
            },
            suite_exit=1,
        )
        assert proc.returncode != 0
        assert "gate_reason=test_failure" in proc.stdout
