"""Behavioral tests for the pre-push hook's scope-aware test gate.

Covers SA-0MT6CEN8D0073F61: the hook runs the FULL suite (``run_tests.py
--scope full``) only when pushing to ``refs/heads/dev`` or
``refs/heads/main``; feature-branch pushes skip test execution (they exit
before the worklog sync), and ``TEST_SCOPE_SKIP=1`` bypasses the gate.

The hook is executed end-to-end inside a throwaway git repo with a fake
``skill/test/scripts/run_tests.py`` that records how it was invoked and
exits with a configurable code.

The pushed commit is the gate's subject (SA-0MVTRBT50005DUEH): the hook
forwards ``<local_sha>`` as ``--gate-commit``, surfaces the runner's
``gate-`` provenance/refusal lines, and mirrors the cache from the checkout
the gate actually resolved.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from shared.git_sandbox import fixture_git_env, init_repo, run_git

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
stderr_file = os.environ.get("RUNNER_STDERR_FILE")
if stderr_file and os.path.exists(stderr_file):
    with open(stderr_file, encoding="utf-8") as f:
        sys.stderr.write(f.read())
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
            ["git", "init", "-q", str(repo)],
            check=True,
            capture_output=True,
            env=fixture_git_env(),
        )
        git_hooks = repo / ".git" / "hooks"
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@t"],
            check=True,
            capture_output=True,
            env=fixture_git_env(),
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "t"],
            check=True,
            capture_output=True,
            env=fixture_git_env(),
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
        **fixture_git_env(),
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
        subprocess.run(
            ["git", "init", "-q", str(repo)],
            check=True,
            capture_output=True,
            env=fixture_git_env(),
        )
        git_hooks = repo / ".git" / "hooks"
        hook_dst = git_hooks / "pre-push"
        hook_dst.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        hook_dst.chmod(0o755)
        # NOTE: no skill/test/scripts/run_tests.py in this repo.

        env = {
            **fixture_git_env(),
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

# ── The pushed commit is the gate's subject (SA-0MVTRBT50005DUEH) ───────────


class TestPushedCommitForwarded:
    def test_dev_push_forwards_local_sha_as_gate_commit(self, tmp_path: Path):
        """AC1: the pushed <local_sha> reaches the suite gate."""
        result, marker = _run_hook(
            tmp_path, "refs/heads/wl-SA-1-x abc1234 refs/heads/dev 1111\n"
        )
        assert result.returncode == 0, result.stderr
        argv = marker.read_text()
        assert "--gate-commit abc1234" in argv, argv

    def test_deletion_push_forwards_no_commit(self, tmp_path: Path):
        """A deletion carries the all-zero sha — there is no commit to gate."""
        zeros = "0" * 40
        result, marker = _run_hook(
            tmp_path, f"refs/heads/wl-SA-1-x {zeros} refs/heads/dev 1111\n"
        )
        assert result.returncode == 0, result.stderr
        assert "--gate-commit" not in marker.read_text()

    def test_conflicting_dev_and_main_shas_refuse_and_run_nothing(
        self, tmp_path: Path
    ):
        """AC4: one suite cannot evidence two pushed commits — refuse."""
        result, marker = _run_hook(
            tmp_path,
            "refs/heads/wl-a aaaa refs/heads/dev 1111\n"
            "refs/heads/wl-b bbbb refs/heads/main 2222\n",
        )
        assert result.returncode == 1
        assert not marker.exists(), "the suite must not run for an ambiguous subject"
        assert "aaaa" in result.stderr and "bbbb" in result.stderr
        assert "TEST_SCOPE_SKIP=1" in result.stderr


class TestGateProvenanceSurfaced:
    def test_gate_provenance_is_echoed_on_the_hook_stderr(self, tmp_path: Path):
        """AC3: the state the gate validated is attributable on the push."""
        lines = tmp_path / "runner-stderr.txt"
        lines.write_text(
            "run_tests: gate-subject pushed=abc checkout=worktree "
            "fingerprint_kind=worktree\n"
            "run_tests: gate-root /tmp/gate-wt\n"
            "run_tests: gate-verdict verdict=cached sha=abc "
            "git_state=0123456789abcdef entries=1 cached=1\n"
        )
        result, _ = _run_hook(
            tmp_path,
            "refs/heads/wl-SA-1-x abc refs/heads/dev 1111\n",
            env_overrides={"RUNNER_STDERR_FILE": str(lines)},
        )
        assert result.returncode == 0, result.stderr
        assert "run_tests: gate-subject" in result.stderr
        assert "run_tests: gate-verdict verdict=cached" in result.stderr
        assert "git_state=0123456789abcdef" in result.stderr

    def test_gate_refusal_is_surfaced_when_the_gate_fails(self, tmp_path: Path):
        """AC4: the refusal message survives the hook's output suppression."""
        lines = tmp_path / "runner-stderr.txt"
        lines.write_text(
            "run_tests: gate-refused pushed commit deadbeef is not present in "
            "this repository. Set TEST_SCOPE_SKIP=1 to bypass.\n"
        )
        result, _ = _run_hook(
            tmp_path,
            "refs/heads/wl-SA-1-x deadbeef refs/heads/dev 1111\n",
            suite_exit=2,
            env_overrides={"RUNNER_STDERR_FILE": str(lines)},
        )
        assert result.returncode == 1
        assert "run_tests: gate-refused" in result.stderr
        assert "TEST_SCOPE_SKIP=1" in result.stderr

    def test_mirrors_the_cache_of_the_resolved_gate_root(self, tmp_path: Path):
        """AC3/Q7: entries of the checkout the gate resolved reach the main cache.

        The gate may run in a linked worktree (the pusher's tree). Its cache is
        per repo root, so the hook mirrors the resolved root's entries into the
        main checkout's cache, where post-push verification and audit F3 read.
        """
        repo = init_repo(tmp_path / "repo")
        (repo / "file.txt").write_text("one\n")
        run_git(repo, ["add", "-A"], check=True)
        run_git(repo, ["commit", "-q", "-m", "init"], check=True)
        main_cache = repo / ".worklog" / "cache"
        main_cache.mkdir(parents=True)

        worktree = tmp_path / "gate-wt"
        run_git(repo, ["worktree", "add", "--detach", str(worktree), "HEAD"], check=True)
        entry = worktree / ".worklog" / "cache" / "deadbeefcafe"
        entry.mkdir(parents=True)
        (entry / "meta.json").write_text("{}")

        runner = worktree / "skill" / "test" / "scripts" / "run_tests.py"
        runner.parent.mkdir(parents=True)
        runner.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('run_tests: gate-subject pushed=abc checkout=worktree',"
            " file=sys.stderr)\n"
            f"print('run_tests: gate-root {worktree}', file=sys.stderr)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        hook = tmp_path / "pre-push"
        hook.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        hook.chmod(0o755)

        env = {
            **fixture_git_env(),
            "CONTEXT_BUDGET_SKIP": "1",
            "WORKLOG_SKIP_PRE_PUSH": "1",
            "BRANCH_POLICY_SKIP": "1",
        }
        result = subprocess.run(
            ["sh", str(hook)],
            cwd=str(worktree),
            input="refs/heads/wl-SA-1-x abc refs/heads/dev 1111\n",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (main_cache / "deadbeefcafe").is_dir(), (
            f"expected the resolved root's cache entry mirrored; stderr={result.stderr!r}"
        )
