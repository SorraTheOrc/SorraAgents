"""Hermetic real-git sandbox + repository-state snapshots.

This module is the single shared helper for tests that need a **real** ``git``
repository. It provides two things (SA-0MUG0WFP8008WN63, F3
SA-0MUG30IIR00978CW):

1. **Hermetic sandbox factories** — create repositories, bare remotes and
   worktrees under a caller-supplied ``tmp_path`` root while *neutralising the
   repository-overriding environment*. A leaked ``GIT_DIR`` (or
   ``GIT_WORK_TREE`` / ``GIT_CONFIG*``) is inherited by every ``git``
   subprocess and overrides ``cwd``, which is how the 2026-09-24 incident let
   an otherwise-isolated fixture mutate the live checkout
   (see ``docs/dev/test-isolation.md``). Every helper here strips those
   variables and pins a fixture-local identity.

2. **Read-only snapshots + diffs** — ``snapshot_repo_state`` captures a
   checkout's refs, local config, registered worktrees and working-tree status
   using plumbing that never mutates what it measures; ``diff_snapshots``
   reports additions, removals and moves. The regression guards (F4/F5) use
   these to fail a run that mutated its own checkout.

Design constraints (SA-0MUG0WFP8008WN63 F3 AC1/N-L5):

- **stdlib-only** — no ``shared.*`` or ``test.*`` imports, so the repo-root
  ``conftest.py`` can load this file by location without touching
  ``sys.path`` and ``run_tests.py`` can import it after its own bootstrap.

  Residual risk (F3 AC6): a mutation is attributed to the *checkout*
  (``refs/heads/*`` etc.). Exclusion of agent worktrees is derived from
  ``git worktree list --porcelain`` — never a ``wl-*`` name pattern, which
  would also have excluded the incident's own fixture branches. A fixture
  branch that happens to be backed by a registered worktree is therefore
  excluded; the incident is still caught via config rewrites, moved ``dev``,
  ``refs/heads/origin/dev`` and the working-tree surface.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Repository-overriding git environment variables. Git honours these over
#: ``cwd``; a leaked value makes an isolated fixture mutate the named repo.
REPOSITORY_OVERRIDE_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

#: Fixture-local identity, injected into every git command via ``-c`` so a
#: fixture never depends on (or rewrites) an ambient identity.
FIXTURE_AUTHOR_NAME = "Git Sandbox"
FIXTURE_AUTHOR_EMAIL = "git-sandbox@example.invalid"

#: Worktree-registration directory whose refs are excluded from snapshot
#: diffs (agent worktrees under ``<checkout>/.worklog/worktrees/``).
AGENT_WORKTREE_DIR = (".worklog", "worktrees")

#: Worklog data namespace. These refs are managed by the ``wl`` tool (they can
#: be fetched/reset during a test run) and are not part of the source checkout,
#: so they are excluded from the mutation surface to avoid false positives.
WORKLOG_REF_PREFIX = "refs/worklog/"


class GitSandboxError(RuntimeError):
    """Raised when a sandbox invariant is violated (e.g. a non-hermetic path)."""


# ---------------------------------------------------------------------------
# Environment isolation and git execution
# ---------------------------------------------------------------------------


def sanitized_git_env() -> dict[str, str]:
    """A copy of the process environment with repository overrides removed."""
    env = dict(os.environ)
    for name in REPOSITORY_OVERRIDE_ENV_VARS:
        env.pop(name, None)
    return env


def run_git(
    cwd: str | os.PathLike[str],
    args: list[str] | tuple[str, ...],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in *cwd* with the repository overrides neutralised.

    The fixture identity is supplied with ``-c user.name=... -c user.email=...``
    so the command never reads or writes an ambient identity.

    Raises:
        GitSandboxError: when *check* is true and git exits non-zero.
    """
    base_env = sanitized_git_env()
    if env:
        base_env.update(env)
    cmd = [
        "git",
        "-c",
        f"user.name={FIXTURE_AUTHOR_NAME}",
        "-c",
        f"user.email={FIXTURE_AUTHOR_EMAIL}",
        *args,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitSandboxError(
            f"git {' '.join(args)} failed ({proc.returncode}) in {cwd}: "
            f"{proc.stderr.strip()}"
        )
    return proc


def git_runner(cwd: str | os.PathLike[str]) -> Callable[[list[str]], subprocess.CompletedProcess]:
    """Return a runner callable bound to *cwd* with a hermetic environment."""

    def _run(args: list[str]) -> subprocess.CompletedProcess:
        return run_git(cwd, args)

    return _run


def git_toplevel(path: str | os.PathLike[str]) -> str | None:
    """The work-tree root containing *path*, or None when *path* is not in one."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    proc = run_git(probe, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def assert_outside_git_worktree(
    path: str | os.PathLike[str],
    *,
    sandbox_root: str | os.PathLike[str] | None = None,
) -> None:
    """Refuse to treat *path* as a fresh sandbox when it is inside a git repo.

    This is the superset of the historical ``_init_repo`` guard
    (SA-0MU8EKJYY007PT42): it rejects *any* path nested inside an enclosing
    repository, not merely the repository root.

    When *sandbox_root* is supplied, a work tree rooted inside that directory
    is accepted (a repo the sandbox itself created); every other enclosing
    work tree is rejected.

    Raises:
        GitSandboxError: when *path* resolves inside a disallowed work tree.
    """
    top = git_toplevel(path)
    if top is None:
        return
    if sandbox_root is not None:
        try:
            Path(top).resolve().relative_to(Path(sandbox_root).resolve())
            return
        except ValueError:
            pass
    raise GitSandboxError(
        f"refusing to create a git sandbox at {path}: it is inside git work "
        f"tree {top!r}. Isolate the fixture under a tmp_path not inside any "
        "repository."
    )


# ---------------------------------------------------------------------------
# Sandbox factories
# ---------------------------------------------------------------------------


def init_repo(
    path: str | os.PathLike[str],
    *,
    default_branch: str = "dev",
    sandbox_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Create a hermetic, non-bare git repository at *path*.

    The path must not resolve inside an existing git work tree (unless it is
    rooted at *sandbox_root*). Identity is pinned via the hermetic runner; no
    ambient configuration is read or written.

    Returns the resolved repository path.
    """
    repo = Path(path)
    assert_outside_git_worktree(repo, sandbox_root=sandbox_root)
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, ["init", "-q", "-b", default_branch], check=True)
    run_git(repo, ["config", "user.name", FIXTURE_AUTHOR_NAME], check=True)
    run_git(repo, ["config", "user.email", FIXTURE_AUTHOR_EMAIL], check=True)
    return repo


