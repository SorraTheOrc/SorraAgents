"""Hermetic regression tests for the committed Worklog post-pull git hooks.

SA-0MVK1Q30O0067NXX. The committed wrappers (``.githooks/post-checkout``,
``post-merge``, ``post-rewrite``) used to ``exec`` a hard-coded per-user path::

    exec "/home/<user>/.config/pi/.githooks/worklog-post-pull" "$@"

On any host where that exact path is absent (a different username, a clean
machine, CI) every checkout, merge and ``git worktree add`` exited 127, which
also broke ``wl sync`` (it syncs through a temporary worktree) and the
implement skill's worktree creation. The defect was invisible on the agent
host because ``scripts/install_pi.sh`` happens to install that exact path as a
symlink to this repository's committed ``.githooks/worklog-post-pull``.

The tests below verify the portable contract:

- AC1: no committed hook contains a machine-specific absolute path (``/home/``,
  ``/Users/``, ``/root/``) and no wrapper ``exec``s a literal absolute path;
- AC2: in a throw-away repository built from the committed ``.githooks/``
  with ``core.hooksPath=.githooks`` and a sandboxed ``$HOME`` (no
  ``.config/pi``), ``git worktree add`` exits 0, emits no
  ``exec: ... not found``, and runs the *repo-relative*
  ``.githooks/worklog-post-pull``;
- AC2 (no-op path): with the shared script absent the hook prints exactly one
  stderr notice naming both attempted locations and exits 0 without syncing;
- AC3: ``wl sync`` completes its push step (to a local bare remote) with a
  sandboxed ``$HOME`` and no ``WORKLOG_SKIP_POST_PULL`` /
  ``core.hooksPath=/dev/null`` workaround;
- AC4: the shared script skips a ``wl``-spawned ``*tmp-worktree-*`` and any
  linked worktree, so a hook-triggered sync can never recurse;
- ``WORKLOG_SKIP_POST_PULL=1`` still bypasses the sync.

Everything runs against a temporary repository and a sandboxed ``HOME``: the
real checkout, the real ``$HOME`` installation and the network are never
touched. ``GIT_*`` variables inherited from the calling session are stripped
so a polluted environment (SA-0MUIZSXEY008NGR8) cannot leak into the fixture.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHOOKS = REPO_ROOT / ".githooks"
WRAPPERS = ("post-checkout", "post-merge", "post-rewrite")
SHARED = "worklog-post-pull"
MACHINE_PATH_MARKERS = ("/home/", "/Users/", "/root/")
NOTICE_PREFIX = "worklog:"
HOME_CENTRAL_REL = Path(".config") / "pi" / ".githooks" / SHARED


# ── Helpers ─────────────────────────────────────────────────────────────────


def _env(home: Path, *, stub_wl: Path | None = None, **extra: str) -> dict[str, str]:
    """Return a hermetic environment: sandboxed HOME, no inherited GIT_*."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for key in ("WORKLOG_SKIP_POST_PULL", "WORKLOG_SKIP_PRE_PUSH"):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "GIT_AUTHOR_NAME": "Hook Test",
            "GIT_AUTHOR_EMAIL": "hook-test@example.invalid",
            "GIT_COMMITTER_NAME": "Hook Test",
            "GIT_COMMITTER_EMAIL": "hook-test@example.invalid",
        }
    )
    if stub_wl is not None:
        env["PATH"] = os.pathsep.join(
            [str(stub_wl.parent), os.environ.get("PATH", "/usr/bin:/bin")]
        )
    env.update(extra)
    return env


