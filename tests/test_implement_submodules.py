"""Tests for the implement.py git submodule initialisation (SA-0MSN52GGN002B0AZ).

Contract (per work item ACs):

- AC1: ``implement.py start`` runs ``git submodule update --init --recursive``
  in the new worktree after worktree creation succeeds.
- AC2: If the submodule command fails, ``implement.py start`` logs a warning
  (via ``LOG.warning``) but does **not** abort — start continues and the
  worktree is usable.
- AC3: A dedicated helper function ``_ensure_submodules(worktree_path,
  repo_root)`` exists at call site, following the same pattern as the
  existing ``_ensure_node_modules_symlink()`` helper.
- AC4: Unit tests cover:
    (a) submodules are initialised on a fresh worktree,
    (b) submodule initialisation failure does not abort start,
    (c) repos without submodules are unaffected.
- AC5: ``implement/SKILL.md`` documents the submodule behaviour.
- AC6: Full project test suite passes with the new changes.

Background: ``git worktree add`` does **not** automatically initialize
submodules.  This helper runs ``git submodule update --init --recursive``
best-effort only — failures produce a warning but never abort start.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"

_FAKE_WORK_ITEM_ID = "SA-TEST456"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, with_submodules: bool = False) -> tuple[Path, Path]:
    """Create a minimal git repo with a dev branch and optionally .gitmodules.

    Returns:
        (repo_root, worktree_dir) — worktree_dir does not exist yet.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(repo_root), check=True, capture_output=True)
    (repo_root / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"],
                   cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "branch", "dev"], cwd=str(repo_root), check=True, capture_output=True)

    if with_submodules:
        # Create a .gitmodules file (simulating a repo that declares submodules)
        gitmodules_content = textwrap.dedent("""\
            [submodule "test-sub"]
                path = test-sub
                url = https://example.com/test.git
        """)
        (repo_root / ".gitmodules").write_text(gitmodules_content)

    worktree_dir = (tmp_path / "wt").resolve()
    return repo_root, worktree_dir


def _git_worktree_add(repo_root: Path, worktree_dir: Path) -> None:
    """Create a real git worktree inside the test repo."""
    subprocess.run(
        ["git", "worktree", "add", "--track", "-b", "wl-test",
         str(worktree_dir), "dev"],
        cwd=str(repo_root), check=True, capture_output=True,
    )


def _module_loader(repo_root: Path) -> str:
    """Python preamble that loads implement.py as ``mod`` with cwd=repo_root."""
    return textwrap.dedent(f"""\
        import json
        import sys, os
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        os.chdir({str(repo_root)!r})
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "implement_under_test", {str(_IMPLEMENT_PY)!r},
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "implement_scripts"
        sys.modules["implement_under_test"] = mod
        spec.loader.exec_module(mod)
    """)


