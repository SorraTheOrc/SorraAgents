"""Disk-backed temporary-directory resolution and stale pytest-root pruning.

Why
---
``/tmp`` is frequently a small tmpfs. On a loaded multi-agent host the
**inode** budget (not the byte budget) is exhausted first by thousands of tiny
files under ``/tmp/pytest-of-<user>/pytest-*``, which surfaces as ``ENOSPC``
inside a suite subprocess (Playwright ``createContext``) and a gate failure
that is infrastructure, not code (SA-0MW2QED9M005FWOK).

This module is the single source of truth for the two framework-side
mitigations. It is standard-library only and importable by both the test
runner (``skill/test/scripts/run_tests.py``) and ``implement.py``.

``resolve_test_tmpdir()``
    Resolve a temporary directory whose contents live on disk-backed storage.
    Precedence:

    1. ``PI_TEST_TMPDIR`` — explicit operator override (created when absent).
    2. ``<PI_TEST_TMPDIR_DISK_ROOT or /var/tmp>/pi-test-<uid>`` — the
       configured disk-backed candidate (created when absent). It is skipped
       when the root is missing, not a directory, or itself mounted tmpfs.
    3. ``tempfile.gettempdir()`` — the final portable fallback for macOS,
       Windows and CI hosts without a usable disk-backed root.

``prune_stale_test_roots()``
    Remove stale ``pytest-of-<user>/pytest-*`` roots. Rules:

    * only the **current user's** ``pytest-of-<user>`` base is considered;
    * a root younger than the age threshold (default 60 minutes) is kept;
    * the ``pytest-current`` symlink target is always kept;
    * the newest ``keep_recent`` roots (default 2) are always kept;
    * a root that a live ``pytest`` process appears to hold is skipped
      (best-effort Linux ``/proc`` scan; a no-op elsewhere);
    * a missing base directory is a no-op.

Configuration (environment):

* ``PI_TEST_TMPDIR`` — explicit temporary-directory override.
* ``PI_TEST_TMPDIR_DISK_ROOT`` — root under which ``pi-test-<uid>`` is created
  (default ``/var/tmp``).
* ``PI_TEST_TMPDIR_MAX_AGE_SECONDS`` — prune age threshold (default ``3600``).
* ``PI_TEST_TMPDIR_KEEP_RECENT`` — number of newest roots always kept
  (default ``2``).

Fail-open contract
------------------
Neither helper ever raises. Every error path is caught, logged and reported in
the returned reason string, so a TMPDIR or prune problem can never change a
test run's exit status, cache entry or scope resolution. Callers log the reason
and continue.

Operator guidance (precedence, prune rules, failure signature, remedy):
``docs/dev/test-skill-reference.md`` §3 “Disk-backed test temporary storage
and the inode-exhaustion signature” (SA-0MW3ENVLU0097JRD).
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

LOG = logging.getLogger("skill.shared.test_tmpdir")

#: Explicit temporary-directory override (highest precedence).
TEST_TMPDIR_ENV = "PI_TEST_TMPDIR"
#: Root under which the per-user disk-backed directory is created.
TEST_TMPDIR_DISK_ROOT_ENV = "PI_TEST_TMPDIR_DISK_ROOT"
TEST_TMPDIR_DISK_ROOT_DEFAULT = "/var/tmp"
#: Per-user directory name created under the disk-backed root.
TEST_TMPDIR_DIRNAME_TEMPLATE = "pi-test-{uid}"

#: Stale-root age threshold (seconds) and how many newest roots to keep.
TEST_ROOT_MAX_AGE_ENV = "PI_TEST_TMPDIR_MAX_AGE_SECONDS"
TEST_ROOT_MAX_AGE_DEFAULT = 3600.0
TEST_ROOT_KEEP_ENV = "PI_TEST_TMPDIR_KEEP_RECENT"
TEST_ROOT_KEEP_DEFAULT = 2

#: pytest's own naming scheme for its per-user base and per-run roots.
PYTEST_BASE_TEMPLATE = "pytest-of-{user}"
PYTEST_ROOT_PREFIX = "pytest-"
PYTEST_CURRENT_NAME = "pytest-current"

_LINUX = sys.platform.startswith("linux")


# ======================================================================
# Small environment / identity helpers
# ======================================================================


def _current_user() -> str:
    """Return the user name pytest uses for ``pytest-of-<user>``.

    ``getpass.getuser()`` matches pytest's Linux behaviour (``pwd`` lookup with
    an environment fallback). On any failure the numeric uid is used so the
    prune still scopes itself to *this* user rather than everyone.
    """
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - identity lookup must never break a run
        getuid = getattr(os, "getuid", None)
        return str(getuid()) if callable(getuid) else "unknown"


def _current_uid() -> str:
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if callable(getuid) else _current_user()


def _env_float(name: str, default: float) -> float:
    """Read a non-negative float from the environment, falling back safely."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        LOG.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default
    if value < 0:
        LOG.warning("Ignoring negative %s=%r; using %s", name, raw, default)
        return default
    return value


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from the environment, falling back safely."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOG.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default
    if value < 0:
        LOG.warning("Ignoring negative %s=%r; using %s", name, raw, default)
        return default
    return value


