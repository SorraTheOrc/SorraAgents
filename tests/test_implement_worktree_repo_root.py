"""Tests for the implement.py worktree repo-root resolution fix.

`implement.py finish` calls ``_remove_worktree()`` which derives the
repository root via ``git rev-parse --show-toplevel`` from the *current
directory*. When the script is run from inside the linked worktree — the
documented workflow — ``--show-toplevel`` resolves to the worktree root, so
safety gate 1 compares the worktree path to itself and refuses to remove it
(RuntimeError), leaving the worktree/branch behind and aborting the finish
phase before push/cleanup/in_review.

These tests verify:

1. ``_get_repo_root()`` returns the MAIN checkout root even when called
   from inside a linked worktree (root-cause fix).
2. ``_remove_worktree()`` removes the worktree when invoked from inside
   the worktree (regression for the reported bug).
3. ``_remove_worktree()`` honors an explicit ``repo_root`` argument
   (defense in depth at the phase_finish call site).
4. ``_remove_worktree()`` still refuses to remove a path that genuinely
   is the repository root, even when called from inside a worktree
   (safety preserved).

Related work item: SA-0MSHQIADG003PBC4
"""

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_runner_script(function_name, args, setup_code=""):
    """Generate a runner script that calls an internal function from implement.py.

    Mirrors the harness in tests/test_implement_abort_safety.py: loads
    implement.py via importlib in a subprocess so internal (underscore)
    functions can be exercised with full control over cwd.
    """
    lines = []
    lines.append("import sys")
    lines.append(f"sys.path.insert(0, {str(_REPO_ROOT)!r})")
    lines.append("")
    lines.append("import importlib.util")
    lines.append("spec = importlib.util.spec_from_file_location(")
    lines.append('    "implement_under_test",')
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


def run_function_test(tmp_path, function_name, args, setup_code=""):
    """Run a helper script that calls an internal function from implement.py."""
    runner_source = _generate_runner_script(function_name, args, setup_code)
    runner_path = tmp_path / "_test_runner.py"
    runner_path.write_text(runner_source)

    proc = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(runner_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc


def _chdir_setup(directory: Path) -> str:
    """Setup code that chdirs the subprocess into *directory*."""
    return f"import os\nos.chdir({str(directory)!r})"


def _result_value(proc: subprocess.CompletedProcess) -> str:
    """Extract the RESULT: line from the runner stdout."""
    result_lines = [l for l in proc.stdout.splitlines() if l.startswith("RESULT:")]
    assert len(result_lines) == 1, (
        f"Expected exactly one RESULT: line, got {result_lines}\n"
        f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    return result_lines[0].replace("RESULT:", "").strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_with_worktree(tmp_path):
    """Create a minimal git repo with a linked worktree.

    Returns dict with:
        - repo_root: Path to the main repo (main working tree)
        - worktree_dir: Path to the linked worktree
    """
    repo_root = tmp_path / "main_repo"
    repo_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(repo_root), check=True, capture_output=True)

    (repo_root / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"],
                   cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "branch", "dev"], cwd=str(repo_root), check=True,
                   capture_output=True)

    worktree_dir = (tmp_path / "test_worktree").resolve()
    subprocess.run(
        ["git", "worktree", "add", "--track", "-b", "wl-test-branch",
         str(worktree_dir), "dev"],
        cwd=str(repo_root), check=True, capture_output=True,
    )

    # Preconditions: worktree has .git as a file, main repo as a directory
    assert (worktree_dir / ".git").is_file(), "worktree .git must be a file"
    assert (repo_root / ".git").is_dir(), "main repo .git must be a directory"

    return {
        "repo_root": repo_root.resolve(),
        "worktree_dir": worktree_dir,
    }


# ---------------------------------------------------------------------------
# Test 1: _get_repo_root() resolves the MAIN root from a linked worktree
# ---------------------------------------------------------------------------


def test_get_repo_root_from_main_checkout_returns_main_root(repo_with_worktree):
    """From the main checkout, _get_repo_root() returns the main root."""
    repo_root = repo_with_worktree["repo_root"]

    proc = run_function_test(
        repo_root,
        "_get_repo_root",
        [],
        setup_code=_chdir_setup(repo_root),
    )
    assert proc.returncode == 0, f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    assert _result_value(proc) == repo_root.as_posix(), (
        f"Expected main root {repo_root.as_posix()}, got {_result_value(proc)}"
    )


def test_get_repo_root_from_inside_worktree_returns_main_root(repo_with_worktree):
    """Regression: _get_repo_root() called from inside a linked worktree must
    return the MAIN checkout root, not the worktree root.
    """
    repo_root = repo_with_worktree["repo_root"]
    worktree_dir = repo_with_worktree["worktree_dir"]

    proc = run_function_test(
        repo_root,
        "_get_repo_root",
        [],
        setup_code=_chdir_setup(worktree_dir),
    )
    assert proc.returncode == 0, f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    assert _result_value(proc) == repo_root.as_posix(), (
        f"Expected main root {repo_root.as_posix()} from inside the worktree, "
        f"got {_result_value(proc)}"
    )


# ---------------------------------------------------------------------------
# Test 2: _remove_worktree() works when run from inside the worktree
# ---------------------------------------------------------------------------


