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
  HEAD sha plus a hash of ``git status --porcelain`` output, resolved against
  the repo containing the working directory (worktree-aware).
- **2-hour TTL** (configurable): expired entries are re-run and replaced.
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
                         "exit_code":..., "completed_at": <epoch float>}
        stdout.txt      full stdout of the run
        stderr.txt      full stderr of the run

Writes are atomic (temp file + ``os.replace``); no locking is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from skill.test_runner import normalize_test_command

DEFAULT_TTL_SECONDS = 2 * 60 * 60  # 2 hours (operator decision)
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
    """Run a git command in *cwd*, returning stdout or None on failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def compute_git_state(cwd: str | Path | None = None) -> str:
    """Return a fingerprint of the git state at *cwd*.

    The fingerprint combines the HEAD commit sha with the full working-tree
    status (tracked modifications plus untracked files) so that any code or
    test change invalidates the cache. Git commands run with ``cwd`` as the
    working directory, so the fingerprint reflects the *worktree containing
    cwd* (implement loops run suites inside git worktrees).

    For non-git directories a stable per-directory fingerprint is returned so
    the cache still works (TTL-only invalidation), documented fallback.
    """
    cwd = str(Path(cwd or os.getcwd()).resolve())
    head = _run_git(cwd, "rev-parse", "HEAD")
    if head is None:
        # Not a git repository: stable per-directory key.
        digest = hashlib.sha256(f"no-git:{cwd}".encode()).hexdigest()
        return digest[:16]
    status = _run_git(cwd, "status", "--porcelain") or ""
    raw = f"{head.strip()}\n{status}"
    return hashlib.sha256(raw.encode()).hexdigest()


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


def cache_key(normalized_command: str, git_state: str) -> str:
    """Return the deterministic cache key for a normalized command + state."""
    raw = f"{normalized_command}{_KEY_SEPARATOR}{git_state}"
    return hashlib.sha256(raw.encode()).hexdigest()[: _KEY_LENGTH]


def _entry_dir(command: str, git_state: str, cwd: str | Path) -> Path:
    """Resolve the entry directory for a command at *cwd*."""
    repo_root = Path(cwd or os.getcwd()).resolve()
    return cache_dir(repo_root) / cache_key(normalize_test_command(command), git_state)


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
) -> dict[str, Any] | None:
    """Return a cached run entry or None on miss/corruption/expiry.

    A hit requires the stored git state to match *git_state* AND the run to be
    within *ttl* seconds old. Corrupt or unreadable entries are treated as a
    miss (fresh run) and never raise into the caller.

    Returns a dict with ``stdout``, ``stderr``, ``exit_code``, ``completed_at``
    (epoch float), ``command`` (normalized), ``git_state`` and ``cached: True``.
    """
    entry_dir = _entry_dir(command, git_state, cwd or os.getcwd())
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
    completed_at = float(meta.get("completed_at", 0))
    if time.time() - completed_at > ttl:
        return None  # expired TTL

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": int(meta.get("exit_code", 0)),
        "completed_at": completed_at,
        "command": meta.get("command", normalize_test_command(command)),
        "git_state": git_state,
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
) -> Path:
    """Persist a run result, replacing any existing entry for the same key.

    Writes stdout/stderr first, then metadata last (atomic each); a partial
    write is therefore treated as corrupt by :func:`lookup` and degrades to a
    fresh run.

    Returns the entry directory path.
    """
    entry_dir = _entry_dir(command, git_state, cwd or os.getcwd())
    entry_dir.mkdir(parents=True, exist_ok=True)

    normalized = normalize_test_command(command)
    meta = {
        "version": _CACHE_VERSION,
        "command": normalized,
        "git_state": git_state,
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
    """Execute a normalized command string, capturing stdout/stderr."""
    return subprocess.run(
        shlex.split(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
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
) -> dict[str, Any]:
    """Run *command* through the cache.

    - A valid cached result (matching git state, within TTL) is returned
      without executing — verified by tests asserting the underlying command
      is not spawned.
    - A miss, changed git-state, or expired TTL runs the normalized command,
      stores the result, and returns it.
    - ``force=True`` bypasses lookup but still stores (refresh).
    - ``no_cache=True`` bypasses lookup AND storage (pure bypass).

    Returns a dict with ``stdout``, ``stderr``, ``exit_code``, ``completed_at``
    (epoch float), ``command`` (normalized), ``git_state`` and ``cached``
    (True when served from cache).
    """
    cwd_path = Path(cwd or os.getcwd()).resolve()
    cwd_str = str(cwd_path)
    git_state = compute_git_state(cwd_str)

    if not force and not no_cache:
        hit = lookup(command, git_state, cwd=cwd_str, ttl=ttl)
        if hit is not None:
            return hit

    normalized = normalize_test_command(command)
    proc = runner(normalized, cwd_str, timeout)
    completed_at = time.time()

    if not no_cache:
        store(
            normalized,
            git_state,
            cwd=cwd_str,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            exit_code=proc.returncode,
            completed_at=completed_at,
        )

    return {
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "exit_code": proc.returncode,
        "completed_at": completed_at,
        "command": normalized,
        "git_state": git_state,
        "cached": False,
    }


def query_cached(
    command: str,
    *,
    cwd: str | Path | None = None,
    ttl: float = DEFAULT_TTL_SECONDS,
) -> dict[str, Any] | None:
    """Read-only cache query: never executes anything.

    Returns the cached entry for *command* at the current git state, or None
    on miss. Intended for ``--summary`` and read-only consumers (e.g. the
    audit skill) that may READ the cache but must not execute the suite.
    """
    cwd_str = str(Path(cwd or os.getcwd()).resolve())
    git_state = compute_git_state(cwd_str)
    return lookup(command, git_state, cwd=cwd_str, ttl=ttl)


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
    "cache_dir",
    "cache_key",
    "compute_git_state",
    "lookup",
    "normalize_test_command",
    "query_cached",
    "run_cached",
    "store",
    "summary_lines",
]
