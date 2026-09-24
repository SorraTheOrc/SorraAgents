"""Behavioral tests for the pre-push hook's scope-aware test gate.

Covers SA-0MT6CEN8D0073F61: the hook runs the FULL suite (``run_tests.py
--scope full``) only when pushing to ``refs/heads/dev`` or
``refs/heads/main``; feature-branch pushes skip test execution (they exit
before the worklog sync), and ``TEST_SCOPE_SKIP=1`` bypasses the gate.

The hook is executed end-to-end inside a throwaway git repo with a fake
``skill/test/scripts/run_tests.py`` that records how it was invoked and
exits with a configurable code.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
sys.exit(int(os.environ.get("SUITE_EXIT", "0")))
"""


def _run_hook(
    tmp_path: Path,
    stdin_text: str,
    with_git_repo: bool = True,
    suite_exit: int = 0,
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Run the pre-push hook inside a temp git repo with a fake runner.

    Returns ``(result, marker)`` where *marker* is the fake runner's
    invocation record (absent when the runner was never invoked).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "runner-marker.txt"

    git_hooks = repo / ".git" / "hooks"
    if with_git_repo:
        subprocess.run(
            ["git", "init", "-q", str(repo)], check=True, capture_output=True
        )
        git_hooks = repo / ".git" / "hooks"
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@t"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "t"],
            check=True, capture_output=True,
        )
    else:
        git_hooks.mkdir(parents=True, exist_ok=True)

    hook_dst = git_hooks / "pre-push"
    hook_dst.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    hook_dst.chmod(0o755)

    runner = repo / "skill" / "test" / "scripts" / "run_tests.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(_FAKE_RUNNER, encoding="utf-8")

    env = {
        **os.environ,
        "CONTEXT_BUDGET_SKIP": "1",
        "WORKLOG_SKIP_PRE_PUSH": "1",
        "BRANCH_POLICY_SKIP": "1",
        "RUNNER_MARKER": str(marker),
        "SUITE_EXIT": str(suite_exit),
    }
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        ["sh", str(hook_dst)],
        cwd=str(repo),
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, marker


# ── AC1/AC2: dev and main pushes run the full suite ─────────────────────────


class TestDevMainPushRunsFullSuite:
    def test_dev_push_runs_full_scope(self, tmp_path: Path):
        """Pushing to refs/heads/dev must invoke run_tests.py --scope full."""
        result, marker = _run_hook(
            tmp_path, "refs/heads/wl-SA-1-x 0000 refs/heads/dev 1111\n"
        )
        assert result.returncode == 0, result.stderr
        assert "full" in marker.read_text(), (
            f"expected --scope full invocation, got {marker.read_text()!r}"
        )

    def test_main_push_runs_full_scope(self, tmp_path: Path):
        """Pushing to refs/heads/main must invoke run_tests.py --scope full."""
        result, marker = _run_hook(
            tmp_path, "refs/heads/dev 0000 refs/heads/main 1111\n"
        )
        assert result.returncode == 0, result.stderr
        assert "full" in marker.read_text()

    def test_multi_ref_push_with_dev_target_runs_suite(self, tmp_path: Path):
        """A push containing a dev ref among others still runs the gate."""
        result, marker = _run_hook(
            tmp_path,
            "refs/heads/wl-a 0000 refs/heads/feature-x 1111\n"
            "refs/heads/wl-b 0000 refs/heads/dev 2222\n",
        )
        assert result.returncode == 0, result.stderr
        assert "full" in marker.read_text()

    def test_suite_failure_blocks_dev_push(self, tmp_path: Path):
        """AC4: a failing suite must block the push with a clear message."""
        result, _ = _run_hook(
            tmp_path,
            "refs/heads/wl-SA-1-x 0000 refs/heads/dev 1111\n",
            suite_exit=1,
        )
        assert result.returncode == 1
        assert "Full test suite failed" in result.stderr


# ── AC3: feature-branch pushes skip tests and exit early ────────────────────


class TestFeaturePushSkipsTests:
    def test_feature_push_does_not_run_suite(self, tmp_path: Path):
        """A non-dev/main push must not invoke run_tests.py at all."""
        result, marker = _run_hook(
            tmp_path, "refs/heads/wl-SA-1-x 0000 refs/heads/wl-SA-1-x 1111\n"
        )
        assert result.returncode == 0, result.stderr
        assert not marker.exists(), "runner must not be invoked for feature pushes"

    def test_empty_stdin_skips_suite(self, tmp_path: Path):
        """No pushed refs (e.g. mirrored setups) must not invoke the suite."""
        result, marker = _run_hook(tmp_path, "")
        assert result.returncode == 0, result.stderr
        assert not marker.exists()


# ── TEST_SCOPE_SKIP bypass + fail-closed when runner missing ────────────────


class TestBypassAndFailClosed:
    def test_test_scope_skip_bypasses_gate(self, tmp_path: Path):
        """TEST_SCOPE_SKIP=1 must skip the suite even for dev pushes."""
        result, marker = _run_hook(
            tmp_path,
            "refs/heads/wl-SA-1-x 0000 refs/heads/dev 1111\n",
            env_overrides={"TEST_SCOPE_SKIP": "1"},
        )
        assert result.returncode == 0, result.stderr
        assert not marker.exists()

    def test_missing_runner_fails_closed(self, tmp_path: Path):
        """A dev push without run_tests.py must fail closed, not silently pass."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True,
                       capture_output=True)
        git_hooks = repo / ".git" / "hooks"
        hook_dst = git_hooks / "pre-push"
        hook_dst.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        hook_dst.chmod(0o755)
        # NOTE: no skill/test/scripts/run_tests.py in this repo.

        env = {
            **os.environ,
            "CONTEXT_BUDGET_SKIP": "1",
            "WORKLOG_SKIP_PRE_PUSH": "1",
            "BRANCH_POLICY_SKIP": "1",
        }
        result = subprocess.run(
            ["sh", str(hook_dst)],
            cwd=str(repo),
            input="refs/heads/wl-SA-1-x 0000 refs/heads/dev 1111\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr