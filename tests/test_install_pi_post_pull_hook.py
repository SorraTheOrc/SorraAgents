"""Regression tests for the central Worklog post-pull hook install (SA-0MU8CRX1X008IORP).

The committed project hooks (`.githooks/post-checkout`, `post-merge`,
`post-rewrite`, all `worklog:post-pull-hook:v1`) delegate to a single
per-machine script at `$HOME/.config/pi/.githooks/worklog-post-pull`. Nothing
used to create that script, so a fresh environment produced

    exec: /home/<user>/.config/pi/.githooks/worklog-post-pull: not found

on every checkout/merge and the post-pull `wl sync` never ran.

These tests verify that `scripts/install_pi.sh`:

- AC1: installs the central hook into a *fresh* `$HOME` (no `.config/pi`),
  executable, and idempotently (a re-run leaves it intact);
- AC2: the installed hook runs `wl sync` on checkout and is bypassed by
  `WORKLOG_SKIP_POST_PULL=1`, with no `not found` error from git;
- AC3: honours the existing `PI_SKIP_HOOK_INSTALL=1` opt-out.

Every install run uses a sandboxed `$HOME` and a sandbox working directory, so
the real installation and the real checkout are never touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_pi.sh"
HOOK_SRC = REPO_ROOT / ".githooks" / "worklog-post-pull"
HOOK_MARKER = "worklog:post-pull-hook:v1"
CENTRAL_REL = Path(".config") / "pi" / ".githooks" / "worklog-post-pull"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fresh_home(root: Path) -> Path:
    """Create a sandboxed HOME whose default source dir resolves to this repo.

    `install_pi.sh` looks for `$HOME/projects/SorraAgents`; symlinking it to the
    checkout keeps the run non-interactive without touching the real HOME.
    """
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    src_link = home / "projects" / "SorraAgents"
    src_link.parent.mkdir(parents=True, exist_ok=True)
    src_link.symlink_to(REPO_ROOT)
    return home


def _run_install(
    home: Path,
    *,
    cwd: Path,
    skip_hook_install: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run install_pi.sh with a sandboxed HOME.

    `cwd` is always provided explicitly: running the installer from inside the
    real checkout with hook wiring enabled would mutate the real repository.
    """
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("PI_SKIP_HOOK_INSTALL", None)
    if skip_hook_install:
        env["PI_SKIP_HOOK_INSTALL"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        check=False,
        env=env,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _make_git_project(root: Path, name: str = "project") -> Path:
    """Create a git repo with an initial commit and `.githooks` enabled."""
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "core.hooksPath", ".githooks")
    (project / ".githooks").mkdir(exist_ok=True)
    (project / "file.txt").write_text("one\n", encoding="utf-8")
    _git(project, "add", "file.txt")
    _git(project, "commit", "-q", "-m", "initial")
    return project


