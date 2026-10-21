"""Behavioural tests for the ship release full-suite gate.

Covers SA-0MVECQUGF0064U81: ``merge-dev-to-main.sh`` asserts the merged
dev→main tree is full green BEFORE the release is made (tag created, branch
pushed, PR opened). The release-branch push is a feature-branch push, so the
pre-push hook skips its test gate — this release gate is the only full-suite
assertion for the promotion. ``SHIP_TEST_SKIP=1`` bypasses (mirroring the
hook's ``TEST_SCOPE_SKIP``).

The script is executed inside a throwaway git repo (main + dev branches with
a bare ``origin``), with a fake ``skill/test/scripts/run_tests.py`` that
records how it was invoked and exits with a configurable code, and a fake
``gh`` binary that records PR creation. ``HOME`` is isolated so the global
``~/.pi/agent/skills`` fallback cannot leak the real runner into the fixture.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, Path(__file__).resolve().parents[2]):
    if (candidate / "skill" / "ship" / "scripts" / "release" / "merge-dev-to-main.sh").exists():
        REPO_ROOT = candidate
        break
RELEASE_SCRIPT = REPO_ROOT / "skill" / "ship" / "scripts" / "release" / "merge-dev-to-main.sh"

_FAKE_RUNNER = """#!/usr/bin/env python3
import os
import sys

marker = os.environ.get("RUNNER_MARKER")
if marker:
    with open(marker, "a", encoding="utf-8") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("SUITE_EXIT", "0")))
"""

_FAKE_GH = """#!/usr/bin/env sh
marker="$GH_MARKER"
if [ -n "$marker" ]; then
  printf '%s\\n' "$*" >> "$marker"
