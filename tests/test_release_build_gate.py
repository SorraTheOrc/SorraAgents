"""Behavioural tests for the ship release build/type-check gate.

Covers SA-0MWKQ5TXL005CLX5: ``merge-dev-to-main.sh`` must refuse to release a
merged dev→main tree that does not build. The full-suite gate runs tests, but
every test runner in the framework transpiles/interprets without
type-checking, so a tree that does not compile can still show a green suite
(the real ContextHub v1.0.30 incident: 9 ``TS2305``/``TS2724`` errors shipped
because the build step was non-fatal).

The script is executed inside a throwaway git repo (``main`` + ``dev`` with a
bare ``origin``), with a fake ``skill/test/scripts/run_tests.py`` that exits
with a configurable code, a fake ``gh`` binary that records PR creation, and a
``package.json`` whose ``scripts.build`` is a controllable ``node build.js``
(``BUILD_EXIT``). ``HOME`` is isolated so the global ``~/.pi/agent/skills``
fallback cannot leak the real runner into the fixture.

The release helpers are copied **standalone** into a temp directory, exactly
as an installed ship skill ships them. A ``{"type": "module"}`` package.json
is written beside them so the ESM ``bump-version.js`` helper loads correctly;
``assert-build.mjs`` is extension-forced ESM and needs no such help.

Test-writing guidelines: this exercises the real production script (not a
re-implementation) and does not duplicate the full-suite gate's coverage — its
subject is the build assertion's fatal semantics, bypass and no-op paths.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from shared.git_sandbox import fixture_git_env, run_git

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, Path(__file__).resolve().parents[2]):
    if (candidate / "skill" / "ship" / "scripts" / "release" / "merge-dev-to-main.sh").exists():
        REPO_ROOT = candidate
        break
RELEASE_DIR = REPO_ROOT / "skill" / "ship" / "scripts" / "release"
RELEASE_SCRIPT = RELEASE_DIR / "merge-dev-to-main.sh"

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

# The fixture build: records that it ran, then exits with the requested code.
_FAKE_BUILD = """#!/usr/bin/env node
const fs = require('node:fs');
const marker = process.env.BUILD_MARKER;
if (marker) {
  fs.appendFileSync(marker, 'build\\n');
}
process.exit(Number(process.env.BUILD_EXIT || '0'));
"""


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command in the fixture sandbox, hermetically."""
    return run_git(cwd or Path.cwd(), args)


def _identity(cwd: Path) -> None:
    _git("config", "user.email", "t@t", cwd=cwd).check_returncode()
    _git("config", "user.name", "t", cwd=cwd).check_returncode()


