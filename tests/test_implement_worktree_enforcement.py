"""Tests for the implement.py worktree placement enforcement gate.

Regression: agents frequently do implementation work outside git worktrees.
`implement.py finish` must refuse (non-zero exit) when it is run from
outside the worktree while the main checkout holds uncommitted changes,
instead of silently building/testing/committing an empty worktree and
marking the item in_review (which stranded the agent's changes in the
main checkout).

This test suite exercises `_worktree_placement_violation()` directly:

1. Violation detected: cwd outside worktree + dirty main checkout + empty worktree.
2. No violation: cwd inside the worktree (even if main checkout is dirty).
3. No violation: cwd outside worktree but main checkout is clean.
4. No violation: main checkout dirty only inside .worklog/.
5. No violation: main checkout dirty with unrelated changes but the work
   lives in the worktree (the gate must not block legitimate finishes).

Related work item: SA-0MSGKAWXQ009VVG2
"""

import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_violation_check(
    worktree_path: Path,
    cwd: Path,
    repo_root: Path,
    runner_dir: Path,
) -> subprocess.CompletedProcess:
    """Run _worktree_placement_violation() in a subprocess.

    Args:
        worktree_path: Absolute path to the worktree.
        cwd: Directory to report as the current directory.
        repo_root: Absolute path to the main repo root.
        runner_dir: Scratch directory OUTSIDE the repos for the runner
            script (so it never dirties the checkout under test).

    Returns:
        subprocess.CompletedProcess whose stdout contains a RESULT: line
        (either ``RESULT:None`` or ``RESULT:<message>``).
    """
    runner_source = f"""\
import sys
sys.path.insert(0, {str(_REPO_ROOT)!r})
import importlib.util
spec = importlib.util.spec_from_file_location(
    "implement_under_test",
    {str(_IMPLEMENT_PY)!r},
)
mod = importlib.util.module_from_spec(spec)
mod.__package__ = "implement_scripts"
sys.modules["implement_under_test"] = mod
spec.loader.exec_module(mod)

result = mod._worktree_placement_violation(
    {str(worktree_path)!r}, cwd={str(cwd)!r}, repo_root={str(repo_root)!r}
)
if result is None:
    print("RESULT:None")
else:
    print("RESULT:" + result)
"""
    runner_dir.mkdir(parents=True, exist_ok=True)
    runner_path = runner_dir / "_test_worktree_gate.py"
    runner_path.write_text(runner_source)
    return subprocess.run(  # noqa: PLW1510
        [sys.executable, str(runner_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _result_of(proc: subprocess.CompletedProcess) -> str:
    """Extract the RESULT: value from the subprocess output."""
    stdout_lines = proc.stdout.strip().splitlines()
    result_lines = [l for l in stdout_lines if l.startswith("RESULT:")]
    assert len(result_lines) == 1, (
        f"Expected exactly one RESULT: line, got {result_lines}\n"
        f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    return result_lines[0].replace("RESULT:", "").strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def worktree_env(tmp_path):
    """Create a minimal git repo (dev branch) with a worktree.

    Returns dict with:
        - repo_root: Path to the main repo
        - worktree_dir: Path to the worktree
    """
    repo_root = tmp_path / "main_repo"
    repo_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(repo_root), check=True, capture_output=True)

    # Create an initial commit
    (repo_root / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"],
                   cwd=str(repo_root), check=True, capture_output=True)

    # Create dev branch
    subprocess.run(["git", "branch", "dev"], cwd=str(repo_root), check=True,
                   capture_output=True)

    # Create a worktree from dev
    worktree_dir = tmp_path / "test_worktree"
    worktree_dir = worktree_dir.resolve()
    subprocess.run(
        ["git", "worktree", "add", "--track", "-b",
         "wl-test-branch", str(worktree_dir), "dev"],
        cwd=str(repo_root), check=True, capture_output=True,
    )

    return {
        "repo_root": repo_root.resolve(),
        "worktree_dir": worktree_dir,
    }


# ---------------------------------------------------------------------------
# Test 1: Violation detected — dirty main checkout, cwd outside worktree
# ---------------------------------------------------------------------------


def test_gate_detects_work_done_in_main_checkout(worktree_env, tmp_path):
    """finish must refuse when changes were made in the main checkout
    instead of the worktree.
    """
    repo_root = worktree_env["repo_root"]
    worktree_dir = worktree_env["worktree_dir"]

    # Simulate the regression: implement in the main checkout (not the worktree)
    (repo_root / "README.md").write_text("# Test\n# dirty change\n")

    proc = _run_violation_check(worktree_dir, repo_root, repo_root, tmp_path)

    assert proc.returncode == 0, (
        f"Script failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    result = _result_of(proc)
    assert result != "None", (
        "Expected a placement violation message, got None.\n"
        f"STDOUT:{proc.stdout}"
    )
    assert "worktree" in result.lower()
    assert "main checkout" in result.lower()
    assert "implement.py finish" in result


# ---------------------------------------------------------------------------
# Test 2: No violation — cwd inside the worktree
# ---------------------------------------------------------------------------


def test_gate_allows_finish_from_inside_worktree(worktree_env, tmp_path):
    """finish run from inside the worktree must pass even when the main
    checkout is dirty (work may be unrelated).
    """
    repo_root = worktree_env["repo_root"]
    worktree_dir = worktree_env["worktree_dir"]

    # Dirty main checkout, but finish is run from inside the worktree
    (repo_root / "README.md").write_text("# Test\n# dirty change\n")
    (worktree_dir / "code.py").write_text("x = 1\n")

    proc = _run_violation_check(worktree_dir, worktree_dir, repo_root, tmp_path)

    assert proc.returncode == 0, (
        f"Script failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    assert _result_of(proc) == "None", (
        "Expected no violation when running from inside the worktree.\n"
        f"STDOUT:{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Test 3: No violation — main checkout clean, work lives in the worktree
# ---------------------------------------------------------------------------


def test_gate_allows_finish_from_clean_main_checkout(worktree_env, tmp_path):
    """finish run from the main checkout must pass when the main checkout
    is clean and the changes live in the worktree.
    """
    repo_root = worktree_env["repo_root"]
    worktree_dir = worktree_env["worktree_dir"]

    # Work is in the worktree; main checkout stays clean
    (worktree_dir / "code.py").write_text("x = 1\n")

    proc = _run_violation_check(worktree_dir, repo_root, repo_root, tmp_path)

    assert proc.returncode == 0, (
        f"Script failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    assert _result_of(proc) == "None", (
        "Expected no violation when the main checkout is clean.\n"
        f"STDOUT:{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Test 4: No violation — main checkout dirty only in .worklog/
# ---------------------------------------------------------------------------


def test_gate_ignores_worklog_changes_in_main_checkout(worktree_env, tmp_path):
    """Routine .worklog/ churn in the main checkout must not trigger the gate."""
    repo_root = worktree_env["repo_root"]
    worktree_dir = worktree_env["worktree_dir"]

    worklog_dir = repo_root / ".worklog"
    worklog_dir.mkdir(exist_ok=True)
    (worklog_dir / "worklog-data.jsonl").write_text('{"id": "SA-TEST"}\n')

    proc = _run_violation_check(worktree_dir, repo_root, repo_root, tmp_path)

    assert proc.returncode == 0, (
        f"Script failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    assert _result_of(proc) == "None", (
        "Expected no violation for .worklog-only changes.\n"
        f"STDOUT:{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Test 5: No violation — work lives in the worktree despite dirty main checkout
# ---------------------------------------------------------------------------


def test_gate_allows_finish_when_work_is_in_worktree_despite_dirty_main(
    worktree_env, tmp_path
):
    """Unrelated pre-existing dirt in the main checkout must not block a
    legitimate finish when the implementation work is in the worktree.
    """
    repo_root = worktree_env["repo_root"]
    worktree_dir = worktree_env["worktree_dir"]

    # Main checkout has unrelated uncommitted changes; the work is in the
    # worktree (uncommitted file there).
    (repo_root / "README.md").write_text("# Test\n# unrelated dirt\n")
    (worktree_dir / "code.py").write_text("x = 1\n")

    proc = _run_violation_check(worktree_dir, repo_root, repo_root, tmp_path)

    assert proc.returncode == 0, (
        f"Script failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    assert _result_of(proc) == "None", (
        "Expected no violation when the work is in the worktree.\n"
        f"STDOUT:{proc.stdout}"
    )
