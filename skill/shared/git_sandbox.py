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

   ``snapshot_guarded_roots`` / ``diff_guarded_roots`` extend that pair to
   **every checkout a guarded run can reach**: the guarded root itself plus,
   when the guarded root is a linked worktree, its **main checkout**
   (WL-0MVPE2BY9001CR9O). A worktree-scoped run shares one object store and a
   symlinked ``node_modules`` with the main checkout, so a run started in
   ``<main>/.worklog/worktrees/<wl-…>`` can unlink tracked files in ``<main>``
   while a root-only snapshot pair reports a clean run.

Design constraints (SA-0MUG0WFP8008WN63 F3 AC1/N-L5):

- **stdlib-only** — no ``shared.*`` or ``test.*`` imports, so the repo-root
  ``conftest.py`` can load this file by location without touching
  ``sys.path`` and ``run_tests.py`` can import it after its own bootstrap.

  Residual risk (F3 AC6): a mutation is attributed to the *checkout*
  (``refs/heads/*`` etc.). Exclusion of concurrent infrastructure is
  **ownership-based** (SA-0MV8311B3005DNG8 AC1/AC3): a ref checked out in a
  registered worktree **other than the guard's own root** is mechanical agent
  churn (a sibling agent's ``implement.py finish`` fast-forwarding ``dev``,
  registering/removing ``.worklog/`` worktrees), and so is the
  ``branch.<name>.*`` local config that worktree registration writes
  (``remote``/``merge``) — both derived from ``git worktree list --porcelain``
  and the retained-local-track config, never a ``wl-*`` name pattern. It is
  also **concurrency-based** (WL-0MV5MYBKJ0033P8U): the exact ref
  ``refs/heads/dev`` (``CONCURRENCY_EXCLUDED_REF_EXACT``) and the whole
  ``refs/remotes/origin/*`` namespace (``CONCURRENCY_EXCLUDED_REF_NAMESPACES``)
  are excluded because concurrent collaborative movement of those refs (a
  sibling finish fast-forward on the shared main checkout, concurrent
  ``fetch``/``pull``/``push`` on a multi-agent host) is indistinguishable
  from suite-caused movement; the exclusions are plumbing-derived ref
  namespaces, never a ``wl-*`` name pattern. The guard's own root branch
  stays monitored except for the concurrency-excluded ``refs/heads/dev``. The
  GIT_DIR-leak incident class is still caught via config rewrites of any
  non-excluded key, working-tree contamination, and moves of non-excluded
  refs (SA-0MUIA3OE40001QJX); a ``refs/heads/dev``-only ref move is no longer
  a standalone incident signal (see ``docs/dev/test-isolation.md`` §8.4).

**Working-tree attribution rule (SA-0MUIJAHXS003M7BG).** The working-tree
surface is attributed by *start-of-run provenance*, derived only from git
plumbing — the two ``git status --porcelain=v1 -uall`` snapshots: a status
entry that **disappeared** was non-clean at snapshot start and is clean at
snapshot end, i.e. the path was **resolved inside the guard window**. A
hermetic suite never commits, checks out or reverts, so such a resolution is
concurrent churn (a sibling session committing work it had already dirtied in
the shared checkout) and is excluded from the verdict, reported instead as an
informational note (``EXCLUDED_CHURN_NOTE_PREFIX``, AC1/AC3). Everything else
stays a mutation (AC2): a path that was **clean at snapshot start** and is
modified, deleted or created at snapshot end, and a path that is *still* dirty
with a different status code. Derivations are status codes and snapshot
provenance only — never a ``wl-*`` or path-name pattern (AC4).

Pinned blind spots (AC4, ``TestWorkingTreeAttributionBoundaries``): a suite
that reverts or deletes another session's *uncommitted* work is masked (the
path was pre-dirty, so its resolution is attributed), the clean→dirty
false-positive direction is deliberately retained, and a content-only change
to an already-dirty path is invisible to a status-code surface.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
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

#: Worklog data namespace. These refs are managed by the ``wl`` tool (they can
#: be fetched/reset during a test run) and are not part of the source checkout,
#: so they are excluded from the mutation surface to avoid false positives.
WORKLOG_REF_PREFIX = "refs/worklog/"

#: Exact ref excluded because concurrent collaborative movement is
#: indistinguishable from suite-caused movement (WL-0MV5MYBKJ0033P8U).
#: ``refs/heads/dev`` is the shared integration branch: sibling agents'
#: ``implement.py finish`` fast-forwards it on the shared main checkout while
#: this suite runs, so its movement is never a hermetic-fixture signal. The
#: incident class stays detectable through the config / working-tree /
#: non-excluded-ref surfaces (see docs/dev/test-isolation.md §8).
CONCURRENCY_EXCLUDED_REF_EXACT = "refs/heads/dev"

#: Remote-tracking ref namespace excluded for the same reason: on a multi-agent
#: host, concurrent fetch/push moves ``refs/remotes/origin/dev`` and every other
#: ``refs/remotes/origin/*`` ref while the suite runs; only ``fetch``/``pull``/
#: ``push`` can ever move them, never a hermetic fixture. Derived from git
#: plumbing namespaces (never a ``wl-*`` name pattern) so the incident's own
#: fixture branches remain detectable.
CONCURRENCY_EXCLUDED_REF_NAMESPACES: tuple[str, ...] = ("refs/remotes/origin/",)

#: Prefix of the informational note emitted when a working-tree difference is
#: attributed to concurrent churn and therefore excluded from the verdict
#: (SA-0MUIJAHXS003M7BG AC3). Emitted — never silent — so a reviewer can tell
#: an excluded foreign change from a clean run.
EXCLUDED_CHURN_NOTE_PREFIX = "excluded: concurrently resolved working-tree change"


def _is_concurrency_excluded_ref(ref: str) -> bool:
    """True when *ref* is a concurrency-excluded namespace (WL-0MV5MYBKJ0033P8U).

    Applies to both the exact integration branch and the remote-tracking
    namespace, so ``diff_snapshots`` and ``fingerprint`` filter the same set
    and cannot drift.
    """
    if ref == CONCURRENCY_EXCLUDED_REF_EXACT:
        return True
    return any(ref.startswith(prefix) for prefix in CONCURRENCY_EXCLUDED_REF_NAMESPACES)


class GitSandboxError(RuntimeError):
    """Raised when a sandbox invariant is violated (e.g. a non-hermetic path)."""


# ---------------------------------------------------------------------------
# Environment isolation and git execution
# ---------------------------------------------------------------------------


def scrub_repository_overrides(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a copy of *env* with every repository-override variable removed.

    This is the single shared choke point every production path that spawns a
    subprocess which may invoke ``git`` must route its environment through
    (SA-0MUIA3OE40001QJX). Git honours ``GIT_DIR`` and friends over ``cwd``,
    so a leaked value makes an otherwise-isolated fixture operate on the
    leaked repository — the mechanism behind the 2026-09-24 and 2026-09-26
    live-checkout incidents.

    Args:
        env: Mapping to scrub (defaults to ``os.environ``). Neither the input
            mapping nor ``os.environ`` is mutated.

    Returns:
        A new ``dict`` containing every entry of *env* except the names in
        :data:`REPOSITORY_OVERRIDE_ENV_VARS`.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    cleaned = dict(source)
    for name in REPOSITORY_OVERRIDE_ENV_VARS:
        cleaned.pop(name, None)
    return cleaned


def is_worktree_git_dir(
    git_dir: str | os.PathLike[str],
    *,
    top: str | os.PathLike[str] | None = None,
) -> bool:
    """Return ``True`` when *git_dir* is a worktree-managed git directory.

    Git sets ``GIT_DIR`` to ``<main_checkout>/.git/worktrees/<name>`` when a
    hook runs from a linked worktree, so the value alone is not evidence of a
    leaked repository override. This predicate recognises that shape so the
    release-gate fail-fast can allow a legitimate worktree ``GIT_DIR`` while
    still refusing a genuine leak (SA-0MUMR3QPM002VK7M).

    Args:
        git_dir: Candidate ``GIT_DIR`` value.
        top: The **main checkout** root. When omitted, it is resolved from the
            current process directory via :func:`main_checkout_root` (the
            common production case: the pre-push hook runs in the linked
            worktree).

    Returns:
        ``True`` when *git_dir* resolves under
        ``<main_checkout>/.git/worktrees/``.
    """
    if top is None:
        top = main_checkout_root(os.getcwd())
    if top is None:
        return False
    worktrees_dir = Path(top).resolve() / ".git" / "worktrees"
    try:
        Path(git_dir).resolve().relative_to(worktrees_dir)
    except (ValueError, OSError):
        return False
    return True


def repository_override_vars_present(
    env: Mapping[str, str] | None = None,
    *,
    top: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Return the repository-override variable names present in *env*.

    Used by the runner diagnostics (log which variables were stripped, never
    their values) and by the release-gate fail-fast check. Defaults to
    ``os.environ``.

    A worktree-managed ``GIT_DIR`` (see :func:`is_worktree_git_dir`) is **not**
    reported: git sets it for pre-push hooks run from a linked worktree, and
    the release gate must run there without the opt-out override
    (SA-0MUMR3QPM002VK7M). Every other override value is still reported.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    present: list[str] = []
    for name in REPOSITORY_OVERRIDE_ENV_VARS:
        if name not in source:
            continue
        if name == "GIT_DIR" and is_worktree_git_dir(source[name], top=top):
            continue
        present.append(name)
    return tuple(present)


def sanitized_git_env() -> dict[str, str]:
    """A copy of the process environment with repository overrides removed."""
    return scrub_repository_overrides(os.environ)


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


def _parse_worktrees(
    output: str, own_root: Path | None = None
) -> tuple[dict[str, str], set[str]]:
    """Parse ``git worktree list --porcelain``.

    Returns ``(branches_by_path, excluded_refs)`` where *excluded_refs* are the
    branch refs checked out in registered worktrees **other than the guard's
    own root** (*own_root*; ``None`` = nothing is excluded). This is the
    ownership-based exclusion (SA-0MV8311B3005DNG8 AC1): a ref checked out in
    any registered worktree that is not the root the guard runs from is
    concurrent-infrastructure churn — regardless of the worktree's directory
    name (``.worklog/worktrees/``, ``.worklog/tmp-worktree-*``, or anywhere
    else). The guard root's own branch stays monitored.
    """
    branches: dict[str, str] = {}
    path: str | None = None
    branch: str | None = None
    excluded: set[str] = set()
    own = Path(own_root).resolve() if own_root is not None else None

    def _flush() -> None:
        if path is None:
            return
        branches[path] = branch or ""
        if branch and own is not None:
            try:
                if Path(path).resolve() != own:
                    excluded.add(branch)
            except OSError:
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


def _retained_worktree_tracked_branches(config: dict[str, str]) -> set[str]:
    """Branches whose ``--track`` worktree-ownership signal survives removal.

    ``git worktree add --track -b <name> ... <local-base>`` writes
    ``branch.<name>.remote=.`` and ``branch.<name>.merge=refs/heads/<base>``;
    ``git worktree remove`` keeps the branch and that config. A
    register→remove cycle that completes entirely inside the guard window
    therefore leaves the branch's ownership config as the only observable
    signal (SA-0MV8311B3005DNG8 AC3 — C1 tracer test
    ``test_register_remove_cycle_wholly_inside_window_is_invisible``).

    Returns the ``refs/heads/<name>`` set for every local branch that carries
    the self-tracking signature. Deleting a worktree never removes it, and a
    plain ``git branch --track <name> <local-base>`` writes the same keys, so
    this is exactly the ownership signal the C1 boundary test documents
    ("the ownership signal must as well").
    """
    tracked: set[str] = set()
    for key, value in config.items():
        prefix = "branch."
        suffix = ".remote"
        if not (key.startswith(prefix) and key.endswith(suffix)):
            continue
        # A worktree created against a local base records the repository
        # itself as the tracking remote (``remote=.``); ``remote=origin`` etc.
        # is an ordinary branch and is not worktree churn.
        if value != ".":
            continue
        name = key[len(prefix):-len(suffix)]
        # Require the merge half of the tracking pair; a bare remote=. is not
        # the worktree-created shape.
        if f"branch.{name}.merge" in config:
            tracked.add(f"refs/heads/{name}")
    return tracked


def _remote_tracking_refs_for_branch(
    refs: dict[str, str], branch_ref: str
) -> set[str]:
    """Remote-tracking refs resolving to an excluded branch (parent AC2).

    A concurrent ``implement.py finish`` fetches before fast-forwarding, moving
    ``refs/remotes/origin/<parent>`` in addition to the local branch. To treat
    the whole fast-forward as churn, every remote-tracking ref whose short
    name matches the excluded branch is excluded too. Remote-tracking refs of
    non-excluded branches remain observable.
    """
    if not branch_ref.startswith("refs/heads/"):
        return set()
    short = branch_ref[len("refs/heads/"):]
    return {
        ref for ref in refs
        if ref.startswith("refs/remotes/")
        and ref.removeprefix("refs/remotes/").split("/", 1)[-1] == short
    }


def main_checkout_root(
    path: str | os.PathLike[str], top: str | None = None
) -> Path | None:
    """Resolve the main checkout owning the linked worktree at *path*.

    Agent worktrees live under ``<main_checkout>/.worklog/worktrees/``. When
    the snapshot is taken from a linked worktree (as ``implement.py finish``
    does), ``path``'s toplevel is the linked worktree, not the main checkout,
    so the exclusion directory must be anchored on the main checkout. Anchored
    on git plumbing — ``git rev-parse --git-common-dir`` (its parent when it is
    named ``.git``) — with the first ``git worktree list --porcelain`` entry as
    a validated fallback; **never** a ``wl-*`` name pattern. *top* may be
    supplied to reuse a toplevel already resolved by the caller. Returns None
    when *path* is not inside a git work tree.
    """
    resolved = Path(path)
    if top is None:
        top = git_toplevel(resolved)
    if top is None:
        return None

    proc = run_git(resolved, ["rev-parse", "--git-common-dir"])
    if proc.returncode == 0 and proc.stdout.strip():
        common = Path(proc.stdout.strip())
        if not common.is_absolute():
            common = Path(top) / common
        common = common.resolve()
        return common.parent if common.name == ".git" else common

    listing = run_git(resolved, ["worktree", "list", "--porcelain"])
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = Path(line[len("worktree "):].strip()).resolve()
            if candidate.is_dir():
                return candidate
    return None


#: Role label for the checkout a guarded run was started in.
GUARDED_ROOT_ROLE = "guarded"

#: Role label for the main checkout of a guarded root that is a linked worktree.
MAIN_CHECKOUT_ROLE = "main_checkout"


def guarded_roots(root: str | os.PathLike[str]) -> tuple[Path, ...]:
    """The checkouts a guarded run started at *root* must watch.

    The result is ``(<guarded toplevel>,)`` for an ordinary checkout, and
    ``(<guarded toplevel>, <main checkout>)`` when *root* is a **linked
    worktree** (WL-0MVPE2BY9001CR9O). The main checkout is resolved from git
    plumbing via :func:`main_checkout_root` — never from a ``wl-*`` directory
    name — so any registered worktree, wherever it lives, gets the same
    coverage.

    A non-git path (or a path git cannot resolve) yields just the resolved
    *root*, so the helper is a no-op for non-repository callers.
    """
    resolved = Path(root).resolve()
    top = git_toplevel(resolved)
    if top is None:
        return (resolved,)
    top_path = Path(top).resolve()
    main = main_checkout_root(resolved, top=top)
    if main is None:
        return (top_path,)
    main_path = Path(main).resolve()
    if main_path == top_path:
        return (top_path,)
    return (top_path, main_path)


def snapshot_guarded_roots(
    root: str | os.PathLike[str],
    *,
    include_status: bool = True,
) -> dict[str, Any]:
    """Snapshot every checkout a guarded run started at *root* can reach.

    Each entry carries the root's ``role`` (:data:`GUARDED_ROOT_ROLE` or
    :data:`MAIN_CHECKOUT_ROLE`), its ``path`` and the per-root
    :func:`snapshot_repo_state` result. The per-root exclusions (ownership-based
    worktree refs, retained ``branch.<name>.*`` config, ``refs/heads/dev`` and
    ``refs/remotes/origin/*``) therefore apply to **each** root independently,
    so concurrent `dev`/`origin/*` movement or a worktree registered/removed
    mid-run stays invisible on both sides.

    Cost is git plumbing only (one plumbing snapshot per checkout, no per-test
    work); ``include_status=False`` keeps the cheap refs+config fingerprint
    shape for the per-test guard.

    Returns ``{"root": <guarded root>, "roots": [entry, ...]}``.
    """
    entries: list[dict[str, Any]] = []
    paths = guarded_roots(root)
    main_path = paths[1] if len(paths) > 1 else None
    for path in paths:
        role = GUARDED_ROOT_ROLE
        if main_path is not None and path == main_path:
            role = MAIN_CHECKOUT_ROLE
        entries.append(
            {
                "role": role,
                "path": str(path),
                "snapshot": snapshot_repo_state(path, include_status=include_status),
            }
        )
    return {"root": str(Path(root).resolve()), "roots": entries}


def fingerprint_guarded_roots(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    """A cheap, hashable fingerprint of every root in a guarded snapshot.

    Used by the per-test guard as a short-circuit trigger only: because each
    entry's :func:`fingerprint` already ignores the ownership-based exclusions,
    registering or removing an agent worktree mid-run cannot move it. Inclusion
    of ``role``/``path`` keeps the two roots distinct even when their refs and
    config are identical.
    """
    return tuple(
        (entry.get("role", ""), entry.get("path", ""), fingerprint(entry["snapshot"]))
        for entry in snapshot.get("roots", ())
    )


def diff_guarded_roots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Diff two :func:`snapshot_guarded_roots` results, root by root.

    Returns ``{"changed": bool, "roots": [{"role", "path", "diff"}, ...]}``
    where each ``diff`` is the :func:`diff_snapshots` result for that root, so a
    caller can report **which** checkout was mutated. ``changed`` is the
    disjunction over all roots; a root present in only one snapshot (no change
    of roots within a run is expected, but a worktree registered mid-run must
    never fail the run) is treated as unchanged. Each root's ``diff`` carries
    its own attribution, so pass the whole result to
    :func:`excluded_churn_notes` to render the informational note (AC3).
    """
    roots: list[dict[str, Any]] = []
    before_roots = before.get("roots", ())
    after_roots = after.get("roots", ())
    changed = False
    for before_entry, after_entry in zip(before_roots, after_roots):
        diff = diff_snapshots(before_entry["snapshot"], after_entry["snapshot"])
        changed = changed or diff["changed"]
        roots.append(
            {
                "role": before_entry.get("role", ""),
                "path": before_entry.get("path", ""),
                "diff": diff,
            }
        )
    return {"changed": changed, "roots": roots}


def describe_guarded_diff(diff: dict[str, Any]) -> str:
    """Render every mutated path of a :func:`diff_guarded_roots` result.

    With a single root this is exactly :func:`describe_diff` (no extra noise for
    the common, worktree-free case). With a guarded root plus its main checkout
    each changed checkout is announced by path, so the failure message names
    *every* mutated main-checkout path instead of hiding it behind one generic
    heading.
    """
    roots = diff.get("roots", ())
    changed_roots = [entry for entry in roots if entry["diff"]["changed"]]
    if not changed_roots:
        return "  (no differences)"
    if len(roots) == 1:
        return describe_diff(changed_roots[0]["diff"])
    lines: list[str] = []
    for entry in changed_roots:
        label = (
            "main checkout"
            if entry.get("role") == MAIN_CHECKOUT_ROLE
            else "guarded checkout"
        )
        lines.append(f"  in {label} {entry.get('path', '')}:")
        lines.extend(describe_diff(entry["diff"]).splitlines())
    return "\n".join(lines)


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

    The ``status`` map itself is the **start-of-run provenance**
    :func:`diff_snapshots` uses to attribute concurrent churn
    (SA-0MUIJAHXS003M7BG): a path present here was already dirty, so its
    disappearance later in the run is a resolution, not suite damage.
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
        own_root=top,
    )
    # The guard root's own checked-out branch must stay monitored even if its
    # ``--track`` worktree registration left a ``branch.<name>.remote=.``
    # config (SA-0MV8311B3005DNG8 AC1).
    own_branch = ""
    for wt_path, wt_branch in worktrees.items():
        try:
            if Path(wt_path).resolve() == Path(top).resolve():
                own_branch = wt_branch
                break
        except OSError:
            continue
    # Retained worktree-track branches (SA-0MV8311B3005DNG8 AC3): a
    # register→remove cycle wholly inside the guard window leaves the branch
    # and its ``--track`` config without a ``git worktree list`` row; the
    # ``branch.<name>.remote=.`` config is the only surviving ownership signal.
    retained = _retained_worktree_tracked_branches(config)
    if own_branch:
        retained.discard(own_branch)
    # Remote-tracking refs of excluded branches (parent AC2): a concurrent
    # finish fetches before fast-forwarding, so ``refs/remotes/origin/<parent>``
    # moves alongside the local branch and must be treated as the same churn.
    churn = set(excluded) | retained
    remote_tracking: set[str] = set()
    for branch_ref in list(churn):
        remote_tracking |= _remote_tracking_refs_for_branch(refs, branch_ref)
    excluded = churn | remote_tracking
    return {
        "root": str(resolved),
        "git": True,
        "refs": refs,
        "config": config,
        "status": status,
        "worktrees": worktrees,
        "excluded_refs": excluded,
    }


def _excluded_branch_config_prefixes(excluded_refs: set[str]) -> tuple[str, ...]:
    """Local-config key prefixes owned by excluded agent-worktree branches.

    ``git worktree add --track -b <branch>`` writes
    ``branch.<branch>.remote`` and ``branch.<branch>.merge``; registering or
    removing an agent worktree therefore churns exactly that config section.
    Deriving the prefixes from ``excluded_refs`` anchors the config exclusion
    to the same ``git worktree list`` evidence as the ref exclusion (never a
    ``wl-*`` name pattern).
    """
    prefixes = [
        f"branch.{ref[len('refs/heads/'):]}."
        for ref in excluded_refs
        if ref.startswith("refs/heads/")
    ]
    return tuple(sorted(prefixes))


def _filter_excluded_config(
    config: dict[str, str], prefixes: tuple[str, ...]
) -> dict[str, str]:
    """Drop local-config keys owned by excluded agent-worktree branches."""
    if not prefixes:
        return dict(config)
    return {key: value for key, value in config.items() if not key.startswith(prefixes)}


def fingerprint(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    """A cheap, hashable fingerprint of a snapshot's refs + local config.

    Refs checked out in registered agent worktrees (``excluded_refs``) and the
    ``branch.<name>.*`` config those worktrees own are ignored entirely — the
    excluded set's own membership and its churn are invisible — matching
    :func:`diff_snapshots`. Agent-worktree registration/removal (frequent on
    shared multi-agent hosts) therefore cannot move the fingerprint and trip
    the per-test guard, even before the authoritative diff is consulted
    (F5 AC4/AC7, SA-0MUINEW6X0034C65). Used by the per-test guard as a cheap
    short-circuit; the full diff remains the sole authority for failure.
    """
    excluded = set(snapshot.get("excluded_refs", set()))
    prefixes = _excluded_branch_config_prefixes(excluded)
    refs = tuple(
        sorted(
            (ref, sha)
            for ref, sha in snapshot.get("refs", {}).items()
            if ref not in excluded and not _is_concurrency_excluded_ref(ref)
        )
    )
    config = tuple(
        sorted(
            _filter_excluded_config(snapshot.get("config", {}), prefixes).items()
        )
    )
    return (refs, config)


def diff_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Report the differences between two snapshots.

    Refs checked out in registered agent worktrees (``excluded_refs``) and the
    ``branch.<name>.*`` config those worktrees own are ignored, using the
    union of both snapshots' excluded sets so a worktree appearing or
    disappearing mid-run is invisible on either side.

    Working-tree differences are additionally **attributed** by start-of-run
    provenance (SA-0MUIJAHXS003M7BG): a status entry present in ``before`` and
    absent from ``after`` was resolved during the run (concurrent churn) and is
    reported under ``excluded.status.removed`` instead of failing the diff —
    see :func:`excluded_churn_notes`. ``changed`` is the attributed verdict, so
    it is ``False`` when the only difference is such a resolution.

    Returns ``{"changed": bool, "refs": ..., "config": ..., "status": ...,
    "excluded": ...}`` where ``status`` lists the verdict-bearing
    (``added``/``changed``) working-tree entries and each other category lists
    ``added``/``removed``/``changed`` entries.
    """
    if not before.get("git", True) or not after.get("git", True):
        return {
            "changed": False,
            "refs": {},
            "config": {},
            "status": {},
            "excluded": {"status": {"removed": {}}},
        }

    excluded = set(before.get("excluded_refs", set())) | set(
        after.get("excluded_refs", set())
    )
    prefixes = _excluded_branch_config_prefixes(excluded)
    before_refs = {
        k: v
        for k, v in before["refs"].items()
        if k not in excluded and not _is_concurrency_excluded_ref(k)
    }
    after_refs = {
        k: v
        for k, v in after["refs"].items()
        if k not in excluded and not _is_concurrency_excluded_ref(k)
    }
    before_config = _filter_excluded_config(before["config"], prefixes)
    after_config = _filter_excluded_config(after["config"], prefixes)

    refs_added = {k: v for k, v in after_refs.items() if k not in before_refs}
    refs_removed = {k: v for k, v in before_refs.items() if k not in after_refs}
    refs_moved = {
        k: (before_refs[k], after_refs[k])
        for k in before_refs
        if k in after_refs and before_refs[k] != after_refs[k]
    }

    config_added = {k: v for k, v in after_config.items() if k not in before_config}
    config_removed = {
        k: v for k, v in before_config.items() if k not in after_config
    }
    config_changed = {
        k: (before_config[k], after_config[k])
        for k in before_config
        if k in after_config and before_config[k] != after_config[k]
    }

    before_status = before["status"]
    after_status = after["status"]
    status_added = {k: v for k, v in after_status.items() if k not in before_status}
    resolved_status = {
        k: v for k, v in before_status.items() if k not in after_status
    }
    status_changed = {
        k: (before_status[k], after_status[k])
        for k in before_status
        if k in after_status and before_status[k] != after_status[k]
    }

    # Working-tree attribution (SA-0MUIJAHXS003M7BG, rule (a)). A status entry
    # that disappeared was already non-clean at snapshot start and is clean at
    # snapshot end: the path was resolved *inside* the guard window. A hermetic
    # suite never commits/checks out/reverts, so only a concurrent session (a
    # sibling committing work it had itself dirtied in the shared checkout) can
    # have resolved it — that is churn, not damage, and it is excluded from the
    # verdict but reported as a note (`excluded_churn_notes`).
    #
    # Deliberately retained as incidents (AC2):
    #   * a path added to the status surface — clean at snapshot start, now
    #     modified / deleted / newly created (the clean→dirty direction);
    #   * a path that is still dirty with a different code — no resolution is
    #     observable, so no attribution is provable.
    # Provenance is the start snapshot's own status codes (git plumbing), never
    # a path-name or `wl-*` pattern (AC4).
    excluded_status = {"removed": resolved_status}

    changed = bool(
        refs_added
        or refs_removed
        or refs_moved
        or config_added
        or config_removed
        or config_changed
        or status_added
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
            "changed": status_changed,
        },
        "excluded": {"status": excluded_status},
    }


def excluded_working_tree_churn(diff: dict[str, Any]) -> dict[str, str]:
    """``path -> status code`` of working-tree differences excluded by attribution.

    These entries were non-clean at snapshot start and clean at snapshot end,
    so the path was resolved during the run — concurrent churn that must not
    fail the run (SA-0MUIJAHXS003M7BG AC1). They never appear in
    ``diff["status"]`` and therefore never set ``diff["changed"]``, but they
    are never silent either: :func:`excluded_churn_notes` renders them (AC3).
    """
    return dict(diff.get("excluded", {}).get("status", {}).get("removed", {}))


def excluded_churn_notes(diff: dict[str, Any]) -> list[str]:
    """Informational notes for working-tree churn excluded from the verdict.

    Accepts either a :func:`diff_guarded_roots` result (``{"roots": [...]}``)
    or a bare :func:`diff_snapshots` result, so both guards — and the outer
    ``guarded_run_all`` consumer — share one renderer. Each note names the
    checkout and every excluded path with its status code, prefixed by
    :data:`EXCLUDED_CHURN_NOTE_PREFIX` (AC3). Empty list = nothing excluded.
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    if "roots" in diff:
        for entry in diff.get("roots", ()):
            entries.append((str(entry.get("path", "")), entry.get("diff") or {}))
    else:
        entries.append(("", diff))

    notes: list[str] = []
    for checkout, root_diff in entries:
        where = f" in {checkout}" if checkout else ""
        for path, code in sorted(excluded_working_tree_churn(root_diff).items()):
            notes.append(f"{EXCLUDED_CHURN_NOTE_PREFIX}{where}: {path} [{code}]")
    return notes


def describe_diff(diff: dict[str, Any]) -> str:
    """Render a human-readable, actionable summary of a snapshot diff.

    Renders the **verdict-bearing** differences only: working-tree churn
    excluded by attribution (SA-0MUIJAHXS003M7BG) is intentionally absent here
    because this renderer produces failure messages — the excluded paths are
    announced by :func:`excluded_churn_notes` instead.
    """
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