# ======================================================================
# TMPDIR resolution
# ======================================================================


def _disk_root() -> Path:
    configured = os.environ.get(TEST_TMPDIR_DISK_ROOT_ENV, "").strip()
    return Path(configured or TEST_TMPDIR_DISK_ROOT_DEFAULT).expanduser()


def _is_tmpfs(path: Path) -> bool:
    """Best-effort: True when *path* sits on a tmpfs mount.

    Returns False whenever the answer is unknown (non-Linux hosts, unreadable
    ``/proc/self/mounts``), so an unknown filesystem is never rejected.
    """
    if not _LINUX:
        return False
    try:
        mounts = Path("/proc/self/mounts").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False
    try:
        resolved = os.path.realpath(path)
    except OSError:
        resolved = str(path)
    best_mount: str | None = None
    best_fstype = ""
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mountpoint = parts[1].replace("\\040", " ")
        fstype = parts[2]
        prefix = mountpoint.rstrip("/") or "/"
        if (
            resolved == mountpoint or resolved.startswith(prefix + os.sep)
        ) and (best_mount is None or len(mountpoint) > len(best_mount)):
            best_mount = mountpoint
            best_fstype = fstype
    return best_mount is not None and best_fstype == "tmpfs"


def _prepare_dir(path: Path) -> bool:
    """Create *path* when absent and report whether it is a writable dir."""
    path.mkdir(parents=True, exist_ok=True)
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _log_tmpdir_choice(path: Path, reason: str) -> None:
    LOG.info("Test TMPDIR=%s (%s)", path, reason)


def _last_resort_tmpdir() -> Path:
    """A path-shaped answer for the absolute worst case; never raises."""
    for candidate in (
        os.environ.get("TMPDIR"),
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        "/tmp",
        ".",
    ):
        if candidate:
            return Path(candidate)
    return Path(".")


def resolve_test_tmpdir() -> tuple[Path, str]:
    """Resolve a disk-backed temporary directory plus a reason string.

    Precedence: ``PI_TEST_TMPDIR`` override, then
    ``<PI_TEST_TMPDIR_DISK_ROOT>/pi-test-<uid>``, then
    ``tempfile.gettempdir()``. Missing directories are created. Never raises:
    an unexpected error is reported in the reason and a last-resort path is
    returned (SA-0MW2QED9M005FWOK).
    """
    notes: list[str] = []
    try:
        override = os.environ.get(TEST_TMPDIR_ENV, "").strip()
        if override:
            candidate = Path(override).expanduser()
            try:
                if _prepare_dir(candidate):
                    reason = f"explicit {TEST_TMPDIR_ENV} override"
                    _log_tmpdir_choice(candidate, reason)
                    return candidate, reason
                notes.append(f"{TEST_TMPDIR_ENV}={override} is not a writable dir")
            except OSError as exc:
                notes.append(f"{TEST_TMPDIR_ENV}={override} unusable: {exc}")

        disk_root = _disk_root()
        candidate = disk_root / TEST_TMPDIR_DIRNAME_TEMPLATE.format(
            uid=_current_uid()
        )
        try:
            if not disk_root.is_dir():
                notes.append(f"disk-backed root {disk_root} is absent")
            elif _is_tmpfs(disk_root):
                notes.append(f"disk-backed root {disk_root} is tmpfs, not disk")
            elif _prepare_dir(candidate):
                reason = f"disk-backed candidate {candidate}"
                _log_tmpdir_choice(candidate, reason)
                notes.append(reason)
                return candidate, "; ".join(notes)
            else:
                notes.append(f"disk-backed candidate {candidate} not writable")
        except OSError as exc:
            notes.append(f"disk-backed candidate {candidate} unusable: {exc}")

        fallback = Path(tempfile.gettempdir())
        notes.append(f"fallback tempfile.gettempdir()={fallback}")
        _log_tmpdir_choice(fallback, "fallback")
        return fallback, "; ".join(notes)
    except Exception as exc:  # noqa: BLE001 - documented fail-open contract
        LOG.warning("Test TMPDIR resolution failed (%s); using last resort", exc)
        return _last_resort_tmpdir(), f"resolution error: {exc}"


