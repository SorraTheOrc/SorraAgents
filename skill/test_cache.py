"""Per-repo cache for full test-suite runs.

The test skill, ship release gates, and implement loops repeatedly execute the
same full suite at the same git state within minutes (observed: ``npm test``
ran twice back-to-back, 405s then 101s, purely to grep summary lines). This
module adds a caching layer so repeated verification is cheap:

- **Per-repo, gitignored storage**: ``<repo>/.worklog/cache/`` when a
  ``.worklog`` directory exists, otherwise ``<repo>/.git/test-cache/``
  (resolved worktree-aware via ``git rev-parse --git-dir``). Non-git repos
  fall back to ``<repo>/.test-cache/``.
- **Keyed by normalized command + git-state fingerprint**: the fingerprint is
  HEAD sha plus a content hash of every path ``git status --porcelain``
  reports as dirty (tracked modifications, staged changes, deletions and
  untracked files), resolved against the repo containing the working
  directory (worktree-aware). Hashing file **contents** — not just the
  status text, which lists paths only — means editing a file that is
  *already* dirty yields a new fingerprint too (AH-0MVFGIV6700329SZ). The
  fingerprint always describes the repository containing the working
  directory: repository-override variables (``GIT_DIR``, ``GIT_WORK_TREE``,
  ``GIT_CONFIG*``, …) are scrubbed before every git call, so a leaked value
  cannot make the cache describe a different repository
  (SA-0MUIA3OE40001QJX).
- **Fail closed on an indeterminate state**: when the state cannot be
  determined — a failing/locked/corrupt index, a git timeout under load, an
  unreadable dirty path — the run is *not* looked up and *not* stored. A
  degraded, state-independent fallback fingerprint would make every dirty
  tree share one key and serve stale green results for the whole TTL
  (WL-0MVPO9P610055UQ3). Correctness first: an unfingerprintable run simply
  executes fresh.
- **Keyed on the post-run state**: the entry records the state the run
  **left behind**, because that is the state every later consumer observes
  (the next run's pre-check, ``query_cached()``, ``--summary``). Keying on
  the pre-run state made a completed run unqueryable whenever transient
  churn (plugin/dist/scratch files) disappeared while it ran
  (WL-0MVPO9P610055UQ3). When the tree moved during the run, the pre-run
  state is still recorded as ``git_state_at_start`` metadata for diagnosis;
  it never keys the entry.
- **2-hour TTL** (configurable): expired entries are re-run and replaced.
  Failed (non-zero-exit) runs use a short 5-minute TTL instead
  (SA-0MSJELL44009XYIL) so transient infra failures are not re-served as
  current results.
- **Correctness first**: a hit requires the git state to match AND the run to
  be within the TTL. Corrupt/unreadable entries degrade to a fresh run and
  never raise into the caller. ``force`` / ``no_cache`` bypass the cache.
- **Read-only query mode**: ``query_cached()`` + ``summary_lines()`` return
  summary lines from a cached run without executing anything (used by the
  ``run_tests.py --summary`` flag and available to read-only consumers such as
  the audit skill — operator-attested path SA-0MSGLAVCZ002LVZ4 and the
  automatic full-suite verification path SA-0MSIU5HFI0024D7W).

Entry layout (stable, documented for read-only consumers)::

    <cache_dir>/<key>/
        metadata.json   {"version":1, "command":..., "git_state":...,
                         "git_state_at_start":...,
                         "scope": "full"|"changed",
                         "test_type": "full"|"unit"|...,
                         "fingerprint_kind": "worktree"|"tree",
                         "interpreter": "<pytest interpreter/exe>",
                         "exit_code":..., "completed_at": <epoch float>}
        stdout.txt      full stdout of the run
        stderr.txt      full stderr of the run

``git_state`` is the key the entry is stored under — the state the run left
behind (see above). ``git_state_at_start`` records the state the run started
in and is informational only (it differs when the tree moved during the run,
which is exactly the transient-churn case that used to make entries
unqueryable); entries predating the field omit it.

``interpreter`` records the concrete interpreter/executable that actually ran
pytest (empty for non-pytest commands and for older entries predating the
field). The cache key stays git-state + command + type — the interpreter is
informational metadata, so an entry produced under a different interpreter is
still visible and auditable rather than silently invalidated
(SA-0MVK154IE007OS3D AC2).

``scope`` records whether the run was a full-suite run (``"full"`` — the
historical default, so older entries without the field read as full) or a
changed-file partial run (``"changed"`` — distinct cache keys from the full
suite anyway, but the field lets read-only consumers such as the audit skill
reject partial runs as full-suite evidence even if command sets ever
converge).

``test_type`` records the typed profile a run belongs to (``"full"`` — the
historical default, so older entries without the field read as full). Only
``test_type == "full"`` populates the audit-accepted full-suite entry; other
types use independent cache keys (the type is part of the key) so a partial
typed run can never satisfy a "full test suite passes" acceptance criterion.

``fingerprint_kind`` records *which kind* of fingerprint keys the entry —
``"worktree"`` (the default: :func:`compute_git_state`, HEAD plus the loose
working-tree contents) or ``"tree"`` (a would-be-commit tree SHA computed by
:func:`compute_tree_state`). It is the entry's provenance: a consumer that asks
for a tree fingerprint is never served an entry recorded for a loose state, and
an entry that predates the field reads as ``"worktree"`` (historical
default), so a tree request misses it and fails closed rather than accepting a
verdict recorded for a differently-identified tree (SA-0MVL2NH1G0094FA2 AC4).

Writes are atomic (temp file + ``os.replace``); no locking is used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shared.git_sandbox import git_env_for_repo
from test_runner import (
    executable_test_command,
    is_pytest_command,
    log_repository_override_scrub,
    normalize_test_command,
    resolve_pytest_executable,
    subprocess_env_with_scrubbed_overrides,
)

LOG = logging.getLogger("skill.test_cache")


class GitStateUnavailable(RuntimeError):
    """The working tree's git state could not be fully determined.

    A cache verdict is only meaningful for a state built from every input
    (HEAD plus the content of every dirty path). Raised by
    :func:`compute_git_state` in strict mode when git cannot report that
    input — a failing/locked/corrupt index, a git timeout under load, a
    repository whose work tree cannot be resolved — or when a dirty path's
    content cannot be read. Callers that can serve a *stale* result
    (:func:`run_cached`, :func:`query_cached`) turn this into "no cache":
    the run executes and stores nothing, because a degraded fingerprint makes
    every dirty tree share one key (WL-0MVPO9P610055UQ3).
    """


#: Fingerprint provenance kinds. ``worktree`` is the historical default (the
#: loose :func:`compute_git_state` fingerprint); ``tree`` is an explicitly
#: supplied would-be-commit tree SHA (:func:`compute_tree_state`).
FINGERPRINT_KIND_WORKTREE = "worktree"
FINGERPRINT_KIND_TREE = "tree"

DEFAULT_TTL_SECONDS = 2 * 60 * 60  # 2 hours (operator decision)
# Non-zero-exit runs get a much shorter TTL (SA-0MSJELL44009XYIL): a failed
# run may reflect a transient infra failure (e.g. /tmp disk quota) and must
# not be re-served as a current result for the full 2h TTL — it expires
# quickly so a later query/run re-executes fresh. Green runs keep the full
# TTL.
FAILED_RUN_TTL_SECONDS = 5 * 60  # 5 minutes
_CACHE_VERSION = 1
_KEY_LENGTH = 32
# Token separated from the command when hashing so that command+state pairs
# cannot collide (e.g. command "a b" + state "c" vs command "a" + state "b c").
_KEY_SEPARATOR = "\x00"

# A runner executes the (normalized) command and returns an object with
# ``stdout``, ``stderr`` and ``returncode`` attributes.
Runner = Callable[[str, str, int], Any]


# ---------------------------------------------------------------------------
# Git-state fingerprint (worktree-aware)
# ---------------------------------------------------------------------------


def _run_git(cwd: str, *args: str) -> str | None:
    """Run a git command in *cwd*, returning stdout or None on failure.

    The environment is :func:`shared.git_sandbox.git_env_for_repo` for *cwd*.
    Repository overrides are scrubbed (SA-0MUIA3OE40001QJX): git honours
    ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_CONFIG*`` over ``cwd``, so a leaked
    value made the fingerprint describe a *different* repository — one
    constant key for every state of the tree being tested, i.e. stale results
    for the whole TTL (WL-0MVPO9P610055UQ3). An ambient ``GIT_INDEX_FILE`` is
    honoured only when it belongs to *cwd*'s own repository: the finish gate
    exports a temporary would-be-commit index for the suite it runs
    (SA-0MVQHVGFR006W8LG), so the repository under test must be fingerprinted
    through it, while a fixture repository that inherited it would see a
    foreign index with no dirty paths and fingerprint every edit identically
    (SA-0MVTET8KQ0031R6Z). The fingerprint always describes the repository
    containing ``cwd``.
    """
    log_repository_override_scrub()
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=git_env_for_repo(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _has_git_directory(cwd: str) -> bool:
    """Whether *cwd* or any ancestor carries a ``.git`` directory or file.

    Distinguishes a genuinely non-git directory (documented TTL-only
    fallback) from a git checkout whose state git could not report (which
    must fail closed instead of falling back to a state-independent key).
    """
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate / ".git"):
            return True
    return False


def compute_git_state(cwd: str | Path | None = None, *, strict: bool = False) -> str:
    """Return a fingerprint of the git state at *cwd*.

    The fingerprint combines the HEAD commit sha with the **content** of every
    path the working tree reports as dirty — tracked modifications, staged
    changes, deletions and untracked files alike. ``git status --porcelain``
    alone is not enough: it records paths and status codes only, so a second
    edit to a file that is already dirty leaves the status text unchanged and
    would otherwise serve a stale cached result (AH-0MVFGIV6700329SZ).

    Git commands run with ``cwd`` as the working directory and a scrubbed
    environment, so the fingerprint reflects the *worktree containing cwd*
    (implement loops run suites inside git worktrees) and never a repository
    named by a leaked ``GIT_DIR``. Porcelain paths are repository-root
    relative, so content is resolved against ``git rev-parse --show-toplevel``.

    For non-git directories a stable per-directory fingerprint is returned so
    the cache still works (TTL-only invalidation), documented fallback.

    *strict* raises :class:`GitStateUnavailable` instead of degrading when the
    state cannot be determined: a git checkout whose work tree, HEAD or dirty
    paths cannot be read, or a dirty path whose content cannot be read. A
    caller that may serve a stale result must pass ``strict=True`` — a
    degraded fingerprint (HEAD only, or a per-directory constant) cannot
    distinguish two dirty trees and would serve one tree's verdict for the
    other (WL-0MVPO9P610055UQ3).
    """
    cwd = str(Path(cwd or os.getcwd()).resolve())
    in_git_checkout = _has_git_directory(cwd)
    toplevel = _run_git(cwd, "rev-parse", "--show-toplevel")
    if toplevel is None:
        if strict and in_git_checkout:
            raise GitStateUnavailable(
                f"git cannot report the work tree containing {cwd} "
                "(corrupt/locked index, timeout, or a broken repository)"
            )
        # Not a git repository: stable per-directory key.
        digest = hashlib.sha256(f"no-git:{cwd}".encode()).hexdigest()
        return digest[:16]

    head = _run_git(cwd, "rev-parse", "HEAD")
    if head is None:
        # An unborn branch (a repository with no commits yet) has no HEAD but
        # is otherwise fingerprinted normally; any other HEAD failure is an
        # indeterminate state.
        unborn = _run_git(cwd, "symbolic-ref", "-q", "HEAD") is not None
        if not unborn and strict:
            raise GitStateUnavailable(
                f"git cannot resolve HEAD in {cwd} (corrupt repository or git timeout)"
            )
        head_token = b"unborn" if unborn else b""
    else:
        head_token = head.strip().encode()

    hasher = hashlib.sha256()
    hasher.update(head_token)
    base = Path(toplevel.strip())
    for status, path in sorted(_status_entries(cwd, strict=strict)):
        hasher.update(b"\0status\0" + status.encode("utf-8", "surrogateescape"))
        _update_with_path_content(hasher, base, path, strict=strict)
    return hasher.hexdigest()


def compute_tree_state(
    index_file: str | Path, *, cwd: str | Path | None = None
) -> str:
    """Return the tree SHA (``git write-tree``) of the index at *index_file*.

    This is the fingerprint of a *would-be commit tree*: the gate stages the
    pending change set into an index (a temporary copy when it must not touch
    the real one) and the commit that follows produces exactly this tree.
    Unlike :func:`compute_git_state` it is not blind to generated, gitignored
    metadata — the tree records what the commit would contain, so a suite that
    reads on-disk generated files is bound to the tree it actually validated
    (SA-0MVL2NH1G0094FA2 AC4).

    Args:
        index_file: Path to the index to read — the repository's real
            ``.git/index`` or a temporary copy (never left modified here).
        cwd: Repository/worktree to run ``git`` in. Defaults to the current
            working directory; the finish gate passes its worktree explicitly.

    Returns:
        The 40-character tree SHA ``git write-tree`` reports for *index_file*.

    Raises:
        RuntimeError: when the index cannot be read or ``git write-tree``
            fails (missing repository, unreadable index). Failing loudly is
            deliberate: a silent fallback to the loose working-tree
            fingerprint would reintroduce the unattributable verdict this
            fingerprint exists to prevent.
    """
    resolved_index = str(Path(index_file).resolve())
    resolved_cwd = str(Path(cwd or os.getcwd()).resolve())
    # GIT_INDEX_FILE names the index explicitly and is *not* one of the
    # repository-override variables the shared scrub removes (F3,
    # SA-0MVQHV9Y3001RNNW), so it survives here.
    env = subprocess_env_with_scrubbed_overrides()
    env["GIT_INDEX_FILE"] = resolved_index
    try:
        proc = subprocess.run(
            ["git", "write-tree"],
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Cannot compute the tree state of index {resolved_index}: {exc}"
        ) from exc
    tree = proc.stdout.strip()
    if proc.returncode != 0 or not tree:
        raise RuntimeError(
            f"Cannot compute the tree state of index {resolved_index}: "
            f"{proc.stderr.strip() or 'git write-tree failed'}"
        )
    return tree


def _status_entries(cwd: str, *, strict: bool = False) -> list[tuple[str, str]]:
    """Return ``(status, path)`` for every dirty path in *cwd*.

    ``--untracked-files=all`` expands untracked directories into individual
    files so each file's content is fingerprinted. ``-z`` keeps paths
    NUL-terminated (no quoting) and places a rename/copy's original path in
    the field following the entry.

    With *strict* a failing ``git status`` raises :class:`GitStateUnavailable`
    rather than reporting an empty (and therefore state-independent) dirty
    set.
    """
    output = _run_git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if output is None:
        if strict:
            raise GitStateUnavailable(
                f"git cannot report the working tree state in {cwd} "
                "(corrupt/locked index, timeout, or a broken repository)"
            )
        return []
    fields = output.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if "R" in status or "C" in status:
            index += 1  # the rename/copy source path follows the entry
        entries.append((status, path))
    return entries


def _update_with_path_content(
    hasher: Any, base: Path, relpath: str, *, strict: bool = False
) -> None:
    """Feed the content at ``base/relpath`` into *hasher*.

    A missing path contributes a stable marker instead of raising (a deleted
    dirty file is a legitimate state). Symlinks hash their target. A directory
    (a submodule gitlink) hashes its own HEAD and status rather than walking
    into another repository; a plain directory falls back to a sorted
    recursive walk.

    With *strict* an unreadable path raises :class:`GitStateUnavailable`
    instead of contributing an "unreadable" marker: the marker is
    content-blind, so two different edits of an unreadable file would share
    one fingerprint and be served each other's cached verdict
    (WL-0MVPO9P610055UQ3).
    """
    path = base / relpath
    hasher.update(b"\0path\0" + relpath.encode("utf-8", "surrogateescape"))
    try:
        if path.is_symlink():
            target = os.readlink(path)
            hasher.update(b"\0link\0" + target.encode("utf-8", "surrogateescape"))
            return
        if path.is_dir():
            sub_head = _run_git(str(path), "rev-parse", "HEAD")
            if sub_head is not None:
                hasher.update(b"\0gitlink\0" + sub_head.strip().encode())
                sub_status = _run_git(str(path), "status", "--porcelain=v1") or ""
                hasher.update(
                    b"\0gitlink-status\0"
                    + sub_status.encode("utf-8", "surrogateescape")
                )
                return
            for child in sorted(path.rglob("*")):
                if child.is_dir() and not child.is_symlink():
                    continue
                _update_with_path_content(
                    hasher, base, child.relative_to(base).as_posix(), strict=strict
                )
            return
        if path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return
        hasher.update(b"\0missing")
    except OSError as exc:
        if strict:
            raise GitStateUnavailable(
                f"cannot read the dirty path {path} to fingerprint it ({exc})"
            ) from exc
        hasher.update(b"\0unreadable")


def is_worktree_clean(cwd: str | Path) -> bool | None:
    """Whether the checkout at *cwd* reports no dirty paths.

    Cleanliness is evaluated with the same ``git status`` call
    (:func:`_status_entries`, ``--untracked-files=all``) that
    :func:`compute_git_state` fingerprints, so "clean" here means exactly "the
    fingerprint is the commit's own state, with no dirty content folded in".
    That is the property the pre-push gate needs to bind a verdict to a pushed
    commit: a dirty checkout's fingerprint belongs to the dirty state, never to
    the commit it happens to have checked out (SA-0MVTRBT50005DUEH).

    Returns ``True`` when no path is dirty, ``False`` when at least one is, and
    ``None`` when the state cannot be reported at all (corrupt/locked index, a
    git timeout, a directory that is not a checkout). A caller that must not
    accept a stale verdict treats ``None`` as "not clean" — fail closed.
    """
    try:
        entries = _status_entries(str(Path(cwd).resolve()), strict=True)
    except GitStateUnavailable:
        return None
    return not entries


def _fingerprint_for_cache(cwd: str) -> str | None:
    """Return the fresh fingerprint for *cwd*, or None when indeterminate.

    The cache never serves or stores a verdict without a fresh, complete
    fingerprint: a degraded one cannot distinguish two dirty trees, so using
    it would return one tree's result for another (WL-0MVPO9P610055UQ3).
    """
    try:
        return compute_git_state(cwd, strict=True)
    except GitStateUnavailable as exc:
        LOG.warning(
            "test cache bypassed: the git state cannot be fingerprinted (%s)",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------


def cache_dir(repo_root: str | Path) -> Path:
    """Resolve the per-repo cache directory for *repo_root*.

    Resolution order:
      1. ``<repo>/.worklog/cache/`` when ``<repo>/.worklog`` exists
      2. ``<git-dir>/test-cache/`` — git dir resolved worktree-aware via
         ``git rev-parse --git-dir`` (``<repo>/.git`` in the main checkout;
         inside the shared git dir for worktrees). Anything under the git dir
         is untracked by definition.
      3. ``<repo>/.test-cache/`` — documented fallback for non-git repos.
    """
    repo_root = Path(repo_root).resolve()
    worklog = repo_root / ".worklog"
    if worklog.is_dir():
        return worklog / "cache"

    git_dir = _run_git(str(repo_root), "rev-parse", "--git-dir")
    if git_dir:
        git_dir_path = Path(git_dir.strip())
        if not git_dir_path.is_absolute():
            git_dir_path = repo_root / git_dir_path
        return git_dir_path / "test-cache"

    return repo_root / ".test-cache"


def cache_key(normalized_command: str, git_state: str, test_type: str = "full") -> str:
    """Return the deterministic cache key for a normalized command + state.

    *test_type* namespaces non-``full`` typed profiles so they never share a
    key with (and can never be mistaken for) a full-suite run. The ``full``
    type keeps the historical key (command + git state only), preserving all
    existing cached entries and audit queries.
    """
    raw = f"{normalized_command}{_KEY_SEPARATOR}{git_state}"
    if test_type != "full":
        raw += f"{_KEY_SEPARATOR}type:{test_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[: _KEY_LENGTH]


def _entry_dir(
    command: str,
    git_state: str,
    cwd: str | Path,
    test_type: str = "full",
) -> Path:
    """Resolve the entry directory for a command at *cwd*."""
    repo_root = Path(cwd or os.getcwd()).resolve()
    return cache_dir(repo_root) / cache_key(
        normalize_test_command(command), git_state, test_type
    )


# ---------------------------------------------------------------------------
# Lookup / store
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file + os.replace)."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def lookup(
    command: str,
    git_state: str,
    *,
    cwd: str | Path | None = None,
    ttl: float = DEFAULT_TTL_SECONDS,
    test_type: str = "full",
    fingerprint_kind: str = FINGERPRINT_KIND_WORKTREE,
) -> dict[str, Any] | None:
    """Return a cached run entry or None on miss/corruption/expiry.

    A hit requires the stored git state to match *git_state*, the stored
    ``test_type`` to match *test_type*, the stored ``fingerprint_kind`` to
    match *fingerprint_kind*, AND the run to be within *ttl* seconds old.
    Corrupt or unreadable entries are treated as a miss (fresh run) and never
    raise into the caller.

    *fingerprint_kind* names the fingerprint namespace *git_state* belongs to:
    ``"worktree"`` (default — the loose :func:`compute_git_state` fingerprint;
    entries that predate the provenance field read as worktree, so existing
    consumers keep their historical hits) or ``"tree"`` (an explicit
    would-be-commit tree SHA from :func:`compute_tree_state`; an entry without
    provenance metadata, or recorded for a loose state, is a miss so a verdict
    is never accepted for a differently-identified tree).

    Returns a dict with ``stdout``, ``stderr``, ``exit_code``, ``completed_at``
    (epoch float), ``command`` (normalized), ``git_state``, ``fingerprint_kind``,
    ``scope`` (``"full"`` for historical entries lacking the field — see module
    docs), ``test_type`` (``"full"`` for historical entries lacking the field)
    and ``cached: True``. ``interpreter`` names the concrete
    interpreter/executable recorded for a pytest run (empty for non-pytest
    commands and entries that predate the field).
    """
    entry_dir = _entry_dir(command, git_state, cwd or os.getcwd(), test_type)
    meta_path = entry_dir / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text())
        stdout = (entry_dir / "stdout.txt").read_text()
        stderr = (entry_dir / "stderr.txt").read_text()
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    if meta.get("version") != _CACHE_VERSION:
        return None
    if meta.get("git_state") != git_state:
        return None  # stale state: never return stale data silently
    if meta.get("test_type", "full") != test_type:
        return None  # type mismatch: never serve one profile as another
    # Provenance: an entry with no recorded kind is a loose-worktree entry
    # (the historical default). A tree request therefore always misses it,
    # and a worktree request never accepts a tree-bound verdict.
    stored_kind = meta.get("fingerprint_kind", FINGERPRINT_KIND_WORKTREE)
    if stored_kind != fingerprint_kind:
        return None
    completed_at = float(meta.get("completed_at", 0))
    exit_code = int(meta.get("exit_code", 0))
    # Failed runs expire after the short failed-run TTL; green runs keep the
    # caller's TTL (SA-0MSJELL44009XYIL).
    effective_ttl = ttl if exit_code == 0 else min(ttl, FAILED_RUN_TTL_SECONDS)
    if time.time() - completed_at > effective_ttl:
        return None  # expired TTL

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": int(meta.get("exit_code", 0)),
        "completed_at": completed_at,
        "command": meta.get("command", normalize_test_command(command)),
        "git_state": git_state,
        "fingerprint_kind": fingerprint_kind,
        "scope": meta.get("scope", "full"),
        "test_type": meta.get("test_type", "full"),
        "interpreter": meta.get("interpreter", ""),
        "cached": True,
    }


def store(
    command: str,
    git_state: str,
    *,
    cwd: str | Path | None = None,
    stdout: str,
    stderr: str,
    exit_code: int,
    completed_at: float | None = None,
    scope: str = "full",
    test_type: str = "full",
    interpreter: str = "",
    git_state_at_start: str = "",
    fingerprint_kind: str = FINGERPRINT_KIND_WORKTREE,
) -> Path:
    """Persist a run result, replacing any existing entry for the same key.

    Writes stdout/stderr first, then metadata last (atomic each); a partial
    write is therefore treated as corrupt by :func:`lookup` and degrades to a
    fresh run.

    *git_state* is the fingerprint **the entry is keyed on**. Callers that
    can serve the entry later (:func:`run_cached`) pass the *post-run* state,
    because that is the state the next consumer observes; keying on the
    pre-run state leaves a completed run unqueryable whenever transient churn
    disappears while it runs (WL-0MVPO9P610055UQ3).

    *git_state_at_start* optionally records the state the run started in. It
    is informational metadata only — it never keys the entry — and differs
    from *git_state* when the tree moved during the run.

    *scope* records whether the run covered the full suite (``"full"``,
    default — the only scope before scope-aware selection existed) or a
    changed-file subset (``"changed"``). Read-only consumers can therefore
    distinguish partial runs from full-suite evidence.

    *test_type* records the typed profile (``"full"``, default). Non-``full``
    types are keyed independently (see :func:`cache_key`) so they can never be
    mistaken for full-suite evidence.

    *interpreter* records the concrete interpreter/executable that ran pytest
    (empty for non-pytest commands). It is metadata only — it does not change
    the cache key — so the choice of interpreter is auditable per entry
    (SA-0MVK154IE007OS3D AC2).

    *fingerprint_kind* records the provenance of *git_state*: ``"worktree"``
    (default — the loose :func:`compute_git_state` fingerprint) or ``"tree"``
    (an explicit would-be-commit tree SHA). Read-only consumers and later
    lookups use it to refuse verdicts recorded for a different kind of tree.

    Returns the entry directory path.
    """
    entry_dir = _entry_dir(command, git_state, cwd or os.getcwd(), test_type)
    entry_dir.mkdir(parents=True, exist_ok=True)

    normalized = normalize_test_command(command)
    meta = {
        "version": _CACHE_VERSION,
        "command": normalized,
        "git_state": git_state,
        "git_state_at_start": git_state_at_start,
        "scope": scope,
        "test_type": test_type,
        "fingerprint_kind": fingerprint_kind,
        "interpreter": interpreter,
        "exit_code": int(exit_code),
        "completed_at": completed_at if completed_at is not None else time.time(),
    }
    _atomic_write(entry_dir / "stdout.txt", stdout)
    _atomic_write(entry_dir / "stderr.txt", stderr)
    _atomic_write(entry_dir / "metadata.json", json.dumps(meta, indent=2))
    return entry_dir


# ---------------------------------------------------------------------------
# Execution through the cache
# ---------------------------------------------------------------------------


def _default_runner(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Execute a normalized command string, capturing stdout/stderr.

    The pytest executable prefix is resolved through
    :func:`executable_test_command` so a bare canonical ``pytest`` command runs
    under the interpreter selected by ``resolve_pytest_command()`` — the
    runner's own interpreter when it can run pytest — instead of a PATH
    ``pytest`` that may be bound to a foreign interpreter
    (SA-0MVK154IE007OS3D). This is the path used by consumers that call
    :func:`run_cached` directly (the audit F3 auto-run and the implement
    finish gate), which previously spawned the bare command.

    The environment is the shared repository-override scrub (F2,
    SA-0MUIA3OE40001QJX): a leaked ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_CONFIG*``
    would otherwise override ``cwd`` in every ``git`` subprocess a test spawns
    and redirect fixture git operations at the live checkout. ``~/.local/bin``
    is prepended to PATH if not already present so that user-installed
    executables (e.g. pytest at ``~/.local/bin/pytest``) are found when the
    audit runner spawns suite commands in a restricted environment
    (SA-0MSUZAJPC003BS66).
    """
    log_repository_override_scrub()
    env = subprocess_env_with_scrubbed_overrides()
    return subprocess.run(
        shlex.split(executable_test_command(command)),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def run_cached(
    command: str,
    *,
    cwd: str | Path | None = None,
    force: bool = False,
    no_cache: bool = False,
    ttl: float = DEFAULT_TTL_SECONDS,
    timeout: int = 600,
    runner: Runner = _default_runner,
    scope: str = "full",
    test_type: str = "full",
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Run *command* through the cache.

    - A valid cached result (matching git state and type, within TTL) is
      returned without executing — verified by tests asserting the underlying
      command is not spawned.
    - A miss, changed git-state, or expired TTL runs the normalized command,
      stores the result, and returns it.
    - ``force=True`` bypasses lookup but still stores (refresh).
    - ``no_cache=True`` bypasses lookup AND storage (pure bypass).
    - The stored entry is keyed on the **post-run** state — the state the
      checkout is in once the run finishes, which is what the next consumer
      (the next run's pre-check, ``query_cached()``, ``--summary``) observes.
      Keying on the pre-run state made a completed run unqueryable whenever
      transient churn disappeared while it ran: the ``missing`` summary
      reported immediately after a green full-suite run
      (WL-0MVPO9P610055UQ3). The pre-run state is recorded as
      ``git_state_at_start`` metadata for diagnosis.
    - When the git state cannot be fingerprinted (corrupt/locked index, git
      timeout, unreadable dirty path) the cache is bypassed entirely: no
      lookup, no store, a fresh execution. A degraded fingerprint would make
      every dirty tree share one key and serve stale verdicts
      (WL-0MVPO9P610055UQ3).
    - *scope* (default ``"full"``) is recorded in the stored metadata so
      read-only consumers can tell partial runs from full-suite evidence.
    - *test_type* (default ``"full"``) selects the typed-profile cache
      namespace; non-``full`` types use independent keys and only ``full``
      populates the audit-accepted full-suite entry.
    - *fingerprint* (default ``None``) is an **explicit expected fingerprint**
      — normally a would-be-commit tree SHA from :func:`compute_tree_state`.
      When supplied it keys the entry and the lookup instead of the loose
      :func:`compute_git_state` value (which is then not computed at all), and
      the entry is recorded with ``fingerprint_kind="tree"``. A stored entry
      whose fingerprint differs, or that predates the provenance metadata, is
      a miss: a cached verdict is accepted only for a matching tree. Callers
      that pass nothing keep the historical loose-worktree keys byte for byte
      (test skill CLI, audit, pre-push).

    Returns a dict with ``stdout``, ``stderr``, ``exit_code``, ``completed_at``
    (epoch float), ``command`` (normalized), ``git_state`` (the state the
    entry is keyed on — empty when the cache was bypassed for an
    indeterminate state), ``fingerprint_kind`` (``"worktree"`` or ``"tree"``),
    ``scope``, ``test_type``, ``interpreter`` (the concrete
    interpreter/executable that ran pytest, empty for non-pytest commands) and
    ``cached`` (True when served from cache).
    """
    cwd_path = Path(cwd or os.getcwd()).resolve()
    cwd_str = str(cwd_path)

    # ``no_cache`` never reads or writes the cache, so it needs no fingerprint
    # (and must not fail when the tree cannot be fingerprinted).
    if fingerprint is not None:
        # Explicit expected fingerprint (a would-be-commit tree SHA): bind the
        # verdict to the tree the caller named and never recompute — or let it
        # be replaced by — the loose worktree state.
        git_state_before = None if no_cache else str(fingerprint)
        fingerprint_kind = FINGERPRINT_KIND_TREE
    else:
        git_state_before = None if no_cache else _fingerprint_for_cache(cwd_str)
        fingerprint_kind = FINGERPRINT_KIND_WORKTREE

    if not force and git_state_before is not None:
        hit = lookup(
            command,
            git_state_before,
            cwd=cwd_str,
            ttl=ttl,
            test_type=test_type,
            fingerprint_kind=fingerprint_kind,
        )
        # A cached entry recorded under a different scope is not a valid hit
        # for this request (scope is stored in metadata; the cache key is the
        # command+state, so this only matters if command sets ever converge —
        # never serve a partial run as a full-suite result and vice versa).
        if hit is not None and hit.get("scope", "full") == scope:
            return hit

    normalized = normalize_test_command(command)
    proc = runner(normalized, cwd_str, timeout)
    completed_at = time.time()

    # Record which interpreter actually ran pytest (metadata only; the cache
    # key is unchanged) so the choice is auditable after the fact
    # (SA-0MVK154IE007OS3D AC2). Non-pytest commands record an empty string.
    interpreter = resolve_pytest_executable() if is_pytest_command(normalized) else ""

    git_state = git_state_before or ""
    if not no_cache:
        if fingerprint is not None:
            # An explicit fingerprint describes the tree the caller named; it
            # cannot be recomputed from the (possibly churned) worktree after
            # the run, so it keys the entry as-is.
            store(
                normalized,
                git_state,
                cwd=cwd_str,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                exit_code=proc.returncode,
                completed_at=completed_at,
                scope=scope,
                test_type=test_type,
                interpreter=interpreter,
                git_state_at_start=git_state_before or "",
                fingerprint_kind=FINGERPRINT_KIND_TREE,
            )
        else:
            # Key the entry on the state the run left behind: that is the
            # state every later consumer computes, so a completed run is
            # always queryable. The pre-run state is kept as metadata only —
            # it never keys the entry, because transient churn present when a
            # run starts can be gone by the time it finishes
            # (WL-0MVPO9P610055UQ3).
            git_state_after = _fingerprint_for_cache(cwd_str)
            if git_state_after is not None:
                git_state = git_state_after
                store(
                    normalized,
                    git_state_after,
                    cwd=cwd_str,
                    stdout=proc.stdout or "",
                    stderr=proc.stderr or "",
                    exit_code=proc.returncode,
                    completed_at=completed_at,
                    scope=scope,
                    test_type=test_type,
                    interpreter=interpreter,
                    git_state_at_start=git_state_before or "",
                )

    return {
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "exit_code": proc.returncode,
        "completed_at": completed_at,
        "command": normalized,
        "git_state": git_state,
        "fingerprint_kind": fingerprint_kind,
        "scope": scope,
        "test_type": test_type,
        "interpreter": interpreter,
        "cached": False,
    }


def query_cached(
    command: str,
    *,
    cwd: str | Path | None = None,
    ttl: float = DEFAULT_TTL_SECONDS,
    test_type: str = "full",
) -> dict[str, Any] | None:
    """Read-only cache query: never executes anything.

    Returns the cached entry for *command* at the current git state and
    *test_type*, or None on miss. Defaults to the ``full`` profile so the
    audit skill's full-suite verification is unchanged; read-only consumers
    must pass a non-``full`` *test_type* to inspect a typed profile.

    An indeterminate git state is a miss, never a hit: the cache cannot prove
    the entry was recorded for this tree, and a degraded fingerprint would
    match every dirty tree (WL-0MVPO9P610055UQ3).
    """
    cwd_str = str(Path(cwd or os.getcwd()).resolve())
    git_state = _fingerprint_for_cache(cwd_str)
    if git_state is None:
        return None
    return lookup(command, git_state, cwd=cwd_str, ttl=ttl, test_type=test_type)


# ---------------------------------------------------------------------------
# Summary extraction
# ---------------------------------------------------------------------------

# Summary lines that agents typically grep for after a run: vitest output
# ("Test Files ...", "Tests ..."), pytest ("N passed", "N failed"), node TAP
# ("# tests", "# pass", "# fail").
_DEFAULT_SUMMARY_PATTERN = r"Test Files|Tests\s+\d+|passed|failed|skipped|xfailed|# (pass|fail|tests)"


def summary_lines(
    stdout: str, stderr: str = "", pattern: str | None = None
) -> list[str]:
    """Return output lines matching *pattern* (default summary pattern).

    Used by ``run_tests.py --summary`` to extract summary lines from a cached
    run without executing it.
    """
    import re

    rx = re.compile(pattern or _DEFAULT_SUMMARY_PATTERN)
    combined = f"{stdout}\n{stderr}"
    return [line for line in combined.splitlines() if rx.search(line)]


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "FAILED_RUN_TTL_SECONDS",
    "FINGERPRINT_KIND_TREE",
    "FINGERPRINT_KIND_WORKTREE",
    "GitStateUnavailable",
    "cache_dir",
    "cache_key",
    "compute_git_state",
    "compute_tree_state",
    "lookup",
    "normalize_test_command",
    "query_cached",
    "run_cached",
    "store",
    "summary_lines",
]