def _build_release_fixture(
    tmp_path: Path,
    *,
    with_build_script: bool = True,
) -> tuple[Path, dict[str, Path], Path]:
    """Create a hermetic release fixture.

    Returns ``(repo, markers, bin_dir)``:

    - ``repo``: a working clone of a bare ``origin`` with ``main`` and
      ``dev`` (dev one commit ahead), clean tree, ``.worklog/`` gitignored.
    - ``markers``: ``runner``/``gh``/``build`` invocation records.
    - ``bin_dir``: on PATH, provides the fake ``gh``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    markers = {
        "runner": tmp_path / "runner-marker.txt",
        "gh": tmp_path / "gh-marker.txt",
        "build": tmp_path / "build-marker.txt",
    }

    _git("init", "-q", "--bare", str(origin)).check_returncode()
    # The seeded default branch (master) is renamed to main below; point the
    # bare origin's HEAD at main so `git clone` checks out main.
    _git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main") \
        .check_returncode()
    seed.mkdir()

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

    # Version source + build script live on main (the release branch forks
    # from origin/main), so the merged tree carries them.
    package = {"name": "fixture", "version": "1.0.0"}
    if with_build_script:
        package["scripts"] = {"build": "node build.js"}
    (repo / "package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    if with_build_script:
        (repo / "build.js").write_text(_FAKE_BUILD, encoding="utf-8")
    _git("-C", str(repo), "add", "package.json").check_returncode()
    if with_build_script:
        _git("-C", str(repo), "add", "build.js").check_returncode()
    _git("-C", str(repo), "commit", "-m", "fixture build").check_returncode()

    # Commit the fake runner BEFORE pushing main: the release branch is forked
    # from origin/main, so the runner must be on origin/main for the full-suite
    # gate to find it in the merged tree.
    runner = repo / "skill" / "test" / "scripts" / "run_tests.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(_FAKE_RUNNER, encoding="utf-8")
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

    return repo, markers, bin_dir


def _copy_release_helpers(destination: Path, *, with_build_gate: bool = True) -> None:
    """Copy the release script and its siblings into a standalone directory.

    A ``{"type": "module"}`` package.json is written beside them so the ESM
    ``bump-version.js`` helper resolves its module type (the skim-friendly
    equivalent of the real skill's package context). ``assert-build.mjs`` is
    extension-forced ESM.
    """
    destination.mkdir(exist_ok=True)
    for name in ("merge-dev-to-main.sh", "worklog-ref-gate.sh", "bump-version.js", "assert-build.mjs"):
        if name == "assert-build.mjs" and not with_build_gate:
            continue
        source = RELEASE_DIR / name
        assert source.exists(), f"missing release helper: {source}"
        target = destination / name
        shutil.copy(source, target)
        if name.endswith(".sh"):
            target.chmod(0o755)
    (destination / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")


def _run_release(
    repo: Path,
    tmp_path: Path,
    bin_dir: Path,
    markers: dict[str, Path],
    *,
    build_exit: int = 0,
    suite_exit: int = 0,
    with_build_gate: bool = True,
    env_overrides: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the release script (copied, sibling helpers included) in the fixture."""
    release_copy = tmp_path / "release"
    _copy_release_helpers(release_copy, with_build_gate=with_build_gate)
    script_copy = release_copy / "merge-dev-to-main.sh"

    env = {
        **fixture_git_env(),
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "RUNNER_MARKER": str(markers["runner"]),
        "SUITE_EXIT": str(suite_exit),
        "GH_MARKER": str(markers["gh"]),
        "BUILD_MARKER": str(markers["build"]),
        "BUILD_EXIT": str(build_exit),
    }
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        ["bash", str(script_copy), *(args or [])],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _has_marker(marker: Path) -> bool:
    return marker.exists() and marker.read_text(encoding="utf-8").strip() != ""


def _tags(repo: Path) -> list[str]:
    return [tag for tag in _git("-C", str(repo), "tag", "-l", "v*").stdout.split() if tag]


# ── Fatal gate: a red build blocks the release before tagging ───────────────


class TestRedBuildBlocksRelease:
    def test_failing_build_blocks_release_before_tag(self, tmp_path: Path) -> None:
        """A merged tree whose build fails must be blocked before any tag exists."""
        repo, markers, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(repo, tmp_path, bin_dir, markers, build_exit=1)

        assert result.returncode != 0, result.stdout
        assert "the release is blocked" in result.stderr, result.stderr
        # The build actually ran (and failed) on the merged tree...
        assert _has_marker(markers["build"]), "the build gate must run the build"
        # ...and the release never reached tagging or PR creation.
        assert _tags(repo) == [], f"no tag may exist on a red build: {_tags(repo)}"
        assert not _has_marker(markers["gh"]), "no PR may be opened on a red build"

    def test_missing_build_gate_helper_fails_closed(self, tmp_path: Path) -> None:
        """A release without assert-build.mjs must fail closed, not silently pass."""
        repo, markers, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(repo, tmp_path, bin_dir, markers, with_build_gate=False)

        assert result.returncode != 0, result.stdout
        assert "assert-build.mjs not found" in result.stderr, result.stderr
        assert _tags(repo) == [], "no tag may exist without a build assertion"
        assert not _has_marker(markers["gh"]), "no PR may be opened without a build gate"


# ── Green/no-op paths: the release proceeds ────────────────────────────────


class TestReleaseProceeds:
    def test_passing_build_release_proceeds_through_tag(self, tmp_path: Path) -> None:
        """A green build lets the release reach tagging and PR creation."""
        repo, markers, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(repo, tmp_path, bin_dir, markers, build_exit=0)

        assert result.returncode == 0, result.stderr
        assert "build green on the merged tree" in result.stderr, result.stderr
        assert _has_marker(markers["build"]), "the build gate must run the build"
        # bump-version.js bumps 1.0.0 → 1.0.1, so the tag is reachable.
        assert "v1.0.1" in _tags(repo), f"expected v1.0.1 tag, got {_tags(repo)}"
        assert _has_marker(markers["gh"]), "release must reach PR creation on green"

    def test_no_build_script_is_a_noop(self, tmp_path: Path) -> None:
        """A project without scripts.build is unaffected by the gate."""
        repo, markers, bin_dir = _build_release_fixture(tmp_path, with_build_script=False)
        result = _run_release(repo, tmp_path, bin_dir, markers)

        assert result.returncode == 0, result.stderr
        assert not _has_marker(markers["build"]), "no build script must mean no build run"
        assert _has_marker(markers["gh"]), "a build-less project must still release"


# ── Explicit bypass and dry-run ────────────────────────────────────────────


class TestBypassAndDryRun:
    def test_ship_build_skip_bypasses_fatal_build_and_logs(
        self, tmp_path: Path
    ) -> None:
        """SHIP_BUILD_SKIP=1 bypasses a red build explicitly and is logged."""
        repo, markers, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(
            repo, tmp_path, bin_dir, markers, build_exit=1,
            env_overrides={"SHIP_BUILD_SKIP": "1"},
        )

        assert result.returncode == 0, result.stderr
        assert "SHIP_BUILD_SKIP=1" in result.stderr, result.stderr
        assert "BYPASSED" in result.stderr, result.stderr
        assert not _has_marker(markers["build"]), "gate must be skipped on SHIP_BUILD_SKIP=1"
        assert _has_marker(markers["gh"]), "bypass must let the release proceed"

    def test_dry_run_announces_gate_without_building(self, tmp_path: Path) -> None:
        """--dry-run announces the gate but never executes the build."""
        repo, markers, bin_dir = _build_release_fixture(tmp_path)
        result = _run_release(repo, tmp_path, bin_dir, markers, args=["--dry-run"])

        assert result.returncode == 0, result.stderr
        assert "Dry-run: build gate would run" in result.stdout, result.stdout
        assert not _has_marker(markers["build"]), "dry-run must not execute the build"
        assert not _has_marker(markers["gh"])
        assert _tags(repo) == []