# ======================================================================
# Stale pytest-root pruning
# ======================================================================


def _pytest_base_candidates() -> list[Path]:
    """The current user's ``pytest-of-<user>`` bases that may hold roots."""
    name = PYTEST_BASE_TEMPLATE.format(user=_current_user())
    roots: list[Path] = []
    resolvers = (
        lambda: resolve_test_tmpdir()[0],
        lambda: Path(tempfile.gettempdir()),
    )
    for resolver in resolvers:
        try:
            root = resolver()
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            LOG.debug("Skipping pytest base candidate from %r: %s", resolver, exc)
            continue
        candidate = root / name
        if candidate not in roots:
            roots.append(candidate)
    return roots


def _pytest_current_target(base: Path) -> Path | None:
    """Resolve ``pytest-current`` defensively; ``None`` when absent/broken."""
    link = base / PYTEST_CURRENT_NAME
    try:
        if link.is_symlink() or link.exists():
            return Path(os.path.realpath(link))
    except OSError:
        return None
    return None


def _active_pytest_paths() -> list[str]:
    """Best-effort absolute paths referenced by live ``pytest`` processes.

    Reads ``/proc/<pid>/cmdline`` on Linux and returns only path-shaped
    arguments of processes whose command line mentions ``pytest``. Any error
    yields an empty list, so an unknown state protects nothing but never
    breaks pruning.
    """
    if not _LINUX:
        return []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    paths: list[str] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"pytest" not in raw:
            continue
        for token in raw.split(b"\0"):
            text = token.decode("utf-8", "replace")
            if text.startswith(os.sep):
                paths.append(text)
    return paths


def _root_in_use(root: Path, active_paths: list[str]) -> bool:
    """True when an active pytest process references *root* or a child of it."""
    if not active_paths:
        return False
    try:
        root_str = os.path.realpath(root)
    except OSError:
        root_str = str(root)
    prefix = root_str.rstrip(os.sep) + os.sep
    for candidate in active_paths:
        try:
            resolved = os.path.realpath(candidate)
        except OSError:
            resolved = candidate
        if resolved == root_str or resolved.startswith(prefix):
            return True
    return False


def _prune_base(
    base: Path,
    *,
    max_age_seconds: float,
    keep_recent: int,
    now: float,
    active_paths: list[str],
) -> dict[str, int]:
    """Prune one ``pytest-of-<user>`` base; returns per-outcome counts."""
    stats = {
        "removed": 0,
        "kept_recent": 0,
        "kept_young": 0,
        "skipped_in_use": 0,
        "failed": 0,
    }
    roots: list[tuple[Path, float]] = []
    for entry in base.iterdir():
        if entry.is_symlink() or not entry.name.startswith(PYTEST_ROOT_PREFIX):
            continue
        try:
            if not entry.is_dir():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        roots.append((entry, mtime))

    protected = _pytest_current_target(base)
    roots.sort(key=lambda item: item[1], reverse=True)
    newest = {root for root, _ in roots[:keep_recent]}

    for root, mtime in roots:
        if root in newest:
            stats["kept_recent"] += 1
            continue
        if protected is not None and Path(os.path.realpath(root)) == protected:
            stats["kept_recent"] += 1
            continue
        if now - mtime < max_age_seconds:
            stats["kept_young"] += 1
            continue
        if _root_in_use(root, active_paths):
            stats["skipped_in_use"] += 1
            LOG.info("Keeping pytest root %s: an active pytest process holds it", root)
            continue
        try:
            shutil.rmtree(root)
            stats["removed"] += 1
        except OSError as exc:
            stats["failed"] += 1
            LOG.warning("Could not remove stale pytest root %s: %s", root, exc)
    return stats


