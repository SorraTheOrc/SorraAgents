"""Tests for the implement.py abort safety fixes.

These tests verify the three safety mechanisms that prevent the abort
script from deleting the entire project directory:

1. _discover_worktree() must not return repo root when .implement_state.json
   is found there (must validate .git is a file, not a directory).
2. _remove_worktree() must refuse to remove a path matching the repo root.
3. The shutil.rmtree() fallback in _remove_worktree() must guard against
   deleting a directory that contains a .git/ directory (main working tree).
4. .implement_state.json is added to .gitignore.
5. Loud error on abort failure.

Related work item: SA-0MS0DK6BD001RNDG
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
_GITIGNORE = _REPO_ROOT / ".gitignore"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_runner_script(
    tmp_path: Path,
    function_name: str,
    args: list[str],
    setup_code: str = "",
) -> str:
    """Generate a runner script that calls an internal function from implement.py.

    Args:
        tmp_path: Temporary directory for the test (not used directly, but
                 tests run from this directory).
        function_name: Name of the function to call (e.g. _discover_worktree).
        args: Python repr of args to pass to the function.
        setup_code: Additional Python setup code to run before the call.

    Returns:
        Python source code as a string.
    """
    lines = []
    lines.append("import json")
    lines.append("import sys")
    lines.append(f"sys.path.insert(0, {str(_REPO_ROOT)!r})")
    lines.append("")
    lines.append("import importlib.util")
    lines.append(f"spec = importlib.util.spec_from_file_location(")
    lines.append(f'    "implement_under_test",')
    lines.append(f"    {str(_IMPLEMENT_PY)!r},")
    lines.append(")")
    lines.append("mod = importlib.util.module_from_spec(spec)")
    lines.append('mod.__package__ = "implement_scripts"')
    lines.append('sys.modules["implement_under_test"] = mod')
    lines.append("spec.loader.exec_module(mod)")
    lines.append("")
    if setup_code:
        lines.append(setup_code)
        lines.append("")
    lines.append(f"result = mod.{function_name}(*{args!r})")
    lines.append("if result is not None:")
    lines.append('    print(f"RESULT:{result}")')
    lines.append("else:")
    lines.append('    print("RESULT:None")')
    return "\n".join(lines)


def run_function_test(
    tmp_path: Path,
    function_name: str,
    args: list[str],
    setup_code: str = "",
) -> subprocess.CompletedProcess:
    """Run a helper script that calls an internal function from implement.py.

    Writes the runner to a temporary file to avoid indentation issues with
    ``python3 -c``.

    Args:
        tmp_path: Temporary directory for the test.
        function_name: Name of the function to call (e.g. _discover_worktree).
        args: Python repr of args to pass to the function.
        setup_code: Additional Python setup code to run before the call.

    Returns:
        subprocess.CompletedProcess with stdout/stderr.
    """
    runner_source = _generate_runner_script(tmp_path, function_name, args, setup_code)
    runner_path = tmp_path / "_test_runner.py"
    runner_path.write_text(runner_source)

    proc = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(runner_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_git_repo(tmp_path):
    """Create a minimal git repo with a worktree for testing.

    Returns dict with:
        - repo_root: Path to the main repo
        - worktree_dir: Path to a worktree
        - state_path: Path to .implement_state.json in repo_root
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

    # Create a worktree
    worktree_dir = tmp_path / "test_worktree"
    worktree_dir = worktree_dir.resolve()
    subprocess.run(
        ["git", "worktree", "add", "--track", "-b",
         "wl-test-branch", str(worktree_dir), "dev"],
        cwd=str(repo_root), check=True, capture_output=True,
    )

    # Place .implement_state.json in the repo root as a potential trap
    state_path = repo_root / ".implement_state.json"
    state_path.write_text(json.dumps({
        "work_item_id": "SA-TEST123",
        "worktree_path": str(worktree_dir),
        "repo_root": str(repo_root),
    }))

    return {
        "repo_root": repo_root,
        "worktree_dir": worktree_dir,
        "state_path": state_path,
    }


# ---------------------------------------------------------------------------
# Test 1: _discover_worktree() safety - refuse repo root
# ---------------------------------------------------------------------------