def test_remove_worktree_succeeds_when_run_from_inside_worktree(repo_with_worktree):
    """The reported bug: _remove_worktree() must remove the worktree when the
    current directory is INSIDE the worktree (the documented workflow).
    Before the fix this raised RuntimeError ('REFUSE to remove the
    repository root') because the repo root was derived from cwd.
    """
    repo_root = repo_with_worktree["repo_root"]
    worktree_dir = repo_with_worktree["worktree_dir"]

    setup = textwrap.dedent(f"""\
        import os
        os.chdir({str(worktree_dir)!r})
        import traceback
        try:
            ok = mod._remove_worktree({str(worktree_dir)!r})
            print(f"REMOVED:{{ok}}")
            print(f"EXISTS:{{os.path.exists({str(worktree_dir)!r})}}")
        except RuntimeError as e:
            print(f"RUNTIME_ERROR:{{e}}")
        except Exception as e:
            print(f"OTHER_ERROR:{{type(e).__name__}}:{{e}}")
        # The worktree (our cwd) was removed; return to a valid directory so
        # the harness's trailing function call can run.
        os.chdir({str(repo_root)!r})
    """)

    proc = run_function_test(
        repo_root,
        "_discover_worktree",  # dummy - overridden by setup_code
        ["SA-TEST123"],
        setup_code=setup,
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert "RUNTIME_ERROR" not in proc.stdout, (
        f"_remove_worktree refused from inside the worktree:\n{proc.stdout}"
    )
    assert "REMOVED:True" in proc.stdout, (
        f"Expected worktree removal to succeed:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "EXISTS:False" in proc.stdout, (
        f"Worktree directory should no longer exist:\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Test 3: _remove_worktree() honors an explicit repo_root argument
# ---------------------------------------------------------------------------


def test_remove_worktree_honors_explicit_repo_root(repo_with_worktree):
    """phase_finish passes the authoritative repo root from the implement
    state file; _remove_worktree() must use it rather than re-deriving from
    cwd (defense in depth for the inside-worktree invocation).
    """
    repo_root = repo_with_worktree["repo_root"]
    worktree_dir = repo_with_worktree["worktree_dir"]

    setup = textwrap.dedent(f"""\
        import os
        os.chdir({str(worktree_dir)!r})
        import traceback
        try:
            ok = mod._remove_worktree(
                {str(worktree_dir)!r}, repo_root={str(repo_root)!r}
            )
            print(f"REMOVED:{{ok}}")
        except RuntimeError as e:
            print(f"RUNTIME_ERROR:{{e}}")
        except Exception as e:
            print(f"OTHER_ERROR:{{type(e).__name__}}:{{e}}")
        # The worktree (our cwd) was removed; return to a valid directory so
        # the harness's trailing function call can run.
        os.chdir({str(repo_root)!r})
    """)

    proc = run_function_test(
        repo_root,
        "_discover_worktree",  # dummy - overridden by setup_code
        ["SA-TEST123"],
        setup_code=setup,
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert "REMOVED:True" in proc.stdout, (
        f"Explicit repo_root should allow removal:\n{proc.stdout}\n{proc.stderr}"
    )


def test_remove_worktree_refuses_when_explicit_repo_root_matches_target(
    repo_with_worktree,
):
    """Even with an explicit repo_root, _remove_worktree() must refuse when
    the target genuinely is that repo root.
    """
    repo_root = repo_with_worktree["repo_root"]
    worktree_dir = repo_with_worktree["worktree_dir"]

    setup = textwrap.dedent(f"""\
        import os
        os.chdir({str(worktree_dir)!r})
        import traceback
        try:
            mod._remove_worktree({str(repo_root)!r}, repo_root={str(repo_root)!r})
            print("NO_EXCEPTION")
        except RuntimeError as e:
            print(f"RUNTIME_ERROR:{{e}}")
        except Exception as e:
            print(f"OTHER_ERROR:{{type(e).__name__}}:{{e}}")
    """)

    proc = run_function_test(
        repo_root,
        "_discover_worktree",  # dummy - overridden by setup_code
        ["SA-TEST123"],
        setup_code=setup,
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert "RUNTIME_ERROR" in proc.stdout, (
        f"Expected RuntimeError for genuine repo root:\n{proc.stdout}"
    )
    assert "refuse" in proc.stdout.lower(), (
        f"Error message should contain 'refuse':\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Test 4: safety preserved when target genuinely is the repo root
# ---------------------------------------------------------------------------


def test_remove_worktree_still_refuses_repo_root_from_inside_worktree(
    repo_with_worktree,
):
    """Safety preserved: _remove_worktree() must REFUSE to remove a path that
    genuinely is the repo root even when called from inside the worktree
    (the scenario where the old cwd-derived root was wrong).
    """
    repo_root = repo_with_worktree["repo_root"]
    worktree_dir = repo_with_worktree["worktree_dir"]

    setup = textwrap.dedent(f"""\
        import os
        os.chdir({str(worktree_dir)!r})
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
        "_discover_worktree",  # dummy - overridden by setup_code
        ["SA-TEST123"],
        setup_code=setup,
    )

    print(f"STDOUT: {proc.stdout}")
    print(f"STDERR: {proc.stderr}")

    assert "RUNTIME_ERROR" in proc.stdout, (
        f"Expected RuntimeError for genuine repo root:\n{proc.stdout}"
    )
    assert "refuse" in proc.stdout.lower(), (
        f"Error message should contain 'refuse':\n{proc.stdout}"
    )