def prune_stale_test_roots(
    base_dir: str | Path | None = None,
    *,
    max_age_seconds: float | None = None,
    keep_recent: int | None = None,
    user: str | None = None,
    now: float | None = None,
) -> tuple[int, str]:
    """Remove stale ``pytest-of-<user>/pytest-*`` roots; never raises.

    Args:
        base_dir: A ``pytest-of-<user>`` directory to prune. When omitted, the
            current user's base under the resolved test TMPDIR *and* under
            ``tempfile.gettempdir()`` are both considered.
        max_age_seconds: Age threshold; defaults to
            ``PI_TEST_TMPDIR_MAX_AGE_SECONDS`` or 3600.
        keep_recent: Newest roots always kept; defaults to
            ``PI_TEST_TMPDIR_KEEP_RECENT`` or 2.
        user: User name to scope to; defaults to the current user.
        now: Clock override (tests); defaults to ``time.time()``.

    Returns:
        ``(removed_count, reason)``. The reason names every base that was
        skipped and the outcome counts, or the error that made the prune a
        no-op.
    """
    try:
        max_age = (
            _env_float(TEST_ROOT_MAX_AGE_ENV, TEST_ROOT_MAX_AGE_DEFAULT)
            if max_age_seconds is None
            else float(max_age_seconds)
        )
        if max_age < 0:
            raise ValueError(f"max_age_seconds must be >= 0, got {max_age}")
        keep = max(
            0,
            _env_int(TEST_ROOT_KEEP_ENV, TEST_ROOT_KEEP_DEFAULT)
            if keep_recent is None
            else int(keep_recent),
        )
        clock = time.time() if now is None else float(now)
        expected_base = PYTEST_BASE_TEMPLATE.format(user=user or _current_user())

        if base_dir is None:
            bases = _pytest_base_candidates()
        else:
            bases = [Path(base_dir)]
        if not bases:
            return 0, "no pytest-of-<user> base directory to prune"

        active_paths = _active_pytest_paths()
        removed_total = 0
        notes: list[str] = []
        for base in bases:
            if not base.is_dir():
                notes.append(f"{base}: absent")
                continue
            if base.name != expected_base:
                notes.append(f"{base}: not {expected_base}; skipped")
                continue
            stats = _prune_base(
                base,
                max_age_seconds=max_age,
                keep_recent=keep,
                now=clock,
                active_paths=active_paths,
            )
            removed_total += stats["removed"]
            notes.append(
                f"{base}: removed={stats['removed']} "
                f"kept_recent={stats['kept_recent']} "
                f"kept_young={stats['kept_young']} "
                f"skipped_in_use={stats['skipped_in_use']} "
                f"failed={stats['failed']}"
            )
            if stats["removed"]:
                LOG.info(
                    "Pruned %s stale pytest root(s) under %s (older than %ss, "
                    "keeping the newest %s)",
                    stats["removed"],
                    base,
                    int(max_age),
                    keep,
                )

        reason = "; ".join(notes)
        return removed_total, reason
    except Exception as exc:  # noqa: BLE001 - documented fail-open contract
        LOG.warning("Stale pytest-root prune failed (%s); continuing", exc)
        return 0, f"prune error: {exc}"


__all__ = [
    "PYTEST_BASE_TEMPLATE",
    "PYTEST_CURRENT_NAME",
    "PYTEST_ROOT_PREFIX",
    "TEST_ROOT_KEEP_DEFAULT",
    "TEST_ROOT_KEEP_ENV",
    "TEST_ROOT_MAX_AGE_DEFAULT",
    "TEST_ROOT_MAX_AGE_ENV",
    "TEST_TMPDIR_DIRNAME_TEMPLATE",
    "TEST_TMPDIR_DISK_ROOT_DEFAULT",
    "TEST_TMPDIR_DISK_ROOT_ENV",
    "TEST_TMPDIR_ENV",
    "prune_stale_test_roots",
    "resolve_test_tmpdir",
]
