"""Regression test for SA-0MTC5CRVI008RD1K: skill_path shim install wiring.

Verifies that `scripts/install_pi.sh`:

- installs the `skill_path` shim to `~/.pi/agent/bin/skill_path` as an
  executable,
- is idempotent (a re-run keeps the shim intact), and
- commits the shim source with executable mode `100755`.

Also verifies the shim's runtime resolution contract: known skills resolve to
their directory and an unknown skill exits non-zero with a clear message.

Install-wiring assertions run against a sandboxed `$HOME` so the real
installation is never touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH_SRC = REPO_ROOT / "scripts" / "skill_path"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_pi.sh"


def _run_install(home: Path) -> subprocess.CompletedProcess:
    """Run install_pi.sh with a sandboxed HOME and no hook wiring."""
    # install_pi.sh's DEFAULT_SRC is "$HOME/projects/SorraAgents"; point it at
    # this checkout so no interactive prompt is triggered.
    src_link = home / "projects" / "SorraAgents"
    src_link.parent.mkdir(parents=True, exist_ok=True)
    if not src_link.exists():
        src_link.symlink_to(REPO_ROOT)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PI_SKIP_HOOK_INSTALL"] = "1"
    # Fail fast rather than hang on an unexpected interactive prompt.
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture()
def sandbox_home():
    home = Path(tempfile.mkdtemp(prefix="skill_path_test_"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


class TestShimInstallWiring:
    """AC2: install_pi.sh installs the shim as an executable, idempotently."""

    def test_shim_installed_after_install(self, sandbox_home):
        result = _run_install(sandbox_home)
        shim = sandbox_home / ".pi" / "agent" / "bin" / "skill_path"
        assert shim.exists(), (
            f"shim not installed at {shim}; install output:\n{result.stdout}"
        )

    def test_installed_shim_is_executable(self, sandbox_home):
        _run_install(sandbox_home)
        shim = sandbox_home / ".pi" / "agent" / "bin" / "skill_path"
        assert shim.exists()
        assert os.access(shim, os.X_OK), f"shim at {shim} is not executable"

    def test_installed_shim_runs_and_resolves(self, sandbox_home):
        """The installed copy (not just the source) resolves a skill."""
        _run_install(sandbox_home)
        shim = sandbox_home / ".pi" / "agent" / "bin" / "skill_path"
        result = subprocess.run(
            ["bash", str(shim), "report"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"installed shim failed: {result.stderr}"
        assert result.stdout.strip().endswith("/report")

    def test_install_is_idempotent(self, sandbox_home):
        _run_install(sandbox_home)
        shim = sandbox_home / ".pi" / "agent" / "bin" / "skill_path"
        assert shim.exists()
        # Second run must leave the shim intact and executable.
        result = _run_install(sandbox_home)
        assert result.returncode == 0, f"re-run failed: {result.stderr}"
        assert shim.exists(), "idempotent re-run removed the shim"
        assert os.access(shim, os.X_OK), "idempotent re-run broke permissions"


class TestShimResolution:
    """AC3: skill_path <name> resolves correctly, unknown skill errors."""

    def _invoke(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SKILL_PATH_SRC), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_resolves_known_skill(self):
        result = self._invoke("report")
        assert result.returncode == 0, f"skill_path report failed: {result.stderr}"
        assert result.stdout.strip().endswith("/report")

    def test_resolves_multiple_known_skills(self):
        for skill in ("audit", "implement", "ship", "test"):
            result = self._invoke(skill)
            assert result.returncode == 0, (
                f"skill_path {skill} failed: {result.stderr}"
            )
            assert result.stdout.strip().endswith(f"/{skill}")

    def test_unknown_skill_fails_gracefully(self):
        result = self._invoke("nonexistent_skill_xyz")
        assert result.returncode != 0
        assert "nonexistent_skill_xyz" in result.stderr, (
            "error message should name the unknown skill"
        )

    def test_no_arguments_exits_nonzero(self):
        result = self._invoke()
        assert result.returncode != 0
        assert "usage:" in result.stderr.lower()


class TestShimSourceMode:
    """AC2: scripts/skill_path is tracked in git as executable (100755)."""

    def test_git_file_mode_is_executable(self):
        rel = SKILL_PATH_SRC.relative_to(REPO_ROOT)
        result = subprocess.run(
            ["git", "ls-files", "-s", str(rel)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        line = result.stdout.strip()
        mode = line.split()[0] if line else ""
        assert mode == "100755", (
            f"scripts/skill_path git mode is {mode}, expected 100755"
        )