def init_bare_remote(
    path: str | os.PathLike[str],
    *,
    sandbox_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Create a hermetic bare repository suitable as a local remote."""
    remote = Path(path)
    assert_outside_git_worktree(remote, sandbox_root=sandbox_root)
    remote.mkdir(parents=True, exist_ok=True)
    run_git(remote, ["init", "--bare", "-q"], check=True)
    return remote


def commit_all(repo: str | os.PathLike[str], message: str) -> str:
    """Stage everything and commit, returning the new commit sha."""
    run_git(repo, ["add", "-A"], check=True)
    run_git(repo, ["commit", "-q", "-m", message], check=True)
    proc = run_git(repo, ["rev-parse", "HEAD"], check=True)
    return proc.stdout.strip()


def add_worktree(
    repo: str | os.PathLike[str],
    worktree_path: str | os.PathLike[str],
    branch: str,
    *,
    base: str | None = None,
    create_branch: bool = True,
) -> Path:
    """Register a worktree of *repo* at *worktree_path* on *branch*."""
    target = Path(worktree_path)
    args = ["worktree", "add"]
    if create_branch:
        args += ["-b", branch]
    args.append(str(target))
    if base:
        args.append(base)
    elif not create_branch:
        args.append(branch)
    run_git(repo, args, check=True)
    return target


def add_remote(repo: str | os.PathLike[str], name: str, url: str) -> None:
    """Add (or replace) remote *name* pointing at *url*."""
    run_git(repo, ["remote", "remove", name])
    run_git(repo, ["remote", "add", name, url], check=True)


# ---------------------------------------------------------------------------
# Read-only snapshots
# ---------------------------------------------------------------------------


def _parse_for_each_ref(output: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and not parts[0].startswith(WORKLOG_REF_PREFIX):
            refs[parts[0]] = parts[1]
    return refs


def _parse_local_config(output: str) -> dict[str, str]:
    config: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    return config


def _parse_status(output: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in output.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:]
        if " -> " in path:  # rename: report the destination
            path = path.split(" -> ", 1)[1]
        entries[path] = code
    return entries


def _parse_worktrees(output: str, root: Path) -> tuple[dict[str, str], set[str]]:
    """Parse ``git worktree list --porcelain``.

    Returns ``(branches_by_path, excluded_refs)`` where *excluded_refs* are the
    branch refs checked out in registered worktrees under
    ``<root>/.worklog/worktrees/`` (agent worktrees whose churn is legitimate).
    """
    branches: dict[str, str] = {}
    path: str | None = None
    branch: str | None = None
    excluded: set[str] = set()
    agent_root = root.joinpath(*AGENT_WORKTREE_DIR)

    def _flush() -> None:
        if path is None:
            return
        branches[path] = branch or ""
        if branch:
            try:
                Path(path).resolve().relative_to(agent_root.resolve())
                excluded.add(branch)
            except ValueError:
                pass

    for line in output.splitlines():
        if line.startswith("worktree "):
            _flush()
            path = line[len("worktree "):].strip()
            branch = None
        elif line.startswith("branch "):
            branch = line[len("branch "):].strip()
    _flush()
    return branches, excluded


def snapshot_repo_state(
    root: str | os.PathLike[str],
    *,
    include_status: bool = True,
) -> dict[str, Any]:
    """Capture a read-only snapshot of the checkout at *root*.

    Surfaces (F3 AC5): refs, local ``.git/config``, registered worktrees, and
    (when *include_status*) the working tree (tracked diff + untracked
    non-ignored paths, via ``git status --porcelain=v1 -uall``). Set
    ``include_status=False`` for the cheap refs+config+worktree fingerprint
    used by the per-test guard (F5 AC4); it must not pay for a full status
    scan on every test. Only read-only plumbing is used — never ``gc``,
    ``update-index``, ``fetch`` or ``--refresh``.

    Returns a dict with ``root``, ``git``, ``refs``, ``config``, ``status``,
    ``worktrees`` and ``excluded_refs``. A non-git root yields empty surfaces
    (the guard is a no-op there).
    """
    resolved = Path(root).resolve()
    top = git_toplevel(resolved)
    if top is None:
        return {
            "root": str(resolved),
            "git": False,
            "refs": {},
            "config": {},
            "status": {},
            "worktrees": {},
            "excluded_refs": set(),
        }

    refs = _parse_for_each_ref(
        run_git(resolved, ["for-each-ref", "--format=%(refname) %(objectname)"]).stdout
    )
    config = _parse_local_config(
        run_git(resolved, ["config", "--local", "--list"]).stdout
    )
    status = (
        _parse_status(
            run_git(resolved, ["status", "--porcelain=v1", "-uall"]).stdout
        )
        if include_status
        else {}
    )
    worktrees, excluded = _parse_worktrees(
        run_git(resolved, ["worktree", "list", "--porcelain"]).stdout,
        Path(top),
    )
    return {
        "root": str(resolved),
        "git": True,
        "refs": refs,
        "config": config,
        "status": status,
        "worktrees": worktrees,
        "excluded_refs": excluded,
    }


def fingerprint(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    """A cheap, hashable fingerprint of a snapshot's refs + local config.

    Refs checked out in registered agent worktrees (``excluded_refs``) are
    ignored, matching :func:`diff_snapshots`, so legitimate agent-worktree
    churn does not trip the per-test guard (F5 AC4/AC7). Used by the per-test
    guard to attribute a mutation without paying for a full ``status`` scan on
    every test.
    """
    excluded = set(snapshot.get("excluded_refs", set()))
    refs = tuple(
        sorted(
            (ref, sha)
            for ref, sha in snapshot.get("refs", {}).items()
            if ref not in excluded
        )
    )
    return (
        refs,
        tuple(sorted(snapshot.get("config", {}).items())),
        tuple(sorted(excluded)),
    )


def diff_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Report the differences between two snapshots.

    Refs checked out in registered agent worktrees (``excluded_refs``) are
    ignored. Returns ``{"changed": bool, "refs": ..., "config": ...,
    "status": ...}`` where each category lists added/removed/changed entries.
    """
    if not before.get("git", True) or not after.get("git", True):
        return {"changed": False, "refs": {}, "config": {}, "status": {}}

    excluded = set(before.get("excluded_refs", set())) | set(
        after.get("excluded_refs", set())
    )
    before_refs = {k: v for k, v in before["refs"].items() if k not in excluded}
    after_refs = {k: v for k, v in after["refs"].items() if k not in excluded}

    refs_added = {k: v for k, v in after_refs.items() if k not in before_refs}
    refs_removed = {k: v for k, v in before_refs.items() if k not in after_refs}
    refs_moved = {
        k: (before_refs[k], after_refs[k])
        for k in before_refs
        if k in after_refs and before_refs[k] != after_refs[k]
    }

    config_added = {
        k: v for k, v in after["config"].items() if k not in before["config"]
    }
    config_removed = {
        k: v for k, v in before["config"].items() if k not in after["config"]
    }
    config_changed = {
        k: (before["config"][k], after["config"][k])
        for k in before["config"]
        if k in after["config"] and before["config"][k] != after["config"][k]
    }

    status_added = {k: v for k, v in after["status"].items() if k not in before["status"]}
    status_removed = {
        k: v for k, v in before["status"].items() if k not in after["status"]
    }
    status_changed = {
        k: (before["status"][k], after["status"][k])
        for k in before["status"]
        if k in after["status"] and before["status"][k] != after["status"][k]
    }

    changed = bool(
        refs_added
        or refs_removed
        or refs_moved
        or config_added
        or config_removed
        or config_changed
        or status_added
        or status_removed
        or status_changed
    )
    return {
        "changed": changed,
        "refs": {
            "added": refs_added,
            "removed": refs_removed,
            "moved": refs_moved,
        },
        "config": {
            "added": config_added,
            "removed": config_removed,
            "changed": config_changed,
        },
        "status": {
            "added": status_added,
            "removed": status_removed,
            "changed": status_changed,
        },
    }


def describe_diff(diff: dict[str, Any]) -> str:
    """Render a human-readable, actionable summary of a snapshot diff."""
    lines: list[str] = []
    refs = diff.get("refs", {})
    for ref, sha in sorted(refs.get("added", {}).items()):
        lines.append(f"  added ref      {ref} -> {sha}")
    for ref, sha in sorted(refs.get("removed", {}).items()):
        lines.append(f"  deleted ref    {ref} ({sha})")
    for ref, (old, new) in sorted(refs.get("moved", {}).items()):
        lines.append(f"  moved ref      {ref}: {old} -> {new}")
    cfg = diff.get("config", {})
    for key, value in sorted(cfg.get("added", {}).items()):
        lines.append(f"  added config   {key}={value}")
    for key, value in sorted(cfg.get("removed", {}).items()):
        lines.append(f"  deleted config {key} ({value})")
    for key, (old, new) in sorted(cfg.get("changed", {}).items()):
        lines.append(f"  changed config {key}: {old} -> {new}")
    status = diff.get("status", {})
    for path, code in sorted(status.get("added", {}).items()):
        lines.append(f"  added path     [{code}] {path}")
    for path, code in sorted(status.get("removed", {}).items()):
        lines.append(f"  deleted path   [{code}] {path}")
    for path, (old, new) in sorted(status.get("changed", {}).items()):
        lines.append(f"  changed path   [{old}->{new}] {path}")
    return "\n".join(lines) if lines else "  (no differences)"