def _run_in_subprocess(tmp_path: Path, runner_source: str) -> subprocess.CompletedProcess:
    """Run a snippet against the implement module in an isolated subprocess."""
    runner_path = tmp_path / "_test_runner.py"
    runner_path.write_text(runner_source)
    proc = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(runner_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc


def _assert_line_in(proc: subprocess.CompletedProcess, prefix: str, value: str) -> None:
    """Assert that stdout contains a ``PREFIX:value`` line."""
    for line in proc.stdout.splitlines():
        if line.startswith(prefix):
            assert line[len(prefix):] == value, (
                f"Expected {prefix}{value!r}, got {line!r}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
            return
    raise AssertionError(
        f"No {prefix} line in stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Unit tests: _ensure_submodules()
# ---------------------------------------------------------------------------


class TestSubmoduleHelper:
    def test_no_op_when_no_gitmodules(self, tmp_path):
        """Repo without .gitmodules → skip silently, no crash."""
        repo, wt = _make_repo(tmp_path, with_submodules=False)
        _git_worktree_add(repo, wt)

        runner = _module_loader(repo) + textwrap.dedent(f"""\
            result = mod._ensure_submodules({str(wt)!r}, {str(repo)!r})
            print(f"RESULT:{{result}}")
        """)
        proc = _run_in_subprocess(tmp_path, runner)
        assert proc.returncode == 0, proc.stderr
        _assert_line_in(proc, "RESULT:", "False")

    def test_submodule_update_called_on_worktree(self, tmp_path):
        """When .gitmodules exists, git submodule update --init --recursive runs."""
        repo, wt = _make_repo(tmp_path, with_submodules=True)
        _git_worktree_add(repo, wt)

        runner = _module_loader(repo) + textwrap.dedent(f"""\
            import subprocess as _sp
            orig_run = _sp.run
            called = []
            def _record(*a, **k):
                called.append((a, k))
                # Return success so the helper thinks it worked
                _result = _sp.CompletedProcess(a[0], 0, stdout="", stderr="")
                return _result
            _sp.run = _record
            result = mod._ensure_submodules({str(wt)!r}, {str(repo)!r})
            print(f"RESULT:{{result}}")
            print(f"CALLED:{{len(called)}}")
            if called:
                print(f"CMD:{{called[0][0][0]!r}}")
            _sp.run = orig_run
        """)
        proc = _run_in_subprocess(tmp_path, runner)
        assert proc.returncode == 0, proc.stderr
        _assert_line_in(proc, "RESULT:", "True")
        _assert_line_in(proc, "CALLED:", "1")
        assert "submodule" in proc.stdout, (
            f"must call git submodule update\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        assert "--init" in proc.stdout, "must pass --init flag"
        assert "--recursive" in proc.stdout, "must pass --recursive flag"

    def test_failure_logged_not_fatal(self, tmp_path):
        """Submodule update failure → logged warning, no exception, start continues."""
        repo, wt = _make_repo(tmp_path, with_submodules=True)
        _git_worktree_add(repo, wt)

        runner = _module_loader(repo) + textwrap.dedent(f"""\
            import subprocess as _sp
            def _fail(*a, **k):
                raise _sp.CalledProcessError(1, a[0], stderr="fatal: cannot fetch submodule")
            _sp.run = _fail
            result = mod._ensure_submodules({str(wt)!r}, {str(repo)!r})
            print(f"RESULT:{{result}}")
        """)
        proc = _run_in_subprocess(tmp_path, runner)
        assert proc.returncode == 0, proc.stderr
        _assert_line_in(proc, "RESULT:", "False")
        # CalledProcessError is caught and logged as a warning; the exact
        # stderr we passed may not appear in the exception's __str__, so we
        # check for the generic error text instead.
        assert "Submodule initialisation errored" in proc.stderr, (
            f"failure must be logged to stderr\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )

    def test_timeout_logged_not_fatal(self, tmp_path):
        """TimeoutExpired → logged warning, no exception, start continues."""
        repo, wt = _make_repo(tmp_path, with_submodules=True)
        _git_worktree_add(repo, wt)

        runner = _module_loader(repo) + textwrap.dedent(f"""\
            import subprocess as _sp
            def _timeout(*a, **k):
                raise _sp.TimeoutExpired(a[0], 600)
            _sp.run = _timeout
            result = mod._ensure_submodules({str(wt)!r}, {str(repo)!r})
            print(f"RESULT:{{result}}")
        """)
        proc = _run_in_subprocess(tmp_path, runner)
        assert proc.returncode == 0, proc.stderr
        _assert_line_in(proc, "RESULT:", "False")
        assert "TimeoutExpired" in proc.stderr or "timeout" in proc.stderr.lower(), (
            f"timeout must be logged to stderr\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )

    def test_os_error_logged_not_fatal(self, tmp_path):
        """OSError during subprocess → logged warning, no exception."""
        repo, wt = _make_repo(tmp_path, with_submodules=True)
        _git_worktree_add(repo, wt)

        runner = _module_loader(repo) + textwrap.dedent(f"""\
            import subprocess as _sp
            def _oserror(*a, **k):
                raise OSError("No such file or directory")
            _sp.run = _oserror
            result = mod._ensure_submodules({str(wt)!r}, {str(repo)!r})
            print(f"RESULT:{{result}}")
        """)
        proc = _run_in_subprocess(tmp_path, runner)
        assert proc.returncode == 0, proc.stderr
        _assert_line_in(proc, "RESULT:", "False")
        assert "No such file or directory" in proc.stderr, (
            f"OSError must be logged to stderr\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


# ---------------------------------------------------------------------------
# Integration tests: phase_start() end-to-end (with external stubs)
# ---------------------------------------------------------------------------


def _phase_start_runner(repo_root: Path, wt_override: Path) -> str:
    """Runner that calls phase_start with external integrations stubbed."""
    return _module_loader(repo_root) + textwrap.dedent(f"""\
        # Stub external integrations that require a real worklog/github.
        mod.is_code_freeze_active = lambda: False

        class _FakeLifecycle:
            @staticmethod
            def update_status(*a, **k):
                return None

        mod.StatusLifecycle = _FakeLifecycle
        mod.git_status = lambda cwd=None: ""
        mod.git_has_dirty_files = lambda status_output=None: False
        mod.wl_show = lambda i: {{"id": i, "title": "Test item"}}
        mod.wl_add_comment = lambda *a, **k: True

        result = mod.phase_start(
            {_FAKE_WORK_ITEM_ID!r},
            json_output=True,
            worktree_path_override={str(wt_override)!r},
        )
        print("RESULT_JSON:" + json.dumps(result))
    """)


class TestPhaseStartIntegration:
    def test_phase_start_succeeds_with_gitmodules_but_no_reachable_url(self, tmp_path):
        """Repo has .gitmodules → submodule helper runs, start still succeeds.

        When the submodule URL is not configured in .git/config (no actual
        submodule tracked), ``git submodule update --init --recursive`` is a
        no-op that returns 0.  The important thing is that ``start`` does not
        abort even if the repo declares submodules that can't be reached.
        """
        repo, wt = _make_repo(tmp_path, with_submodules=True)
        proc = _run_in_subprocess(tmp_path, _phase_start_runner(repo, wt))
        assert proc.returncode == 0, proc.stderr

        result = json.loads(
            next(l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON:"))[len("RESULT_JSON:"):] 
        )
        assert result["success"] is True, result

    def test_phase_start_skips_without_gitmodules(self, tmp_path):
        """Repo without .gitmodules → submodule helper skips, start succeeds."""
        repo, wt = _make_repo(tmp_path, with_submodules=False)
        proc = _run_in_subprocess(tmp_path, _phase_start_runner(repo, wt))
        assert proc.returncode == 0, proc.stderr

        result = json.loads(
            next(l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON:"))[len("RESULT_JSON:"):]
        )
        assert result["success"] is True, result


# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------


class TestStructural:
    SOURCE = _IMPLEMENT_PY.read_text() if _IMPLEMENT_PY.exists() else ""

    def test_phase_start_calls_submodules_after_worktree_add(self):
        """The submodule helper must run after the worktree is created."""
        assert self.SOURCE, f"implement.py not found at {_IMPLEMENT_PY}"
        add_idx = self.SOURCE.index("git_worktree_add(branch, wt_path, parent_branch)")
        call_idx = self.SOURCE.index("_ensure_submodules(abs_wt_path")
        assert add_idx != -1
        assert call_idx > add_idx, (
            "submodule init helper must be called AFTER the worktree is created"
        )

    def test_phase_start_calls_submodules_after_node_modules_symlink(self):
        """Submodule init must run AFTER node_modules symlink (same insertion point)."""
        assert self.SOURCE
        nm_idx = self.SOURCE.index("_ensure_node_modules_symlink(abs_wt_path")
        sub_idx = self.SOURCE.index("_ensure_submodules(abs_wt_path")
        assert nm_idx != -1
        assert sub_idx != -1
        assert sub_idx > nm_idx, (
            "submodule init helper must be called AFTER the node_modules symlink"
        )

    def test_helper_defined(self):
        """The submodule helper function must exist."""
        assert self.SOURCE
        assert "def _ensure_submodules(" in self.SOURCE

    def test_helper_has_docstring(self):
        """The submodule helper must have a docstring."""
        assert self.SOURCE
        # The function body starts with a triple-quoted docstring
        func_start = self.SOURCE.index("def _ensure_submodules(")
        func_body = self.SOURCE[func_start:func_start + 2000]
        assert '"""' in func_body, "Helper must have a docstring"


# ---------------------------------------------------------------------------
# Documentation tests
# ---------------------------------------------------------------------------


class TestDocumentation:
    def test_skill_md_documents_submodule_auto_init(self):
        """SKILL.md documents that submodules are auto-initialised."""
        assert _SKILL_MD.exists(), f"SKILL.md not found at {_SKILL_MD}"
        content = _SKILL_MD.read_text()
        assert "submodule" in content.lower(), (
            "SKILL.md must mention submodules"
        )
        assert "initialis" in content.lower() or "initializ" in content.lower(), (
            "SKILL.md must document that submodules are auto-initialised"
        )

    def test_skill_md_warns_about_failure_not_fatal(self):
        """SKILL.md documents that submodule init failure is non-fatal (warning only)."""
        assert _SKILL_MD.exists(), f"SKILL.md not found at {_SKILL_MD}"
        content = _SKILL_MD.read_text()
        # The doc should mention that failures are warnings, not aborts.
        has_warning = "warning" in content.lower()
        has_best_effort = "best effort" in content.lower() or "best-effort" in content.lower() or "not fatal" in content.lower()
        assert has_warning or has_best_effort, (
            "SKILL.md must document that submodule init failures are non-fatal / produce a warning"
        )
