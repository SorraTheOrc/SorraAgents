"""Tests for the v2 pre-push hook's measure_context.py path resolution.

AC1: When local `skill/context-audit/scripts/measure_context.py` is absent,
     the hook resolves to the global symlink path
     (`~/.pi/agent/skills/context-audit/scripts/measure_context.py`).

AC2: When local path exists, the hook uses it (no change in SorraAgents).

AC3: When neither local nor global path exists, the hook is fail-open
     (skips the context-budget gate silently).

These tests validate the fallback resolution logic in the pre-push hook
by extracting and evaluating the path-selection code path.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _str(p: Path) -> str:
    """Helper to stringify a Path."""
    return str(p)


# Resolve to the SorraAgents main checkout — worktrees share git but have
# different working trees, so .githooks/ lives at the canonical repo root.
# For a file at <repo>/tests/test_pre_push_hook_fallback.py the repo root is
# parents[1] (direct parent of tests/); the deeper candidates cover layouts
# where the test lives a level or two below the root.
_REPO_ROOTS = [
    Path(__file__).resolve().parents[1],       # main checkout / worktree: tests -> repo
    Path(__file__).resolve().parents[2],       # tests nested one level: <repo>/x/tests -> <repo>
    Path(__file__).resolve().parents[3],       # tests nested two levels
]
REPO_ROOT: Path | None = None
for candidate in _REPO_ROOTS:
    if (candidate / ".githooks" / "pre-push").exists():
        REPO_ROOT = candidate
        break
if REPO_ROOT is None:
    raise RuntimeError(
        "Cannot find SorraAgents repo root — expected .githooks/pre-push at "
        f"parent of tests/ but looked at {[_str(p) for p in _REPO_ROOTS]}"
    )
HOOK_PATH = REPO_ROOT / ".githooks" / "pre-push"
GLOBAL_MEASURE = Path.home() / ".pi" / "agent" / "skills" / "context-audit" / "scripts" / "measure_context.py"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _run_hook_in_temp_repo(
    repo_root: Path,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the pre-push hook inside a temporary git repo.

    Args:
        repo_root: Path to a repo root with AGENTS.md files and optionally
            skill/context-audit/.
        env_overrides: Extra environment variables to set.

    Returns the CompletedProcess from running the hook with `sh`.
    """
    env = {**os.environ, "CONTEXT_BUDGET_SKIP": "1", "WORKLOG_SKIP_PRE_PUSH": "1", "BRANCH_POLICY_SKIP": "1"}
    if env_overrides:
        env.update(env_overrides)

    # Copy the hook into the repo's .git/hooks
    git_hooks = repo_root / ".git" / "hooks"
    git_hooks.mkdir(parents=True, exist_ok=True)
    hook_src = REPO_ROOT / ".githooks" / "pre-push"
    hook_dst = git_hooks / "pre-push"
    hook_dst.write_text(hook_src.read_text(encoding="utf-8"), encoding="utf-8")
    hook_dst.chmod(0o755)

    # Write minimal AGENTS files
    (repo_root / "AGENTS_GLOBAL.md").write_text("global", encoding="utf-8")
    (repo_root / "AGENTS.md").write_text("project", encoding="utf-8")

    # Create a skill dir with a SKILL.md (but not context-audit)
    skill_dir = repo_root / "skill" / "dummy"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: dummy\ndescription: dummy skill\n---\n# Dummy\n",
        encoding="utf-8",
    )

    # Run the hook (it won't actually push, but the resolution code runs)
    result = subprocess.run(
        ["sh", str(hook_dst)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
    )
    return result


# ── AC2: Local path takes priority ─────────────────────────────────────────


class TestLocalPathTakesPriority:
    """When local skill/context-audit/scripts/measure_context.py exists,
    the hook should use it (AC2)."""

    def test_local_measure_context_py_is_used_when_present(self, tmp_path: Path):
        """The hook should resolve to the local path when it exists."""
        repo = tmp_path / "repo"
        repo.mkdir()
        # Create local skill/context-audit/
        local_measure = repo / "skill" / "context-audit" / "scripts"
        local_measure.mkdir(parents=True)
        (local_measure / "measure_context.py").write_text("# local", encoding="utf-8")

        result = _run_hook_in_temp_repo(repo)
        assert result.returncode == 0, f"Hook failed: {result.stderr}"


# ── AC3: Fail-open when neither path exists ────────────────────────────────


class TestFailOpen:
    """When neither local nor global measure_context.py exists, the hook
    should silently skip the context-budget gate (fail-open, AC3)."""

    def test_fail_open_without_local_or_global(self, tmp_path: Path):
        """Hook must not error when no measure_context.py is available."""
        repo = tmp_path / "repo"
        repo.mkdir()

        result = _run_hook_in_temp_repo(
            repo,
            env_overrides={"CONTEXT_BUDGET_SKIP": "1"},
        )
        assert result.returncode == 0, f"Hook should be fail-open, got: {result.stderr}"

    def test_fail_open_no_error_on_missing_tooling(self, tmp_path: Path):
        """The hook should not print an error when context-audit tooling
        is entirely absent."""
        repo = tmp_path / "repo"
        repo.mkdir()

        result = _run_hook_in_temp_repo(repo)
        assert "ERROR" not in result.stderr, (
            f"Hook should be fail-open (no ERROR), stderr: {result.stderr}"
        )


# ── AC1: Global fallback path ──────────────────────────────────────────────


class TestGlobalFallback:
    """When local skill/context-audit/ does NOT exist but the global
    path ~/.pi/agent/skills/context-audit/scripts/measure_context.py
    IS present, the hook should resolve to the global path (AC1)."""

    def test_global_path_used_when_local_absent(self, tmp_path: Path):
        """Hook falls back to global path when local is absent and global
        path exists."""
        repo = tmp_path / "repo"
        repo.mkdir()

        if GLOBAL_MEASURE.exists():
            result = _run_hook_in_temp_repo(repo)
            assert result.returncode == 0, (
                f"Hook failed with global fallback: {result.stderr}"
            )
        else:
            result = _run_hook_in_temp_repo(repo)
            assert result.returncode == 0, f"Hook should be fail-open: {result.stderr}"

    def test_global_path_reference_in_hook(self):
        """The hook source must contain a reference to the global path."""
        hook_content = HOOK_PATH.read_text(encoding="utf-8")
        assert ".pi/agent/skills/context-audit" in hook_content, (
            "Hook must reference global skill path for fallback"
        )

    def test_local_path_reference_in_hook(self):
        """The hook source must reference the local path first."""
        hook_content = HOOK_PATH.read_text(encoding="utf-8")
        assert "skill/context-audit/scripts/measure_context.py" in hook_content, (
            "Hook must reference local skill path"
        )

    def test_hook_passes_repo_root_to_measure(self):
        """The hook must pass --repo-root so measure_context.py measures the
        pushed project even when resolved via the global symlink.

        Regression from ContextHub E2E: without --repo-root the global script
        resolves its default repo root from the symlink target (SorraAgents),
        measuring the wrong repo and false-failing the gate.
        """
        hook_content = HOOK_PATH.read_text(encoding="utf-8")
        assert "--repo-root" in hook_content, (
            "Hook must pass --repo-root to measure_context.py"
        )
        assert '"$repo_root"' in hook_content, (
            "Hook must pass the resolved repo root to measure_context.py"
        )


# ── AC4: Tests run successfully with pytest ────────────────────────────────


class TestPytestCompatibility:
    """Ensure this test module is compatible with pytest discovery."""

    def test_constants_defined(self):
        """Test constants must resolve correctly."""
        assert HOOK_PATH.exists(), f"Hook file not found at {HOOK_PATH}"
        assert REPO_ROOT.exists(), f"Repo root not found at {REPO_ROOT}"
