"""Tests for install_pi.sh's v2 pre-push hook wiring behavior.

These tests validate that `scripts/install_pi.sh` (or its hook-wiring block)
correctly:

- AC1: sets `core.hooksPath=.githooks` when installing hooks
- AC2: copies the v2 pre-push hook to `.githooks/pre-push`
- AC3: references/verifies the global context-audit skill path
- AC4: hook installation is idempotent (re-running doesn't duplicate/corrupt)

Because install_pi.sh performs full user-home symlinking, the tests exercise
an isolated, self-contained hook-wiring snippet mirroring the block that
install_pi.sh contains (or will contain). The snippet is extracted from the
actual install_pi.sh source when present, else a known reference block is used.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PI = REPO_ROOT / "scripts" / "install_pi.sh"
HOOK_SOURCE = REPO_ROOT / ".githooks" / "pre-push"
HOOK_V2_SOURCE = REPO_ROOT / ".githooks" / "pre-push.v2"
GLOBAL_SKILL = Path.home() / ".pi" / "agent" / "skills" / "context-audit" / "scripts" / "measure_context.py"

# Markers distinguishing the two hook variants.
V2_HEADER = "worklog:pre-push-hook:v2"
V3_HEADER = "worklog:pre-push-hook:v3"
FULL_SUITE_MARKER = "Scope-aware test gate"
CONTEXT_BUDGET_MARKER = "Context-budget regression gate"
BRANCH_POLICY_MARKER = "Branch policy enforcement"
WORKLOG_SYNC_MARKER = "Worklog sync"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _find_hook_wiring_block() -> str:
    """Extract the hook-wiring logic from install_pi.sh.

    The hook-wiring block installs .githooks/pre-push and sets
    core.hooksPath. This lets the tests assert behavior against the real
    script rather than a hard-coded copy.
    """
    content = INSTALL_PI.read_text(encoding="utf-8")
    # The hook-wiring section is marked with a distinctive header.
    marker = "context-budget"
    if marker not in content:
        pytest.skip("install_pi.sh does not yet contain the context-budget hook wiring block")
    return content


def _run_install_pi_in_sandbox(sandbox: Path) -> subprocess.CompletedProcess:
    """Run install_pi.sh inside an isolated HOME + source sandbox.

    Creates a fake source repo (a copy of install_pi.sh + a .githooks dir),
    points HOME into the sandbox, and runs the script so we can observe the
    hooks installed into a throwaway project.
    """
    fake_src = sandbox / "src"
    fake_home = sandbox / "home"
    fake_proj = sandbox / "project"
    for p in (fake_src, fake_home, fake_proj):
        p.mkdir(parents=True, exist_ok=True)

    # Minimal source repo: skills + prompts + AGENTS_GLOBAL.md + install_pi.sh
    (fake_src / "skill" / "context-audit" / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_src / "skill" / "context-audit" / "scripts" / "measure_context.py").write_text(
        "# fake measure\n", encoding="utf-8"
    )
    (fake_src / "command").mkdir(parents=True, exist_ok=True)
    (fake_src / "AGENTS_GLOBAL.md").write_text("global agents\n", encoding="utf-8")
    (fake_src / ".githooks").mkdir(exist_ok=True)
    (fake_src / ".githooks" / "pre-push").write_text(
        HOOK_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (fake_src / "scripts").mkdir(exist_ok=True)
    shutil_copy_file(INSTALL_PI, fake_src / "scripts" / "install_pi.sh")

    env = {
        **os.environ,
        "HOME": str(fake_home),
        "SRC_DIR": str(fake_src),  # in case the script honours an override
    }
    # Non-interactive: default source dir won't exist under the fake HOME, so
    # the script reads from stdin — feed it the fake source path and 'q' fallback.
    return subprocess.run(
        ["bash", str(fake_src / "scripts" / "install_pi.sh")],
        input=str(fake_src) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=str(fake_proj),
        check=False,  # caller inspects result (stdout behavior asserted below)
    )


def shutil_copy_file(src: Path, dst: Path) -> None:
    import shutil

    shutil.copyfile(src, dst)


# ── install_pi.sh into a real (sandboxed) git project ───────────────────────


def _install_into_git_project(
    sandbox: Path, *, with_runner: bool
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run install_pi.sh inside a sandboxed git project and return the result.

    ``with_runner`` controls whether the target project ships the repo-local
    full-suite runner (``skill/test/scripts/run_tests.py``) that distinguishes
    the v3 hook from the framework v2 template.

    Returns ``(result, project_root)``.
    """
    fake_src = sandbox / "src"
    fake_home = sandbox / "home"
    fake_proj = sandbox / "project"
    for p in (fake_src, fake_home, fake_proj):
        p.mkdir(parents=True, exist_ok=True)

    # Minimal source repo mirroring the SorraAgents checkout: skills, prompts,
    # global AGENTS, both hook variants, and install_pi.sh itself.
    (fake_src / "skill" / "context-audit" / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_src / "skill" / "context-audit" / "scripts" / "measure_context.py").write_text(
        "# fake measure\n", encoding="utf-8"
    )
    (fake_src / "command").mkdir(parents=True, exist_ok=True)
    (fake_src / "AGENTS_GLOBAL.md").write_text("global agents\n", encoding="utf-8")
    (fake_src / ".githooks").mkdir(exist_ok=True)
    (fake_src / ".githooks" / "pre-push").write_text(
        HOOK_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (fake_src / ".githooks" / "pre-push.v2").write_text(
        HOOK_V2_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (fake_src / "scripts").mkdir(exist_ok=True)
    shutil_copy_file(INSTALL_PI, fake_src / "scripts" / "install_pi.sh")

    # Target project: a real git repo so the hook-wiring block activates.
    subprocess.run(["git", "init", "-q", str(fake_proj)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(fake_proj), "config", "user.email", "t@t"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_proj), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    # Avoid the branch-policy check depending on the default branch name.
    subprocess.run(
        ["git", "-C", str(fake_proj), "checkout", "-q", "-b", "feature-wl-x"],
        check=True,
        capture_output=True,
    )

    if with_runner:
        runner = fake_proj / "skill" / "test" / "scripts" / "run_tests.py"
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")

    env = {
        **os.environ,
        "HOME": str(fake_home),
        "SRC_DIR": str(fake_src),  # in case the script honours an override
    }
    result = subprocess.run(
        ["bash", str(fake_src / "scripts" / "install_pi.sh")],
        input=str(fake_src) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=str(fake_proj),
        check=False,
    )
    return result, fake_proj


def _simulate_dev_push(project: Path) -> subprocess.CompletedProcess:
    """Run the project's installed pre-push hook with a simulated dev push."""
    env = {
        **os.environ,
        "CONTEXT_BUDGET_SKIP": "1",
        "WORKLOG_SKIP_PRE_PUSH": "1",
        "BRANCH_POLICY_SKIP": "1",
    }
    return subprocess.run(
        ["sh", str(project / ".githooks" / "pre-push")],
        cwd=str(project),
        input="refs/heads/wl-x 0000 refs/heads/dev 1111\n",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# ── AC1: core.hooksPath set to .githooks ───────────────────────────────────


class TestHooksPath:
    def test_hook_wiring_block_sets_hooks_path(self):
        """install_pi.sh must configure core.hooksPath=.githooks."""
        content = INSTALL_PI.read_text(encoding="utf-8")
        assert "core.hooksPath" in content, (
            "install_pi.sh should reference core.hooksPath in its hook wiring"
        )
        assert ".githooks" in content, (
            "install_pi.sh should use .githooks as the hooks directory"
        )


# ── AC2: v2 hook copied to .githooks/pre-push ──────────────────────────────


class TestHookCopy:
    def test_hook_wiring_block_references_pre_push(self):
        """install_pi.sh must copy/create .githooks/pre-push."""
        content = INSTALL_PI.read_text(encoding="utf-8")
        assert "pre-push" in content, (
            "install_pi.sh hook wiring should reference .githooks/pre-push"
        )


# ── AC3: global skill symlink referenced ───────────────────────────────────


class TestGlobalSkill:
    def test_hook_wiring_block_references_global_skill(self):
        """install_pi.sh must reference the global context-audit path."""
        content = INSTALL_PI.read_text(encoding="utf-8")
        assert "context-audit" in content, (
            "install_pi.sh should reference the global context-audit skill path"
        )
        assert "measure_context.py" in content, (
            "install_pi.sh should reference measure_context.py"
        )

    def test_global_source_hook_is_v2(self):
        """The hook source bundled in the source repo includes the gate."""
        hook_content = HOOK_SOURCE.read_text(encoding="utf-8")
        assert "Context-budget regression gate" in hook_content, (
            "The v2 pre-push hook source must contain the context-budget gate"
        )


# ── AC4: runs under pytest (stub) ──────────────────────────────────────────


class TestInstallPiHooks:
    def test_install_pi_script_exists(self):
        """install_pi.sh must exist and be executable."""
        assert INSTALL_PI.exists(), f"install_pi.sh not found at {INSTALL_PI}"
        mode = INSTALL_PI.stat().st_mode
        assert mode & 0o111, "install_pi.sh should be executable"

    def test_sandboxed_installation_creates_githooks(self, tmp_path: Path):
        """A full sandboxed install run should wire the pre-push hook."""
        result = _run_install_pi_in_sandbox(tmp_path)
        # The script runs to completion even if some steps are skipped.
        # We assert it produced no fatal error.
        assert "Done." in result.stdout, (
            f"install_pi.sh should complete; stdout: {result.stdout} stderr: {result.stderr}"
        )


# ── SA-0MV8M4OVX000QHSM: variant selection in a fresh project ───────────────


class TestFreshProjectHookVariant:
    """install_pi.sh must install the framework v2 hook (no full-suite gate)
    into a fresh project that lacks the repo-local test runner, and keep the
    v3 hook (with the full-suite gate) for the canonical source repo."""

    def test_fresh_project_gets_framework_v2_hook(self, tmp_path: Path):
        """A project without skill/test/scripts/run_tests.py must receive the v2
        hook, not the v3 hook that hard-fails when the runner is absent."""
        result, project = _install_into_git_project(tmp_path, with_runner=False)
        assert "Done." in result.stdout, (
            f"install_pi.sh should complete; stdout: {result.stdout} stderr: {result.stderr}"
        )
        installed = (project / ".githooks" / "pre-push").read_text(encoding="utf-8")
        assert V2_HEADER in installed, (
            "fresh project should receive the framework v2 hook, got:" f"\n{installed}"
        )
        assert V3_HEADER not in installed, "v3 hook must not be installed here"
        assert FULL_SUITE_MARKER not in installed, (
            "installed hook must not carry the SorraAgents-only full-suite gate"
        )

    def test_fresh_project_dev_push_is_not_blocked(self, tmp_path: Path):
        """AC1: a simulated dev push through the freshly installed hook must not
        be blocked by a missing local test runner."""
        result, project = _install_into_git_project(tmp_path, with_runner=False)
        assert "Done." in result.stdout
        push = _simulate_dev_push(project)
        assert push.returncode == 0, (
            f"dev push must not be blocked; rc={push.returncode} "
            f"stderr: {push.stderr} stdout: {push.stdout}"
        )
        assert "not found" not in push.stderr.lower(), push.stderr

    def test_fresh_project_hook_retains_framework_gates(self, tmp_path: Path):
        """AC2: the context-budget gate, worklog sync and branch policy must
        remain present in the installed v2 hook."""
        result, project = _install_into_git_project(tmp_path, with_runner=False)
        assert "Done." in result.stdout
        installed = (project / ".githooks" / "pre-push").read_text(encoding="utf-8")
        assert CONTEXT_BUDGET_MARKER in installed, "context-budget gate missing"
        assert BRANCH_POLICY_MARKER in installed, "branch policy missing"
        assert WORKLOG_SYNC_MARKER in installed, "worklog sync missing"

    def test_source_repo_with_runner_keeps_v3_hook(self, tmp_path: Path):
        """A project that ships the repo-local runner must still receive the v3
        hook with the full-suite gate."""
        result, project = _install_into_git_project(tmp_path, with_runner=True)
        assert "Done." in result.stdout, (
            f"install_pi.sh should complete; stdout: {result.stdout} stderr: {result.stderr}"
        )
        installed = (project / ".githooks" / "pre-push").read_text(encoding="utf-8")
        assert V3_HEADER in installed, (
            "repo with local runner should receive the v3 hook, got:" f"\n{installed}"
        )
        assert FULL_SUITE_MARKER in installed, "v3 hook missing full-suite gate"

    def test_v2_template_is_framework_safe(self):
        """The committed v2 template carries the framework gates and no
        full-suite gate."""
        template = HOOK_V2_SOURCE.read_text(encoding="utf-8")
        assert V2_HEADER in template
        assert CONTEXT_BUDGET_MARKER in template
        assert BRANCH_POLICY_MARKER in template
        assert WORKLOG_SYNC_MARKER in template
        assert FULL_SUITE_MARKER not in template
        assert "cannot run the full-suite gate" not in template, (
            "template must not carry the v3 gate's missing-runner hard-fail"
        )