def _write_hook(path: Path, central: Path) -> None:
    """Write a post-pull delegating hook (mirrors the committed wrapper)."""
    path.write_text(
        "#!/bin/sh\n# worklog:post-pull-hook:v1\n"
        f'exec "{central}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _fake_wl(bin_dir: Path, marker: Path) -> Path:
    """Create a fake `wl` that records its invocation into `marker`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    wl = bin_dir / "wl"
    wl.write_text(
        "#!/bin/sh\n" f'printf "%s\\n" "$@" >> "{marker}"\n' "exit 0\n",
        encoding="utf-8",
    )
    wl.chmod(0o755)
    return wl


@pytest.fixture()
def sandbox():
    root = Path(tempfile.mkdtemp(prefix="post_pull_hook_test_"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── AC1/AC3: install wiring ────────────────────────────────────────────────


class TestCentralHookInstall:
    def test_installs_into_fresh_home(self, sandbox):
        """A HOME without `.config/pi` gets an executable central hook."""
        home = _fresh_home(sandbox)
        central = home / CENTRAL_REL
        assert not central.parent.exists(), "precondition: directory must not exist"

        result = _run_install(home, cwd=sandbox)

        assert result.returncode == 0, f"installer failed: {result.stderr}"
        assert central.exists(), (
            f"central hook not installed at {central}; stdout:\n{result.stdout}"
        )
        assert os.access(central, os.X_OK), f"{central} is not executable"
        assert HOOK_MARKER in central.read_text(encoding="utf-8"), (
            "installed hook is not the committed worklog post-pull script"
        )
        # The installed hook must resolve to this repo's committed source so
        # hook fixes propagate without a reinstall.
        assert central.resolve() == HOOK_SRC.resolve()

    def test_install_is_idempotent(self, sandbox):
        """Re-running the installer keeps the hook intact and does not error."""
        home = _fresh_home(sandbox)
        central = home / CENTRAL_REL
        first = _run_install(home, cwd=sandbox)
        assert first.returncode == 0, first.stderr
        assert central.exists()

        second = _run_install(home, cwd=sandbox)

        assert second.returncode == 0, f"re-run failed: {second.stderr}"
        assert central.exists(), "idempotent re-run removed the central hook"
        assert os.access(central, os.X_OK), "idempotent re-run broke permissions"
        backups = list(central.parent.glob("worklog-post-pull.bak.*"))
        assert backups == [], f"re-run created spurious backups: {backups}"

    def test_skip_hook_install_prevents_installation(self, sandbox):
        """PI_SKIP_HOOK_INSTALL=1 must leave the central hook untouched."""
        home = _fresh_home(sandbox)
        result = _run_install(home, cwd=sandbox, skip_hook_install=True)

        assert result.returncode == 0, result.stderr
        assert not (home / CENTRAL_REL).exists(), (
            "PI_SKIP_HOOK_INSTALL=1 must not install the central hook"
        )


# ── AC2: installed hook behaviour ──────────────────────────────────────────


class TestInstalledHookBehaviour:
    def _install(self, sandbox: Path) -> Path:
        home = _fresh_home(sandbox)
        result = _run_install(home, cwd=sandbox)
        assert result.returncode == 0, result.stderr
        return home / CENTRAL_REL

    def test_runs_wl_sync(self, sandbox):
        hook = self._install(sandbox)
        marker = sandbox / "wl-calls.txt"
        _fake_wl(sandbox / "bin", marker)
        env = {
            **os.environ,
            "PATH": f"{sandbox / 'bin'}{os.pathsep}{os.environ['PATH']}",
        }
        env.pop("WORKLOG_SKIP_POST_PULL", None)

        result = subprocess.run(
            ["sh", str(hook)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert marker.exists(), "installed hook did not invoke wl sync"
        assert marker.read_text(encoding="utf-8").strip() == "sync"

    def test_skip_env_bypasses_wl_sync(self, sandbox):
        hook = self._install(sandbox)
        marker = sandbox / "wl-calls.txt"
        _fake_wl(sandbox / "bin", marker)
        env = {
            **os.environ,
            "PATH": f"{sandbox / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "WORKLOG_SKIP_POST_PULL": "1",
        }

        result = subprocess.run(
            ["sh", str(hook)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert not marker.exists(), "WORKLOG_SKIP_POST_PULL=1 must bypass wl sync"

    def test_missing_wl_is_fail_open(self, sandbox):
        """A machine without wl must not turn checkout into a hard failure."""
        hook = self._install(sandbox)
        empty_bin = sandbox / "empty-bin"
        empty_bin.mkdir()
        # Resolve `sh` before narrowing PATH so the hook itself can start.
        sh = shutil.which("sh")
        assert sh is not None, "precondition: sh must be available"
        env = {**os.environ, "PATH": str(empty_bin)}
        env.pop("WORKLOG_SKIP_POST_PULL", None)

        result = subprocess.run(
            [sh, str(hook)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "wl/worklog not found" in result.stderr


# ── AC2: end-to-end through a real git checkout ────────────────────────────


class TestGitCheckoutIntegration:
    def test_checkout_runs_post_pull_sync_without_not_found(self, sandbox):
        """AC2: `git checkout` delegates to the installed central hook, which
        runs `wl sync`, with no `not found` error."""
        home = _fresh_home(sandbox)
        project = _make_git_project(sandbox)
        central = home / CENTRAL_REL
        _write_hook(project / ".githooks" / "post-checkout", central)

        install = _run_install(home, cwd=project)
        assert install.returncode == 0, install.stderr
        assert central.exists(), "installer did not install the central hook"

        marker = sandbox / "wl-calls.txt"
        _fake_wl(sandbox / "bin", marker)
        env = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{sandbox / 'bin'}{os.pathsep}{os.environ['PATH']}",
        }
        env.pop("WORKLOG_SKIP_POST_PULL", None)

        checkout = subprocess.run(
            ["git", "-C", str(project), "checkout", "-q", "-b", "feature-wl-x"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

        assert checkout.returncode == 0, checkout.stderr
        assert "not found" not in checkout.stderr.lower(), checkout.stderr
        assert marker.exists(), "post-checkout did not trigger the post-pull wl sync"

    def test_warns_when_project_hook_points_elsewhere(self, sandbox):
        """A committed wrapper that delegates to a different central path would
        still break checkout, so the installer must say so (fail-open)."""
        home = _fresh_home(sandbox)
        project = _make_git_project(sandbox)
        foreign = home / "elsewhere" / "worklog-post-pull"
        _write_hook(project / ".githooks" / "post-checkout", foreign)

        result = _run_install(home, cwd=project)

        assert result.returncode == 0, result.stderr
        assert str(home / CENTRAL_REL) in result.stderr, (
            "warning should name the central path the project hook must reference;"
            f" stderr:\n{result.stderr}"
        )
        assert "WARNING" in result.stderr