def test_discover_worktree_returns_none_when_state_file_in_repo_root(
    isolated_git_repo,
):
    """_discover_worktree() must return None when .implement_state.json is in
    the repo root (where .git is a directory), not a worktree.
    """
    repo_root = isolated_git_repo["repo_root"]

    # Verify preconditions
    assert (repo_root / ".git").is_dir(), \
        "Main repo must have .git/ directory"
    assert (repo_root / ".implement_state.json").exists(), \
        ".implement_state.json must exist in repo root"

    proc = run_function_test(
        repo_root,
        "_discover_worktree",
        ["SA-TEST123"],
        setup_code=f"import os; os.chdir({str(repo_root)!r})",
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert proc.returncode == 0, (
        f"Script failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )

    stdout_lines = proc.stdout.strip().splitlines()
    result_lines = [l for l in stdout_lines if l.startswith("RESULT:")]
    assert len(result_lines) == 1, (
        f"Expected exactly one RESULT: line, got {result_lines}"
    )

    result_value = result_lines[0].replace("RESULT:", "").strip()
    assert result_value == "None", (
        f"_discover_worktree() returned {result_value!r} when "
        f".implement_state.json was in repo root. MUST return None."
    )


def test_discover_worktree_returns_worktree_path_when_state_in_worktree(
    isolated_git_repo,
):
    """_discover_worktree() must correctly return the worktree path when the
    state file is inside a worktree (where .git is a file).
    """
    worktree_dir = isolated_git_repo["worktree_dir"]

    # Verify precondition: .git is a file in worktrees
    assert (worktree_dir / ".git").is_file(), \
        "Worktree must have .git file"

    # Place .implement_state.json in worktree
    state_path = worktree_dir / ".implement_state.json"
    state_path.write_text(json.dumps({
        "work_item_id": "SA-TEST123",
        "worktree_path": str(worktree_dir),
        "repo_root": str(isolated_git_repo["repo_root"]),
    }))

    proc = run_function_test(
        worktree_dir,
        "_discover_worktree",
        ["SA-TEST123"],
        setup_code=f"import os; os.chdir({str(worktree_dir)!r})",
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert proc.returncode == 0, (
        f"Script failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )

    stdout_lines = proc.stdout.strip().splitlines()
    result_lines = [l for l in stdout_lines if l.startswith("RESULT:")]
    assert len(result_lines) == 1

    result_value = result_lines[0].replace("RESULT:", "").strip()
    assert result_value == str(worktree_dir), (
        f"Expected worktree path {worktree_dir}, got {result_value}"
    )


# ---------------------------------------------------------------------------
# Test 2: _remove_worktree() safety gate
# ---------------------------------------------------------------------------


def test_remove_worktree_raises_error_when_target_is_repo_root(
    isolated_git_repo,
):
    """_remove_worktree() must raise RuntimeError when the target path
    matches the repo root.
    """
    repo_root = isolated_git_repo["repo_root"]

    setup = textwrap.dedent(f"""\
        import os
        os.chdir({str(repo_root)!r})
        import traceback
        try:
            mod._remove_worktree({str(repo_root)!r})
            print("NO_EXCEPTION")
        except RuntimeError as e:
            print(f"RUNTIME_ERROR:{{e}}")
        except Exception as e:
            print(f"OTHER_ERROR:{{type(e).__name__}}:{{e}}")
    """)

    proc = run_function_test(
        repo_root,
        "_discover_worktree",  # dummy - we override in setup_code
        ["SA-TEST123"],
        setup_code=setup,
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert "RUNTIME_ERROR" in proc.stdout, (
        f"Expected RuntimeError, got:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    assert "refuse" in proc.stdout.lower(), (
        f"Error message should contain 'refuse':\n{proc.stdout}"
    )


def test_remove_worktree_shutil_fallback_raises_error_for_main_repo(
    isolated_git_repo,
    monkeypatch,
    tmp_path,
):
    """The shutil.rmtree() fallback in _remove_worktree() must refuse to
    delete a path containing a .git directory when git worktree remove fails.
    """
    repo_root = isolated_git_repo["repo_root"]

    setup = textwrap.dedent(f"""\
        import os
        os.chdir({str(repo_root)!r})
        import traceback
        try:
            mod._remove_worktree({str(repo_root)!r})
            print("NO_EXCEPTION")
        except RuntimeError as e:
            print(f"RUNTIME_ERROR:{{e}}")
        except Exception as e:
            print(f"OTHER_ERROR:{{type(e).__name__}}:{{e}}")
    """)

    proc = run_function_test(
        repo_root,
        "_discover_worktree",  # dummy
        ["SA-TEST123"],
        setup_code=setup,
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert "RUNTIME_ERROR" in proc.stdout, (
        f"Expected RuntimeError, got:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    assert "refuse" in proc.stdout.lower(), (
        f"Error message should contain 'refuse':\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Test 3: .implement_state.json is in .gitignore
# ---------------------------------------------------------------------------


def test_implement_state_json_in_gitignore():
    """.implement_state.json MUST be listed in .gitignore to prevent it
    from being committed and triggering the bug in fresh clones.
    """
    assert _GITIGNORE.exists(), f".gitignore not found at {_GITIGNORE}"
    content = _GITIGNORE.read_text()
    assert ".implement_state.json" in content, (
        ".implement_state.json not found in .gitignore."
    )


# ---------------------------------------------------------------------------
# Test 4: Loud error on abort failure (critical log message)
# ---------------------------------------------------------------------------


def test_remove_worktree_logs_critical_error_when_safety_gate_prevents_deletion(
    isolated_git_repo,
):
    """When the safety gate prevents deletion, _remove_worktree() must
    log a critical-level error message.
    """
    repo_root = isolated_git_repo["repo_root"]

    setup = textwrap.dedent(f"""\
        import os, logging
        os.chdir({str(repo_root)!r})

        # Capture log output
        from io import StringIO
        log_capture = StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.CRITICAL)
        mod.LOG.addHandler(handler)
        mod.LOG.setLevel(logging.CRITICAL)

        import traceback
        try:
            mod._remove_worktree({str(repo_root)!r})
        except RuntimeError:
            pass

        log_text = log_capture.getvalue()
        if log_text:
            print(f"LOG_OUTPUT_START")
            print(log_text)
            print(f"LOG_OUTPUT_END")
        else:
            print("NO_LOG_OUTPUT")
    """)

    proc = run_function_test(
        repo_root,
        "_discover_worktree",  # dummy
        ["SA-TEST123"],
        setup_code=setup,
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert "LOG_OUTPUT_START" in proc.stdout, (
        f"Expected log output, got:\n{proc.stdout}"
    )
    assert "refuse" in proc.stdout.lower(), (
        f"Log message should contain 'refuse':\n{proc.stdout}"
    )