fi
echo "https://github.com/example/fixture/pull/7"
exit 0
"""


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, check=False,
    )


def _build_release_fixture(
    tmp_path: Path,
    *,
    with_runner: bool = True,
) -> tuple[Path, Path, Path, Path]:
    """Create a hermetic release fixture.

    Returns ``(repo, runner_marker, gh_marker, bin_dir)``:

    - ``repo``: a working clone of a bare ``origin`` with ``main`` and
      ``dev`` (dev one commit ahead), clean tree, ``.worklog/`` gitignored.
    - ``runner_marker``: fake run_tests.py invocation record (absent when
      the runner is never invoked).
    - ``gh_marker``: fake gh invocation record (absent when gh is never run).
    - ``bin_dir``: on PATH, provides the fake ``gh``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    runner_marker = tmp_path / "runner-marker.txt"
    gh_marker = tmp_path / "gh-marker.txt"

    _git("init", "-q", "--bare", str(origin)).check_returncode()
    # The seeded default branch (master) is renamed to main below; point the
    # bare origin's HEAD at main so `git clone` checks out main.
    _git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main") \
        .check_returncode()
    seed.mkdir()

    def _identity(cwd: Path) -> None:
        _git("config", "user.email", "t@t", cwd=cwd).check_returncode()
        _git("config", "user.name", "t", cwd=cwd).check_returncode()

    _git("init", "-q", str(seed), cwd=seed).check_returncode()
    _identity(seed)
    (seed / "base.txt").write_text("base\n", encoding="utf-8")
    _git("-C", str(seed), "add", "base.txt").check_returncode()
    _git("-C", str(seed), "commit", "-m", "base").check_returncode()
    _git("-C", str(seed), "branch", "-M", "main").check_returncode()
    _git("-C", str(seed), "checkout", "-q", "-b", "dev").check_returncode()
    (seed / "dev.txt").write_text("dev\n", encoding="utf-8")
    _git("-C", str(seed), "add", "dev.txt").check_returncode()
    _git("-C", str(seed), "commit", "-m", "dev work").check_returncode()
    _git("-C", str(seed), "remote", "add", "origin", str(origin)).check_returncode()
    _git("-C", str(seed), "push", "-q", "origin", "main", "dev").check_returncode()

    _git("clone", "-q", str(origin), str(repo)).check_returncode()
    _identity(repo)
    (repo / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    _git("-C", str(repo), "add", ".gitignore").check_returncode()
    _git("-C", str(repo), "commit", "-m", "gitignore").check_returncode()
    if with_runner:
        runner = repo / "skill" / "test" / "scripts" / "run_tests.py"
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text(_FAKE_RUNNER, encoding="utf-8")
        # Commit the fake runner BEFORE pushing main: the release branch is
        # forked from origin/main, so the runner must be on origin/main for
        # the gate to find it in the merged tree (and the release script
        # refuses a dirty tree, so it must be committed, not untracked).
        _git("-C", str(repo), "add", "skill").check_returncode()
        _git("-C", str(repo), "commit", "-m", "fake runner").check_returncode()
    push = _git("-C", str(repo), "push", "-q", "origin", "main")
    assert push.returncode == 0, f"fixture push origin main failed:\n{push.stderr}"
    _git("-C", str(repo), "fetch", "-q", "origin").check_returncode()

    home.mkdir(exist_ok=True)
    bin_dir.mkdir(exist_ok=True)
    gh_bin = bin_dir / "gh"
    gh_bin.write_text(_FAKE_GH, encoding="utf-8")
    gh_bin.chmod(0o755)

    return repo, runner_marker, gh_marker, bin_dir


def _run_release(
    repo: Path,
    tmp_path: Path,
    bin_dir: Path,
    runner_marker: Path,
    gh_marker: Path,
    *,
    suite_exit: int = 0,
    env_overrides: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the release script (copied, sibling helpers omitted) in the fixture."""
    release_dir = tmp_path / "release"
    release_dir.mkdir(exist_ok=True)
    script_copy = release_dir / "merge-dev-to-main.sh"
    shutil.copy(RELEASE_SCRIPT, script_copy)
    script_copy.chmod(0o755)

    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "RUNNER_MARKER": str(runner_marker),
        "SUITE_EXIT": str(suite_exit),
        "GH_MARKER": str(gh_marker),
    }
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        ["bash", str(script_copy), *(args or [])],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result


def _has_marker(marker: Path) -> bool:
    return marker.exists() and marker.read_text(encoding="utf-8").strip() != ""


# ── Gate green: release proceeds ────────────────────────────────────────────


class TestGreenGate:
    def test_green_gate_invokes_full_scope_and_release_proceeds(
        self, tmp_path: Path
    ) -> None:
        """A green full suite on the merged tree lets the release proceed."""
        repo, runner_marker, gh_marker, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(repo, tmp_path, bin_dir, runner_marker, gh_marker)

        assert result.returncode == 0, result.stderr
        # The gate ran the full suite with the release invocation.
        assert "--scope full" in runner_marker.read_text(), runner_marker.read_text()
        assert "--strict-git-env" in runner_marker.read_text(), runner_marker.read_text()
        assert "Release gate: full suite green" in result.stderr, result.stderr
        # A merge commit was created, and the release proceeded to PR creation.
        merge_log = _git("-C", str(repo), "log", "--oneline", "-3").stdout
        assert "Merge origin/dev into main" in merge_log, merge_log
        assert _has_marker(gh_marker), "release must reach gh pr create on green"


# ── Gate red: release blocked ───────────────────────────────────────────────


class TestRedGate:
    def test_red_gate_blocks_release_before_pr(self, tmp_path: Path) -> None:
        """A failing full suite must abort the release before gh is invoked."""
        repo, runner_marker, gh_marker, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(repo, tmp_path, bin_dir, runner_marker, gh_marker,
                              suite_exit=1)

        assert result.returncode != 0, result.stdout
        assert "the release is blocked" in result.stderr, result.stderr
        assert _has_marker(runner_marker), "the gate must have run the suite"
        assert not _has_marker(gh_marker), "no PR may be opened on a red tree"


# ── Dry-run and bypass ──────────────────────────────────────────────────────


class TestDryRunAndBypass:
    def test_dry_run_skips_suite_execution(self, tmp_path: Path) -> None:
        """--dry-run announces the gate but never executes the suite."""
        repo, runner_marker, gh_marker, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(repo, tmp_path, bin_dir, runner_marker, gh_marker,
                              args=["--dry-run"])

        assert result.returncode == 0, result.stderr
        assert "Dry-run: full-suite gate would run" in result.stdout, result.stdout
        assert not runner_marker.exists(), "dry-run must not execute the suite"
        assert not _has_marker(gh_marker)

    def test_ship_test_skip_bypasses_gate(self, tmp_path: Path) -> None:
        """SHIP_TEST_SKIP=1 skips the gate even with a red suite."""
        repo, runner_marker, gh_marker, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(repo, tmp_path, bin_dir, runner_marker, gh_marker,
                              suite_exit=1, env_overrides={"SHIP_TEST_SKIP": "1"})

        assert result.returncode == 0, result.stderr
        assert not runner_marker.exists(), "gate must be skipped on SHIP_TEST_SKIP=1"
        assert _has_marker(gh_marker)


# ── Missing runner: fail closed ─────────────────────────────────────────────


class TestMissingRunnerFailClosed:
    def test_missing_runner_fails_closed(self, tmp_path: Path) -> None:
        """A release without run_tests.py must fail closed, not silently pass."""
        repo, runner_marker, gh_marker, bin_dir = _build_release_fixture(
            tmp_path, with_runner=False
        )
        result = _run_release(repo, tmp_path, bin_dir, runner_marker, gh_marker)

        assert result.returncode != 0, result.stdout
        assert "run_tests.py not found" in result.stderr, result.stderr
        assert not _has_marker(gh_marker), "no PR may be opened without a gate"