def _run(
    args: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _stub_wl(directory: Path, calls: Path) -> Path:
    """Create a ``wl`` stub that records its invocations instead of syncing."""
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "wl"
    stub.write_text(
        "#!/bin/sh\n" f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n" "exit 0\n"
    )
    stub.chmod(0o755)
    return stub


def _make_temp_repo(
    tmp_path: Path,
    *,
    include_shared: bool = True,
    shared_body: str | None = None,
) -> tuple[Path, dict[str, str]]:
    """Build a throw-away repository from this repo's committed ``.githooks/``.

    Returns the repository root and the hermetic environment used to create it
    (a sandboxed ``HOME`` with no ``.config/pi`` central hook install).
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home)

    repo = tmp_path / "repo"
    hooks = repo / ".githooks"
    hooks.mkdir(parents=True)
    for name in WRAPPERS:
        target = hooks / name
        shutil.copyfile(GITHOOKS / name, target)
        target.chmod(0o755)
    if include_shared:
        target = hooks / SHARED
        if shared_body is None:
            shutil.copyfile(GITHOOKS / SHARED, target)
        else:
            target.write_text(shared_body)
        target.chmod(0o755)
    (repo / "README.md").write_text("hook fixture\n")

    assert _run(["git", "init", "-q", "-b", "dev", "."], cwd=repo, env=env).returncode == 0
    _run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo, env=env)
    assert _run(["git", "add", "-A"], cwd=repo, env=env).returncode == 0
    commit = _run(["git", "commit", "-qm", "hook fixture"], cwd=repo, env=env)
    assert commit.returncode == 0, commit.stderr
    return repo, env


def _worklog_notices(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith(NOTICE_PREFIX)]


# ── AC1: no machine-specific path in the committed hooks ────────────────────


@pytest.mark.parametrize("hook", (*WRAPPERS, SHARED))
def test_committed_hook_has_no_machine_specific_path(hook: str) -> None:
    text = (GITHOOKS / hook).read_text(encoding="utf-8")

    offences = [marker for marker in MACHINE_PATH_MARKERS if marker in text]
    assert not offences, f"{hook} hard-codes a machine-specific path: {offences}"

    for line in text.splitlines():
        assert not re.match(r'^exec\s+"?/', line.strip()), (
            f"{hook} execs a literal absolute path (not portable): {line!r}"
        )


# ── AC2: git worktree add succeeds under a sandboxed HOME ───────────────────


def test_worktree_add_succeeds_with_committed_hooks(tmp_path: Path) -> None:
    repo, env = _make_temp_repo(tmp_path)
    calls = tmp_path / "wl-calls"
    env = _env(Path(env["HOME"]), stub_wl=_stub_wl(tmp_path / "bin", calls))
    worktree = tmp_path / "linked"

    proc = _run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repo,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert "exec:" not in proc.stderr
    assert "not found" not in proc.stderr
    assert (worktree / ".githooks" / WRAPPERS[0]).is_file()
    # The real shared script ran; the linked-worktree guard stops it before
    # it would call `wl sync` (AC4), so no sync happened.
    assert not calls.exists()


def test_worktree_add_runs_the_repo_relative_shared_script(tmp_path: Path) -> None:
    """The wrapper must resolve the script inside the repo, never `$HOME`."""
    marker = tmp_path / "shared-ran"
    shared_stub = (
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$0\" > {shlex.quote(str(marker))}\n"
        "exit 0\n"
    )
    repo, env = _make_temp_repo(tmp_path, shared_body=shared_stub)
    worktree = tmp_path / "linked"

    proc = _run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repo,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert marker.is_file(), "the repo-relative worklog-post-pull never ran"
    ran = Path(marker.read_text(encoding="utf-8").strip())
    assert ran.name == SHARED
    assert (worktree / ".githooks" / SHARED).resolve() == ran.resolve()
    assert str(env["HOME"]) not in str(ran)


def test_missing_shared_script_is_one_notice_and_success(tmp_path: Path) -> None:
    repo, env = _make_temp_repo(tmp_path, include_shared=False)
    calls = tmp_path / "wl-calls"
    env = _env(Path(env["HOME"]), stub_wl=_stub_wl(tmp_path / "bin", calls))
    worktree = tmp_path / "linked"

    proc = _run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repo,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert worktree.is_dir()

    notices = _worklog_notices(proc.stderr)
    assert len(notices) == 1, f"expected exactly one notice, got {notices}"
    notice = notices[0]
    assert f".githooks/{SHARED}" in notice
    assert str(HOME_CENTRAL_REL) in notice
    assert not calls.exists(), "the no-op path must not attempt a sync"


# ── AC4 + bypass: the shared script's guards ────────────────────────────────


def _run_shared(
    repo: Path, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return _run(["sh", str(repo / ".githooks" / SHARED)], cwd=cwd, env=env)


def test_bypass_variable_skips_the_sync(tmp_path: Path) -> None:
    repo, env = _make_temp_repo(tmp_path)
    calls = tmp_path / "wl-calls"
    env = _env(
        Path(env["HOME"]),
        stub_wl=_stub_wl(tmp_path / "bin", calls),
        WORKLOG_SKIP_POST_PULL="1",
        PWD=str(repo),
    )

    proc = _run_shared(repo, repo, env)

    assert proc.returncode == 0, proc.stderr
    assert not calls.exists()


def test_sync_is_skipped_inside_a_wl_tmp_worktree(tmp_path: Path) -> None:
    repo, env = _make_temp_repo(tmp_path)
    calls = tmp_path / "wl-calls"
    tmp_worktree = tmp_path / "tmp-worktree-fixture"
    tmp_worktree.mkdir()
    env = _env(
        Path(env["HOME"]),
        stub_wl=_stub_wl(tmp_path / "bin", calls),
        PWD=str(tmp_worktree),
    )

    proc = _run_shared(repo, tmp_worktree, env)

    assert proc.returncode == 0, proc.stderr
    assert not calls.exists(), "a sync must never run inside wl's own temp worktree"


def test_sync_is_skipped_inside_a_linked_worktree(tmp_path: Path) -> None:
    repo, env = _make_temp_repo(tmp_path)
    worktree = tmp_path / "linked"
    added = _run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repo,
        env=env,
    )
    assert added.returncode == 0, added.stderr

    calls = tmp_path / "wl-calls"
    env = _env(
        Path(env["HOME"]),
        stub_wl=_stub_wl(tmp_path / "bin", calls),
        PWD=str(worktree),
    )

    proc = _run_shared(repo, worktree, env)

    assert proc.returncode == 0, proc.stderr
    assert not calls.exists(), "sync runs from the main checkout only"


# ── AC3: wl sync completes its push step without the central install ────────


@pytest.mark.skipif(
    shutil.which("wl") is None and shutil.which("worklog") is None,
    reason="the wl/worklog CLI is not installed",
)
def test_wl_sync_pushes_with_a_sandboxed_home(tmp_path: Path) -> None:
    repo, env = _make_temp_repo(tmp_path)
    bare = tmp_path / "remote.git"
    assert _run(["git", "init", "-q", "--bare", str(bare)], cwd=tmp_path, env=env).returncode == 0
    assert (
        _run(["git", "remote", "add", "origin", str(bare)], cwd=repo, env=env).returncode
        == 0
    )
    assert _run(["git", "push", "-q", "origin", "dev"], cwd=repo, env=env).returncode == 0

    initialised = _run(
        [
            "wl",
            "init",
            "--json",
            "--project-name",
            "HookPortabilityFixture",
            "--prefix",
            "HPF",
            "--auto-export",
            "no",
            "--auto-sync",
            "no",
            "--agents-template",
            "skip",
        ],
        cwd=repo,
        env=env,
    )
    assert initialised.returncode == 0, initialised.stderr

    synced = _run(["wl", "sync", "--json"], cwd=repo, env=env)

    assert synced.returncode == 0, synced.stderr
    assert "exec:" not in synced.stderr
    assert "not found" not in synced.stderr
    assert '"pushed": true' in synced.stdout, synced.stdout[-2000:]
