#!/usr/bin/env python3
"""Orchestration script for the implement skill workflow.

Manages the deterministic lifecycle of an implementation work item: claim,
worktree creation, signal handling, build/test/commit cycle, cleanup, and
stage advancement.

Usage:
  implement.py start <work-item-id>          # Phase 1: setup
  implement.py finish <work-item-id>         # Phase 2: build, test, commit, push, cleanup
  implement.py abort <work-item-id>          # Abort and cleanup
  implement.py parent <parent-id>            # Recurse into children (epic/parent items)

Optional flags:
  --json                    JSON output for agents
  --no-refactor             Skip the refactor step
  --max-retry N             Max test-fix loop retries (default: 3)
  --commit-msg <msg>        Commit message override
  --parent-branch <branch>  Override parent branch (default: dev)
  --worktree-path <path>    Override worktree path
  -v, --verbose             Verbose logging

Environment:
  IMPLEMENT_TEST_COMMAND    Override the finish test-step command (shell string)

Exit codes:
  0 – success
  1 – error during execution (non-abort)
  2 – aborted
"""  # noqa: EXE001

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure the repository root is on sys.path so skill package imports work
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

from shared.code_freeze import is_code_freeze_active
from shared.status_lifecycle import StatusLifecycle, worklog_dir_flag
from test_cache import run_cached
from test_runner import canonicalize_quiet_test_command

# Canonical quiet full-suite commands — identical cache keys to the test
# skill's run_tests.py (SA-0MSN6FBFS006Z5QP) so cached runs are shared
# full-suite evidence rather than a fail-fast partial run.
PYTEST_CMD = canonicalize_quiet_test_command("pytest")  # pytest -q -r a --disable-warnings
NPM_TEST_CMD = canonicalize_quiet_test_command("npm test")  # npm --silent test

LOG = logging.getLogger("implement.scripts.implement")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path.cwd().resolve()
DEFAULT_PARENT_BRANCH = "dev"
DEFAULT_WORKTREE_DIR = ".worklog/worktrees"
DEFAULT_MAX_RETRY = 3
SLUG_MAX_LENGTH = 40
WORK_ITEM_ID_PATTERN = re.compile(r"^[A-Z]+-\w+$")

# State file name stored inside the worktree
STATE_FILE_NAME = ".implement_state.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ImplementState:
    """Persistent state for an in-progress implementation.

    Written to a JSON file inside the worktree so that the finish phase
    can resume deterministically.
    """

    work_item_id: str
    worktree_path: str
    repo_root: str
    parent_branch: str = DEFAULT_PARENT_BRANCH
    commit_msg: str = ""
    started_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "worktree_path": self.worktree_path,
            "repo_root": self.repo_root,
            "parent_branch": self.parent_branch,
            "commit_msg": self.commit_msg,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImplementState:
        return cls(
            work_item_id=data["work_item_id"],
            worktree_path=data["worktree_path"],
            repo_root=data["repo_root"],
            parent_branch=data.get("parent_branch", DEFAULT_PARENT_BRANCH),
            commit_msg=data.get("commit_msg", ""),
            started_at=data.get("started_at", ""),
        )


# ---------------------------------------------------------------------------
# File-scoped signal state
# ---------------------------------------------------------------------------

_worktree_path_global: str | None = None
_work_item_id_global: str | None = None
_repo_root_global: str | None = None
_cleanup_done = False


def _signal_handler(signum: int, frame: Any) -> None:
    """Global signal handler for SIGTERM/SIGINT during any phase.

    Runs deterministic cleanup: resets the work-item status (never leaves it
    in-progress), terminates child processes, removes the worktree, and exits.

    Uses ``os._exit`` (not ``sys.exit``) so the status reset is authoritative:
    a ``sys.exit``-raised ``SystemExit`` would unwind through any active
    ``StatusLifecycle`` context manager, which would restore the original
    (in-progress) status and undo the reset.
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    signame = signal.Signals(signum).name
    LOG.warning("Received signal %s, running deterministic cleanup...", signame)

    try:
        wt = _worktree_path_global
        wid = _work_item_id_global
        rr = _repo_root_global

        # Reset the work item status FIRST: this is the critical invariant.
        # A failure in later cleanup steps must not prevent the item from
        # being released back to a valid terminal state.
        if wid:
            try:
                _safety_reset_if_in_progress(wid)
            except RuntimeError:
                LOG.error("Failed to reset work item %s status to open", wid)

        if wt and Path(wt).exists():
            cleanup_worktree_processes(wt)
            _remove_worktree(wt)
        if rr:
            _restore_repo_state(rr)
    except Exception as exc:  # noqa: BLE001
        LOG.error("Cleanup handler error: %s", exc)

    LOG.info("Cleanup complete. Exiting due to %s.", signame)
    os._exit(2)


def _register_signal_handlers() -> None:
    """Register SIGTERM and SIGINT handlers."""
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


def _store_signal_globals(
    worktree_path: str, work_item_id: str, repo_root: str
) -> None:
    """Store references for the signal handler."""
    global _worktree_path_global, _work_item_id_global, _repo_root_global
    _worktree_path_global = worktree_path
    _work_item_id_global = work_item_id
    _repo_root_global = repo_root


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def run_cmd(
    cmd: list[str],
    cwd: str | None = None,
    capture: bool = True,
    check: bool = False,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a shell command and return the result.

    Args:
        cmd: Command as a list of strings.
        cwd: Working directory (default: current).
        capture: If True, capture stdout/stderr.
        check: If True, raise CalledProcessError on non-zero exit.
        timeout: Timeout in seconds.
        env: Optional environment variable overrides.

    Returns:
        ``subprocess.CompletedProcess``

    Raises:
        ``subprocess.TimeoutExpired`` on timeout.
        ``subprocess.CalledProcessError`` if *check* is True and exit != 0.
    """
    LOG.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd or os.getcwd())
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=check,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )


def format_json_output(data: dict[str, Any]) -> str:
    """Format dict as pretty-printed JSON."""
    return json.dumps(data, indent=2, default=str)


def slug_from_title(title: str) -> str:
    """Create a short slug from a work-item title.

    Args:
        title: The work-item title string.

    Returns:
        A kebab-case slug truncated to ``SLUG_MAX_LENGTH`` characters.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return slug[:SLUG_MAX_LENGTH].rstrip("-")


def worktree_path_for(work_item_id: str, slug: str) -> str:
    """Compute the canonical worktree path.

    Args:
        work_item_id: The work item ID.
        slug: Short slug for the branch name.

    Returns:
        Relative path string like ``.worklog/worktrees/wl-<id>-<slug>``.
    """
    return f"{DEFAULT_WORKTREE_DIR}/wl-{work_item_id}-{slug}"


def branch_name_for(work_item_id: str, slug: str) -> str:
    """Compute the canonical feature branch name.

    Args:
        work_item_id: The work item ID.
        slug: Short slug for the branch name.

    Returns:
        Branch name string like ``wl-<id>-<slug>``.
    """
    return f"wl-{work_item_id}-{slug}"


# ---------------------------------------------------------------------------
# Worklog interaction helpers
# ---------------------------------------------------------------------------


def wl_show(work_item_id: str) -> dict[str, Any]:
    """Fetch a work item by ID as JSON.

    Args:
        work_item_id: The work item ID.

    Returns:
        Parsed JSON dict from ``wl show``.
    """
    cmd = ["wl", "show", work_item_id, "--json"]
    cmd[1:1] = worklog_dir_flag()
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        LOG.error("Failed to fetch work item %s: %s", work_item_id, result.stderr.strip())
        return {}
    try:
        data = json.loads(result.stdout.strip())
        if isinstance(data, dict) and "workItem" in data:
            return data["workItem"]
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        LOG.error("Invalid JSON from wl show: %s", exc)
        return {}


def wl_show_children(work_item_id: str) -> list[dict[str, Any]]:
    """Fetch a work item's children as a list of dicts.

    Uses ``wl show <id> --children --json`` and returns the ``children``
    array (empty when the item has no children or the fetch fails).

    Args:
        work_item_id: The parent work item ID.

    Returns:
        List of child work-item dicts (may be empty).
    """
    cmd = ["wl", "show", work_item_id, "--children", "--json"]
    cmd[1:1] = worklog_dir_flag()
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        LOG.error(
            "Failed to fetch children of %s: %s",
            work_item_id,
            result.stderr.strip(),
        )
        return []
    try:
        data = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        LOG.error("Invalid JSON from wl show --children: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    return children if isinstance(children, list) else []


def wl_dep_blockers(work_item_id: str) -> list[dict[str, Any]]:
    """Fetch a work item's outbound (depends-on) dependency edges.

    Uses ``wl dep list <id> --json`` and returns the ``outbound`` array —
    the items this work item is blocked by / depends on. Empty when the
    item has no dependencies or the fetch fails.

    Args:
        work_item_id: The work item ID.

    Returns:
        List of outbound dependency-edge dicts (may be empty).
    """
    cmd = ["wl", "dep", "list", work_item_id, "--json"]
    cmd[1:1] = worklog_dir_flag()
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        LOG.error(
            "Failed to fetch dependencies of %s: %s",
            work_item_id,
            result.stderr.strip(),
        )
        return []
    try:
        data = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        LOG.error("Invalid JSON from wl dep list: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    outbound = data.get("outbound")
    return outbound if isinstance(outbound, list) else []


def wl_add_comment(work_item_id: str, comment: str) -> bool:
    """Add a comment to a work item.

    Args:
        work_item_id: The work item ID.
        comment: The comment text.

    Returns:
        True if the comment was added.
    """
    cmd = [
        "wl", "comment", "add", work_item_id,
        "--comment", comment, "--author", "implement",
    ]
    cmd[1:1] = worklog_dir_flag()
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        LOG.warning("Failed to add comment to %s: %s", work_item_id, result.stderr.strip())
        return False
    return True


def _safety_reset_if_in_progress(work_item_id: str) -> None:
    """Reset a work item to ``open`` ONLY if it is currently in-progress.

    Guards against clobbering items that already reached a valid terminal
    state (``open``/``blocked``/``completed``) when a late, unrelated error
    or signal occurs (e.g. after a successful finish phase).

    Args:
        work_item_id: The work item ID.
    """
    try:
        data = StatusLifecycle.show(work_item_id)
    except RuntimeError:
        LOG.warning("Could not fetch %s; skipping safety reset", work_item_id)
        return
    wi = data.get("workItem", {})
    current = wi.get("status", "")
    if current in ("in-progress", "in_progress"):
        StatusLifecycle.update_status(work_item_id, "open")
        LOG.info("Safety reset: %s status in-progress -> open", work_item_id)
    else:
        LOG.info("Safety reset skipped: %s status is %r", work_item_id, current)


# ---------------------------------------------------------------------------
# Parent-recursion helpers
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = {"in_review", "completed", "done", "deleted"}


def _is_terminal_status(status: str) -> bool:
    """Whether a work-item status is terminal for parent advancement.

    A terminal child is never re-implemented and counts toward parent
    advancement (AC-4). ``in_review``/``completed``/``done`` are terminal;
    ``open``/``blocked``/``in_progress`` are not.

    Soft-deleted items (status ``deleted``) are also treated as terminal:
    they are spurious/removed and can never be implemented, so they must
    never be claimed by ``phase_parent`` nor block parent advancement
    (SA-0MSRVN28E009OL5S).

    Args:
        status: The work-item status string.

    Returns:
        True if the status is terminal.
    """
    return status in TERMINAL_STATUSES


def _classify_child(child: dict[str, Any]) -> str:
    """Classify a child for the parent-recursion plan.

    Returns one of:
      - ``"skip-terminal"`` — already in a terminal state; never re-implemented.
      - ``"skip-in-progress"`` — claimed by another agent; reported, not clobbered.
      - ``"implement"`` — the standard workflow should run on this child.

    Args:
        child: A child work-item dict from ``wl show --children``.

    Returns:
        The classification string.
    """
    status = str(child.get("status", ""))
    if _is_terminal_status(status):
        return "skip-terminal"
    if status in ("in-progress", "in_progress"):
        return "skip-in-progress"
    return "implement"


def _resolve_implementation_order(
    children: list[dict[str, Any]],
    blockers: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Return children in dependency order (blockers first).

    Builds a graph over the parent's children using outbound (depends-on)
    edges: for each child, the blockers that are THEMSELVES children of the
    parent form in-chain edges (blocker must be implemented before child).
    Blockers outside the children set (e.g. cross-project) do not create
    edges — they are reported as coordination notes by the caller.

    Ordering is a deterministic topological sort (Kahn's algorithm) with
    stable tie-breaks (``sortIndex``, then id). A dependency cycle fails
    fast: the function returns an empty list plus a clear error message
    naming the remaining (cyclic) children — no infinite recursion.

    Args:
        children: List of child work-item dicts from ``wl show --children``.
        blockers: Mapping child id → outbound dependency-edge dicts from
            ``wl dep list`` (may be empty).

    Returns:
        ``(ordered_children, None)`` on success, or
        ``([], error_message)`` when a cycle is detected.
    """
    child_by_id: dict[str, dict[str, Any]] = {
        str(c.get("id")): c for c in children if c.get("id")
    }
    child_ids = set(child_by_id)

    # in_degree[c] = number of in-chain blockers (blockers that are also
    # children of this parent). dependents[b] = children blocked by b.
    in_degree: dict[str, int] = {cid: 0 for cid in child_ids}
    dependents: dict[str, list[str]] = {cid: [] for cid in child_ids}
    for child in children:
        cid = str(child.get("id", ""))
        if cid not in child_ids:
            continue
        for edge in blockers.get(cid, []):
            bid = str(edge.get("id", ""))
            if bid in child_ids:
                in_degree[cid] += 1
                dependents[bid].append(cid)

    def _key(cid: str) -> tuple[int, str]:
        return (int(child_by_id[cid].get("sortIndex") or 0), cid)

    ready = sorted((cid for cid in child_ids if in_degree[cid] == 0), key=_key)
    ordered: list[str] = []
    while ready:
        cid = ready.pop(0)
        ordered.append(cid)
        for dep in dependents[cid]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                ready.append(dep)
        ready.sort(key=_key)

    if len(ordered) != len(child_ids):
        remaining = sorted(child_ids - set(ordered), key=_key)
        cycle_path = " -> ".join(remaining)
        return [], (
            f"Dependency cycle detected among children: {cycle_path}. "
            f"Resolve the blocking relationships and re-run."
        )
    return [child_by_id[cid] for cid in ordered], None


def _next_child_to_implement(
    children: list[dict[str, Any]],
    blockers_map: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Return the next child to run through the standard implement workflow.

    Children already terminal or in progress elsewhere are skipped
    (AC-4/AC-7). The *children* argument must already be in dependency
    order (see :func:`_resolve_implementation_order`). A child is startable
    only when every in-chain blocker (a blocker that is also one of these
    children) is already terminal — a blocked child is never implemented
    before its blockers (AC-2). Independent siblings of an in-progress
    child may still start.

    Args:
        children: List of child work-item dicts in dependency order.
        blockers_map: Mapping child id → outbound dependency-edge dicts from
            ``wl dep list`` (defaults to {}).

    Returns:
        The next child dict, or None when no child is startable (every
        remaining child is terminal, in progress elsewhere, or blocked by a
        non-terminal sibling).
    """
    blockers_map = blockers_map or {}
    child_ids = {str(c.get("id")) for c in children}
    terminal_ids = {
        str(c.get("id"))
        for c in children
        if _is_terminal_status(str(c.get("status", "")))
    }
    for child in children:
        action = _classify_child(child)
        if action == "skip-terminal":
            continue
        if action == "skip-in-progress":
            continue  # never clobber another agent's claim
        cid = str(child.get("id", ""))
        in_chain_blockers = [
            str(b.get("id"))
            for b in blockers_map.get(cid, [])
            if str(b.get("id")) in child_ids
        ]
        if all(bid in terminal_ids for bid in in_chain_blockers):
            return child
        # else: blocked by a non-terminal sibling — dependents come later
        # in the order and are transitively blocked; keep scanning for an
        # independent sibling that is startable.
    return None


# ---------------------------------------------------------------------------
# Git / Worktree helpers
# ---------------------------------------------------------------------------


def git_rev_parse_is_inside_work_tree() -> bool:
    """Check if current directory is inside a git worktree."""
    result = run_cmd(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_status(cwd: str | None = None) -> str:
    """Get the current git status (porcelain v1).

    Args:
        cwd: Working directory to check (defaults to current directory).

    Returns:
        Porcelain status output, or empty string on error.
    """
    result = run_cmd(
        ["git", "status", "--porcelain=v1", "-b"],
        cwd=cwd,
        capture=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def git_has_dirty_files(status_output: str | None = None) -> bool:
    """Check if there are uncommitted changes outside .worklog/.

    Args:
        status_output: Optional pre-fetched git status output.

    Returns:
        True if any non-.worklog files are dirty.
    """
    if status_output is None:
        status_output = git_status()
    for line in status_output.splitlines():
        # Skip branch info line
        if line.startswith("##"):
            continue
        # Skip .worklog/ changes
        file_path = line[3:].strip() if len(line) > 3 else ""
        if file_path.startswith(".worklog/"):
            continue
        if line.strip():
            return True
    return False


def git_worktree_add(
    branch: str, worktree_path: str, parent_branch: str = DEFAULT_PARENT_BRANCH
) -> bool:
    """Create a new git worktree from a parent branch.

    Args:
        branch: Name for the new branch.
        worktree_path: Path for the worktree directory.
        parent_branch: Source branch to fork from.

    Returns:
        True if the worktree was created.
    """
    result = run_cmd(
        ["git", "worktree", "add", "--track", "-b", branch, worktree_path, parent_branch],
        check=False,
        timeout=120,
    )
    return result.returncode == 0


def git_commit(cwd: str, message: str) -> bool:
    """Stage all changes and commit in the given directory.

    Args:
        cwd: Working directory (worktree root).
        message: Commit message.

    Returns:
        True if the commit succeeded.
    """
    # Stage all changes (surgical: only what was modified)
    add_result = run_cmd(["git", "add", "-A"], cwd=cwd, check=False)
    if add_result.returncode != 0:
        LOG.error("git add failed: %s", add_result.stderr.strip())
        return False

    # Check if there's anything to commit
    diff_result = run_cmd(
        ["git", "diff", "--cached", "--quiet"], cwd=cwd, check=False
    )
    if diff_result.returncode == 0:
        LOG.info("No changes to commit (clean working tree)")
        return True

    commit_result = run_cmd(
        ["git", "commit", "-m", message],
        cwd=cwd,
        check=False,
    )
    if commit_result.returncode != 0:
        LOG.error("git commit failed: %s", commit_result.stderr.strip())
        return False
    return True


def git_push_to_dev(cwd: str, branch: str) -> bool:
    """Push the current branch into the dev branch on origin.

    Args:
        cwd: Working directory (worktree root).
        branch: Local branch name to push.

    Returns:
        True if the push succeeded.
    """
    # Push to dev (refs/heads/dev target)
    result = run_cmd(
        ["git", "push", "origin", f"{branch}:refs/heads/dev"],
        cwd=cwd,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        LOG.error("git push to dev failed: %s", result.stderr.strip())
        return False
    return True


def git_get_commit_hash(cwd: str) -> str:
    """Get the current HEAD commit hash.

    Args:
        cwd: Working directory.

    Returns:
        Short commit hash string.
    """
    result = run_cmd(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=cwd,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return "unknown"


def _get_repo_root(cwd: str | None = None) -> str | None:
    """Get the absolute path to the MAIN git repository root.

    ``git rev-parse --show-toplevel`` resolves to the root of the *current*
    working tree. Inside a linked worktree (``.git`` is a file, not a
    directory) that is the worktree root, not the main checkout. Callers
    (the ``_remove_worktree`` safety gate, node_modules symlinking, repo
    restore/push) need the main checkout root, so resolve it via
    ``git rev-parse --git-common-dir`` (whose parent is the main working
    tree root) when *cwd* is inside a linked worktree.

    Args:
        cwd: Optional working directory (defaults to current directory).

    Returns:
        Absolute main repo root path, or None if not in a git repo.
    """
    result = run_cmd(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    toplevel = Path(result.stdout.strip()).resolve()

    # The main working tree has .git as a DIRECTORY; a linked worktree has
    # it as a FILE pointing at the real git directory.
    if (toplevel / ".git").is_dir():
        return toplevel.as_posix()

    # cwd is inside a linked worktree: derive the main repo root from the
    # common git dir (its parent is the main working tree root).
    common = run_cmd(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=cwd,
        check=False,
        timeout=30,
    )
    if common.returncode == 0 and common.stdout.strip():
        common_dir = Path(common.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = toplevel / common_dir
        main_root = common_dir.resolve().parent
        git_marker = main_root / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return main_root.as_posix()
    return toplevel.as_posix()


def _ensure_node_modules_symlink(worktree_path: str, repo_root: str | None) -> bool:
    """Auto-symlink the main checkout's node_modules into a worktree.

    Git worktrees do not include gitignored directories, so tests that spawn
    a real subprocess via an absolute worktree-relative path (e.g. CLI e2e
    tests invoking ``<worktree>/node_modules/.bin/tsx``) fail with
    MODULE_NOT_FOUND. Symlinking the main checkout's node_modules resolves
    them without manual setup.

    The symlink is created only when BOTH conditions hold:
    - the worktree does not already have a node_modules entry (dir or
      symlink), AND
    - the main checkout has a node_modules directory.

    Failures are logged, never fatal: the worktree remains usable and
    dependencies can be installed inside it normally.

    Args:
        worktree_path: Absolute path to the worktree directory.
        repo_root: Absolute path to the main checkout, or None if unknown.

    Returns:
        True if the symlink was created, False otherwise (skipped/failed).
    """
    wt_nm = Path(worktree_path) / "node_modules"
    if os.path.lexists(wt_nm):
        LOG.info(
            "Worktree already has node_modules (%s); skipping symlink.", wt_nm
        )
        return False
    if not repo_root:
        LOG.info("No repo root known; skipping node_modules symlink.")
        return False
    root_nm = Path(repo_root) / "node_modules"
    if not root_nm.is_dir():
        LOG.info(
            "Main checkout has no node_modules (%s); skipping symlink.", root_nm
        )
        return False
    try:
        os.symlink(str(root_nm), str(wt_nm), target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        LOG.warning(
            "Failed to symlink node_modules into worktree %s: %s",
            worktree_path,
            exc,
        )
        return False
    LOG.info("Symlinked %s -> %s", wt_nm, root_nm)
    return True


def _ensure_submodules(worktree_path: str, repo_root: str | None = None) -> bool:
    """Initialize git submodules inside a newly-created worktree.

    ``git worktree add`` does not automatically initialize submodules.  This
    helper runs ``git submodule update --init --recursive`` inside the
    worktree so that downstream builds and tests see the expected submodule
    content.

    Best-effort only — submodule initialisation failure is **never fatal**.
    On failure a warning is logged and the worktree is left usable.

    When the main checkout has no ``.gitmodules`` the command is a no-op and
    returns ``False``.

    Args:
        worktree_path: Absolute path to the worktree directory.
        repo_root: Absolute path to the main checkout (unused but kept for
            API symmetry with ``_ensure_node_modules_symlink``).

    Returns:
        ``True`` if submodules were initialised (or there were none),
        ``False`` if the command failed or there was nothing to do.
    """
    # Quick check: does the repo even have submodules?
    gitmodules = Path(repo_root) / ".gitmodules" if repo_root else None
    if gitmodules and not gitmodules.is_file():
        LOG.info("No .gitmodules found at %s; skipping submodule init.", gitmodules)
        return False

    try:
        result = subprocess.run(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min — generous for large submodule trees.
            check=False,
        )
        if result.returncode != 0:
            LOG.warning(
                "Submodule initialisation failed in worktree %s (rc=%d): %s",
                worktree_path,
                result.returncode,
                result.stderr.strip() or result.stdout.strip(),
            )
            return False
        LOG.info(
            "Submodules initialised in worktree %s.", worktree_path
        )
        return True
    except (subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError) as exc:
        LOG.warning(
            "Submodule initialisation errored in worktree %s: %s",
            worktree_path,
            exc,
        )
        return False


def _remove_worktree(worktree_path: str, repo_root: str | None = None) -> bool:
    """Remove a worktree directory gracefully.

    Safety gates:
    1. Validates the target path is not the repository root. The root is
       taken from the *repo_root* argument when provided (``phase_finish``
       passes the authoritative value from the implement state file);
       otherwise it is derived from the current directory, resolving to the
       MAIN checkout root even when the current directory is inside a linked
       worktree (see ``_get_repo_root``).
    2. Before manual (shutil) removal, checks that the target does
       NOT contain a ``.git/`` directory (which would mean it's the
       main working tree).

    Args:
        worktree_path: Path to the worktree.
        repo_root: Optional explicit main repo root (defaults to discovery
            from the current directory).

    Returns:
        True if removal succeeded.

    Raises:
        RuntimeError: If the target path matches the repo root or
            contains a ``.git`` directory (main working tree).
    """
    resolved_path = str(Path(worktree_path).resolve())

    # ── Safety gate 1: refuse if target is the repo root ──────────
    repo_root = repo_root or _get_repo_root()
    if repo_root and Path(resolved_path) == Path(repo_root).resolve():
        msg = (
            f"REFUSE to remove the repository root: {resolved_path}. "
            f"This is the main working tree, not a worktree. "
            f"Check for stale .implement_state.json in the repo root."
        )
        LOG.critical(msg)
        raise RuntimeError(msg)

    # If the current directory is inside the worktree being removed, step
    # out to the repo root first. A deleted cwd breaks os.getcwd() and
    # subprocess spawning for every later step (StatusLifecycle.__exit__,
    # phase_finish's completion comment, the signal handler).
    try:
        current_cwd = Path.cwd().resolve()
    except OSError:
        current_cwd = None
    target = Path(resolved_path)
    if (
        current_cwd is not None
        and (current_cwd == target or target in current_cwd.parents)
    ):
        os.chdir(repo_root or str(Path.home()))

    LOG.info("Removing worktree: %s", resolved_path)
    result = run_cmd(
        ["git", "worktree", "remove", "--force", resolved_path],
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        LOG.warning(
            "git worktree remove failed, trying manual removal: %s",
            result.stderr.strip(),
        )

        # ── Safety gate 2: refuse if target has a .git/ directory ──
        target = Path(resolved_path)
        git_dir = target / ".git"
        if git_dir.is_dir():
            msg = (
                f"REFUSE to manually remove {resolved_path}: it contains "
                f"a .git/ directory, indicating the main working tree. "
                f"This would destroy the entire repository. Aborting."
            )
            LOG.critical(msg)
            raise RuntimeError(msg)

        # Fall back to manual removal (safe now - .git/ check passed)
        try:
            import shutil
            shutil.rmtree(resolved_path, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Manual worktree removal failed: %s", exc)
            return False

    # Prune stale worktree references. Run from an explicit, still-valid
    # directory: the current directory may have been the worktree that was
    # just removed (finish/abort can run from inside it), and a deleted cwd
    # breaks both the log line and the subprocess spawn.
    run_cmd(["git", "worktree", "prune"], cwd=repo_root, check=False)
    return True


def _restore_repo_state(repo_root: str) -> None:
    """Restore the repo to the dev branch.

    Args:
        repo_root: Path to the repo root.
    """
    run_cmd(["git", "checkout", DEFAULT_PARENT_BRANCH], cwd=repo_root, check=False)
    run_cmd(["git", "pull", "origin", DEFAULT_PARENT_BRANCH], cwd=repo_root, check=False)


# ---------------------------------------------------------------------------
# Process cleanup helpers
# ---------------------------------------------------------------------------


def cleanup_worktree_processes(worktree_path: str) -> dict[str, Any]:
    """Terminate processes associated with the given worktree path.

    Attempts ``wl cleanup-worktree <path>`` first. Falls back to finding
    and killing processes with the worktree path in their cwd.

    Args:
        worktree_path: Absolute path to the worktree directory.

    Returns:
        A dict with ``method`` (str), ``terminated`` (int), ``warning`` (str).
    """
    result: dict[str, Any] = {
        "method": "none",
        "terminated": 0,
        "warning": "",
    }

    abs_path = str(Path(worktree_path).resolve())

    # Try wl cleanup-worktree first
    wl_result = run_cmd(
        ["wl", "cleanup-worktree", abs_path],
        check=False,
        timeout=30,
    )
    if wl_result.returncode == 0:
        result["method"] = "wl_cleanup_worktree"
        try:
            data = json.loads(wl_result.stdout.strip())
            result["terminated"] = data.get("terminated", 0)
            result["warning"] = data.get("warning", "")
        except (json.JSONDecodeError, ValueError):
            result["terminated"] = 1  # Assume success if exit code is 0
        return result

    # If wl cleanup-worktree is unavailable (exit code != 0), try pgrep fallback
    LOG.warning(
        "wl cleanup-worktree unavailable (exit %d: %s); "
        "falling back to process scan",
        wl_result.returncode,
        wl_result.stderr.strip() or "command not found",
    )
    result["method"] = "pgrep_fallback"

    try:
        # Find processes with cwd matching the worktree path
        pgrep_result = run_cmd(
            ["pgrep", "-f", abs_path],
            check=False,
            timeout=15,
            capture=True,
        )
        if pgrep_result.returncode == 0 and pgrep_result.stdout.strip():
            pids = [
                int(pid) for pid in pgrep_result.stdout.strip().split()
                if pid.isdigit()
            ]
            # Exclude the current process
            current_pid = os.getpid()
            for pid in pids:
                if pid == current_pid:
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                    result["terminated"] += 1
                except (OSError, PermissionError):
                    pass

            # Give processes time to terminate
            if result["terminated"] > 0:
                time.sleep(2)

            # Force kill any remaining
            for pid in pids:
                if pid == current_pid:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except (OSError, PermissionError):
                    pass

            result["warning"] = (
                "wl cleanup-worktree unavailable; used pgrep fallback. "
                "Result may be incomplete."
            )
        else:
            result["warning"] = "wl cleanup-worktree unavailable; no processes found via pgrep"
    except (FileNotFoundError, OSError) as exc:
        result["warning"] = (
            f"wl cleanup-worktree unavailable and pgrep fallback failed: {exc}"
        )

    return result


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------


def _package_script(cwd: str, name: str) -> Any:
    """Return the raw ``scripts.<name>`` value from the root package.json.

    Reads the root ``package.json`` directly so the check works without npm.
    Malformed JSON, a non-object ``scripts`` value, or a missing file counts
    as "no script" (fail-open — never block finish on a broken manifest).

    Args:
        cwd: Working directory (worktree root).
        name: Script name, e.g. ``build`` or ``test``.

    Returns:
        The raw ``scripts.<name>`` value, or None when the entry (or a
        readable package.json) does not exist.
    """
    package_json = Path(cwd) / "package.json"
    if not package_json.exists():
        return None
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return None
    return scripts.get(name)


def _has_build_script(cwd: str) -> bool:
    """Check whether the repo root package.json defines a ``build`` script.

    A repo without a ``scripts.build`` entry (e.g. Python-only projects) has
    nothing to build: ``npm run build`` would exit 1 with
    ``Missing script: "build"`` and block the finish phase for every
    implementation. Reads the root ``package.json`` directly so the check
    works without npm.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True if the root package.json exists and contains a non-empty
        ``scripts.build`` entry. Malformed JSON or a non-object ``scripts``
        value counts as "no build script" (fail-open — never block finish on
        a broken manifest).
    """
    return bool(_package_script(cwd, "build"))


def _has_test_script(cwd: str) -> bool:
    """Check whether the repo root package.json defines a ``test`` script.

    Mirrors :func:`_has_build_script` for the test step: a repo without a
    ``scripts.test`` entry has no npm test suite — ``npm test`` would exit 1
    with ``Missing script: "test"``.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True if the root package.json exists and contains a non-empty
        ``scripts.test`` entry.
    """
    return bool(_package_script(cwd, "test"))


def run_build(cwd: str) -> dict[str, Any]:
    """Run the project build script.

    Repos whose root package.json has no ``build`` script (e.g. Python-only
    projects) skip the build step: it is reported as a no-op with
    ``success: True`` so the finish phase proceeds to tests → commit → push
    instead of aborting. Repos WITH a build script run ``npm run build``
    unchanged — a real build failure still blocks finish.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        A dict with ``success`` (bool), ``stdout`` (str), ``stderr`` (str),
        ``exit_code`` (int), and ``skipped`` (bool) — ``skipped`` is True
        when the build step was bypassed because no build script exists.
    """
    if not _has_build_script(cwd):
        msg = "No build script in package.json — skipping build step (no-op)."
        LOG.info(msg)
        return {
            "success": True,
            "stdout": msg,
            "stderr": "",
            "exit_code": 0,
            "skipped": True,
        }
    result = run_cmd(
        ["npm", "run", "build"],
        cwd=cwd,
        check=False,
        timeout=300,
        capture=True,
    )
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "exit_code": result.returncode,
        "skipped": False,
    }


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_PYTEST_CONFIG_MARKERS = (
    ("pyproject.toml", "[tool.pytest.ini_options]"),
    ("setup.cfg", "[tool:pytest]"),
    ("tox.ini", "[pytest]"),
)


def _pytest_importable(cwd: str) -> bool:
    """Whether ``python3 -m pytest`` would work in *cwd*.

    Probes with the same interpreter that runs the suite (``python3``) so
    venv/global-path differences are respected — ``shutil.which`` alone is
    not enough because pytest is commonly only available as a module inside
    a project venv.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when ``python3 -c "import pytest"`` exits 0.
    """
    try:
        probe = run_cmd(
            ["python3", "-c", "import pytest"],
            cwd=cwd,
            check=False,
            timeout=60,
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _has_pytest_markers(cwd: str) -> bool:
    """Whether the repo declares pytest configuration.

    A ``pytest.ini`` file alone counts; for the other config files the
    relevant section must be present (``[tool.pytest.ini_options]`` in
    pyproject.toml, ``[tool:pytest]`` in setup.cfg, ``[pytest]`` in tox.ini)
    so an unrelated config file does not trigger a pytest run.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when the repo declares pytest configuration.
    """
    root = Path(cwd)
    if (root / "pytest.ini").is_file():
        return True
    for name, marker in _PYTEST_CONFIG_MARKERS:
        path = root / name
        if not path.is_file():
            continue
        try:
            if marker in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def _has_pytest_test_files(cwd: str) -> bool:
    """Whether the repo contains pytest-style test files.

    Checks a bounded set of conventional locations (``tests/`` and ``test/``
    dirs, plus root/``src`` ``test_*.py`` / ``*_test.py`` globs) so the scan
    never walks into node_modules or other vendored trees.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when the repo contains Python test files.
    """
    root = Path(cwd)
    for dirname in ("tests", "test"):
        test_dir = root / dirname
        if test_dir.is_dir() and any(test_dir.rglob("*.py")):
            return True
    for base in (root, root / "src"):
        if base.is_dir() and (
            any(base.glob("test_*.py")) or any(base.glob("*_test.py"))
        ):
            return True
    return False


def _has_pytest_suite(cwd: str) -> bool:
    """Whether the repo has a runnable pytest suite.

    Both conditions must hold: pytest must be importable via ``python3``
    (otherwise running it would fail with ModuleNotFoundError) AND the repo
    must declare pytest (config marker or test files). The second condition
    prevents false positives on hosts with a global pytest that would
    otherwise run on empty bash-only repos and block finish with pytest's
    "no tests ran" exit code.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when the repo has a runnable pytest suite.
    """
    return _pytest_importable(cwd) and (
        _has_pytest_markers(cwd) or _has_pytest_test_files(cwd)
    )


def _is_unity_project(cwd: str) -> bool:
    """Whether *cwd* looks like a Unity project root.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when an ``Assets/`` directory or
        ``ProjectSettings/ProjectVersion.txt`` exists.
    """
    root = Path(cwd)
    return (root / "Assets").is_dir() or (root / "ProjectSettings" / "ProjectVersion.txt").is_file()


def _find_repo_test_runner(cwd: str) -> str | None:
    """Locate a repo-local test runner script at the repo root.

    Unity repos (and other non-npm/non-pytest repos) commonly ship their own
    runner, e.g. ``run_tests.sh`` or ``run_unity_tests.bat``; when present it
    is executed instead of skipping the test step.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        The script file name, or None when no runner is present.
    """
    root = Path(cwd)
    for name in ("run_tests.sh", "run_unity_tests.sh", "run_unity_tests.bat"):
        if (root / name).is_file():
            return name
    return None


def _detect_test_tooling(cwd: str) -> str | None:
    """Detect the repo's test tooling, in precedence order.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        ``pytest`` | ``npm`` | ``repo-script`` | ``unity``, or None when the
        repo has no test tooling at all.
    """
    if _has_pytest_suite(cwd):
        return "pytest"
    if _has_test_script(cwd):
        return "npm"
    if _find_repo_test_runner(cwd) is not None:
        return "repo-script"
    if _is_unity_project(cwd):
        return "unity"
    return None


def _skip_test_result(message: str, tooling: str | None) -> dict[str, Any]:
    """Build a skipped/no-op test result.

    Args:
        message: Informative no-op message (also logged).
        tooling: The detected tooling, if any (e.g. ``unity``).

    Returns:
        A result dict with ``success: True`` so finish proceeds.
    """
    LOG.info(message)
    return {
        "success": True,
        "stdout": message,
        "stderr": "",
        "exit_code": 0,
        "failures": [],
        "skipped": True,
        "tooling": tooling,
    }


def _finalize_test_result(result: dict[str, Any], tooling: str | None) -> dict[str, Any]:
    """Add metadata keys and parse failures from a raw run result.

    Args:
        result: Raw run dict (stdout/stderr/exit_code).
        tooling: The runner that produced the result.

    Returns:
        The result dict with ``success``, ``failures``, ``skipped: False``
        and ``tooling`` keys.
    """
    failures: list[str] = []
    combined = f"{result['stdout']}\n{result['stderr']}"
    for line in combined.splitlines():
        if "FAILED" in line or "failed" in line.lower():
            failures.append(line.strip())
    return {
        "success": result["exit_code"] == 0,
        "stdout": result["stdout"].strip(),
        "stderr": result["stderr"].strip(),
        "exit_code": result["exit_code"],
        "failures": failures,
        "skipped": False,
        "tooling": tooling,
    }


def _shell_command_runner(
    command: str, cwd: str, timeout: int
) -> subprocess.CompletedProcess:
    """Run a shell command string through bash (per-repo overrides/runners)."""
    return run_cmd(
        ["bash", "-c", command],
        cwd=cwd,
        check=False,
        timeout=timeout,
        capture=True,
    )


def run_tests(cwd: str) -> dict[str, Any]:
    """Run the full test suite, routed through the per-repo run cache.

    The test step is tolerant of repos with no test tooling: when neither
    pytest, an npm ``test`` script, nor a repo-local runner is detected, the
    step is skipped and reported as a no-op (``success: True``,
    ``skipped: True``) so finish proceeds to commit → push instead of
    aborting on ENOENT / ``Missing script: "test"``.

    Detection order:

    1. ``IMPLEMENT_TEST_COMMAND`` env var — per-repo override; run as-is.
    2. pytest — when the repo declares pytest (config markers or test files)
       AND the module imports via ``python3``.
    3. npm — when the root package.json defines a ``scripts.test`` entry.
    4. repo-local runner — ``run_tests.sh`` / ``run_unity_tests.sh`` /
       ``run_unity_tests.bat`` at the repo root.
    5. Unity project (``Assets/`` or ``ProjectSettings/ProjectVersion.txt``)
       without a configured runner → skipped with a Unity-specific message.
    6. Otherwise → skipped with a generic no-tooling message.

    Repos WITH tooling behave as before: the detected command runs (through
    ``skill.test_cache.run_cached`` — a valid cached result for the same
    worktree git state within the TTL is reused, see SA-0MSGN5OJ4002OZKY)
    and a non-zero exit still blocks finish. When pytest is the detected
    tooling, npm test remains the fallback if the repo also defines a
    ``scripts.test`` entry.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        A dict with ``success`` (bool), ``stdout`` (str), ``stderr`` (str),
        ``exit_code`` (int), ``failures`` (list[str]), plus ``skipped``
        (bool — True when no tooling was found) and ``tooling`` (str | None —
        the detected runner: pytest/npm/repo-script/override/unity).
    """
    # 1. Per-repo override (env var) — highest precedence
    override = os.environ.get("IMPLEMENT_TEST_COMMAND", "").strip()
    if override:
        return _finalize_test_result(
            run_cached(
                override,
                cwd=cwd,
                timeout=600,
                runner=_shell_command_runner,
            ),
            tooling="override",
        )

    tooling = _detect_test_tooling(cwd)

    # 2. pytest (with npm test fallback when the repo also has a test script)
    if tooling == "pytest":
        pytest_run = run_cached(
            PYTEST_CMD,
            cwd=cwd,
            timeout=600,
            runner=lambda command, cwd_, timeout_: run_cmd(
                command.split(), cwd=cwd_, check=False, timeout=timeout_, capture=True
            ),
        )
        result = pytest_run
        final_tooling = "pytest"
        if result["exit_code"] != 0 and _has_test_script(cwd):
            # Try npm test as fallback (also cached, canonical form)
            npm_run = run_cached(
                NPM_TEST_CMD,
                cwd=cwd,
                timeout=600,
                runner=lambda command, cwd_, timeout_: run_cmd(
                    command.split(), cwd=cwd_, check=False, timeout=timeout_, capture=True
                ),
            )
            if npm_run["exit_code"] == 0:
                return _finalize_test_result(npm_run, tooling="npm")
            # Use the npm result if pytest also failed
            result = npm_run
            final_tooling = "npm"
        return _finalize_test_result(result, tooling=final_tooling)

    # 3. npm test script (no pytest suite)
    if tooling == "npm":
        return _finalize_test_result(
            run_cached(
                NPM_TEST_CMD,
                cwd=cwd,
                timeout=600,
                runner=lambda command, cwd_, timeout_: run_cmd(
                    command.split(), cwd=cwd_, check=False, timeout=timeout_, capture=True
                ),
            ),
            tooling="npm",
        )

    # 4. Repo-local runner script
    if tooling == "repo-script":
        runner_script = _find_repo_test_runner(cwd)
        if runner_script is None:
            return _skip_test_result(
                "Repo-local runner script disappeared before execution — "
                "skipping test step (no-op).",
                tooling="repo-script",
            )
        if runner_script.endswith(".bat"):
            if shutil.which("cmd.exe") is None:
                return _skip_test_result(
                    f"Repo-local test runner {runner_script} requires cmd.exe "
                    "which is not available on this host — skipping test step "
                    "(no-op).",
                    tooling="repo-script",
                )
            command = f"cmd.exe /c {runner_script}"
        else:
            command = f"bash {runner_script}"
        return _finalize_test_result(
            run_cached(command, cwd=cwd, timeout=600, runner=_shell_command_runner),
            tooling="repo-script",
        )

    # 5. Unity project without a configured runner → Unity-specific skip
    if tooling == "unity":
        return _skip_test_result(
            "Unity project detected (Assets/ or ProjectSettings/ProjectVersion.txt) "
            "but no test runner configured — no pytest suite, no npm test script, "
            "and no repo-local runner (run_tests.sh / run_unity_tests.sh / "
            "run_unity_tests.bat). Skipping test step (no-op). Set "
            "IMPLEMENT_TEST_COMMAND to run Unity tests if needed.",
            tooling="unity",
        )

    # 6. No tooling at all → generic skip
    return _skip_test_result(
        "No test tooling detected (no pytest suite, no npm test script, no "
        "repo-local test runner) — skipping test step (no-op).",
        tooling=None,
    )


def run_refactor(work_item_id: str, cwd: str) -> dict[str, Any]:
    """Run the refactor step.

    Args:
        work_item_id: The work item ID (for context).
        cwd: Working directory (worktree root).

    Returns:
        A dict with ``success`` (bool) and ``report`` (dict).
    """
    # Resolve refactor script relative to skills root
    skills_root = Path(__file__).resolve().parents[2]  # .../.pi/agent/skills/
    refactor_path = skills_root / "refactor" / "scripts" / "refactor.py"

    if not refactor_path.exists():
        # Fallback: try the relative path defined in the old SKILL.md convention
        refactor_path = Path(cwd) / "skill" / "refactor" / "scripts" / "refactor.py"
    if not refactor_path.exists():
        LOG.warning("Refactor script not found at %s; skipping refactor step", refactor_path)
        return {"success": True, "report": {"skipped": True, "reason": "script_not_found"}}

    result = run_cmd(
        ["python3", str(refactor_path), work_item_id, "--json"],
        cwd=cwd,
        check=False,
        timeout=300,
        capture=True,
    )
    report: dict[str, Any] = {"skipped": False}
    if result.stdout.strip():
        try:
            report = json.loads(result.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            report["raw_output"] = result.stdout.strip()

    return {
        "success": result.returncode == 0,
        "report": report,
    }


# ---------------------------------------------------------------------------
# State file management
# ---------------------------------------------------------------------------


def write_state(state: ImplementState, worktree_path: str) -> None:
    """Write the implement state file into the worktree.

    Args:
        state: The current implement state.
        worktree_path: Path to the worktree root.
    """
    state_path = Path(worktree_path) / STATE_FILE_NAME
    state_path.write_text(json.dumps(state.to_dict(), indent=2))
    LOG.debug("State written to %s", state_path)


def read_state(worktree_path: str) -> ImplementState | None:
    """Read the implement state file from the worktree.

    Args:
        worktree_path: Path to the worktree root.

    Returns:
        ``ImplementState`` if found, else ``None``.
    """
    state_path = Path(worktree_path) / STATE_FILE_NAME
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text())
        return ImplementState.from_dict(data)
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        LOG.warning("Failed to read state file %s: %s", state_path, exc)
        return None


def remove_state(worktree_path: str) -> None:
    """Remove the implement state file from the worktree.

    Args:
        worktree_path: Path to the worktree root.
    """
    state_path = Path(worktree_path) / STATE_FILE_NAME
    if state_path.exists():
        state_path.unlink()
        LOG.debug("State file removed: %s", state_path)


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------


def phase_start(
    work_item_id: str,
    json_output: bool = False,
    no_refactor: bool = False,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    worktree_path_override: str | None = None,
    max_retry: int = DEFAULT_MAX_RETRY,
    verbose: bool = False,
) -> dict[str, Any]:
    """Phase 1: Setup the implementation environment.

    Steps:
    1. Validate the work item ID
    2. Code Freeze gate: refuse when a release is in progress
    3. Claim the work item (status → in_progress)
    4. Safety gate: check for dirty working tree
    5. Fetch work item details (audit)
    6. Create a worktree from the parent branch
    7. Register signal handlers
    8. Write persistent state

    Args:
        work_item_id: The work item ID.
        json_output: If True, output JSON.
        no_refactor: If True, skip refactor step.
        parent_branch: Parent branch for worktree.
        worktree_path_override: Override worktree path.
        max_retry: Max test-fix retries.
        verbose: Enable verbose logging.

    Returns:
        Dict with result information.
    """
    report: dict[str, Any] = {
        "phase": "start",
        "work_item_id": work_item_id,
        "success": True,
        "worktree_path": "",
        "branch": "",
        "message": "",
    }

    # ── Step 1: Validate work item ID ──────────────────────────────
    if not WORK_ITEM_ID_PATTERN.match(work_item_id):
        msg = f"Invalid work item ID format: {work_item_id}. Expected pattern like SA-XXXXXXXXXXX"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # ── Step 2: Code Freeze gate (before claiming) ────────────────
    if is_code_freeze_active():
        msg = (
            "Project is in Code Freeze — implementation blocked until the "
            "release completes."
        )
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        report["code_freeze"] = True
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {msg}\n")
        return report

    # ── Step 3: Claim the work item via shared helper ─────────────
    LOG.info("Claiming work item %s...", work_item_id)
    try:
        StatusLifecycle.update_status(work_item_id, "in_progress")
    except RuntimeError:
        msg = f"Failed to claim work item {work_item_id}"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # ── Step 4: Safety gate (dirty working tree) ───────────────────
    LOG.info("Checking git working tree...")
    status_output = git_status()
    is_dirty = git_has_dirty_files(status_output)

    if is_dirty:
        msg = (
            f"Dirty working tree detected. Uncommitted changes exist outside .worklog/.\n"
            f"Do NOT stash, commit, or revert the user's uncommitted changes.\n"
            f"STOP and ask the operator how to proceed (commit, stash, revert, or abort)\n"
            f"— never touch the user's working tree without explicit permission.\n"
            f"\nTo abort and release the work item, run:\n"
            f"  python3 {Path(__file__).resolve()} abort {work_item_id}\n"
            f"\nGit status:\n{status_output}"
        )
        LOG.warning("Dirty working tree:\n%s", status_output)
        if not json_output:
            print("\n⚠  Dirty working tree detected")
            print("=" * 60)
            print(status_output)
            print("=" * 60)
            print("Ask the operator how to proceed — do not stash, commit, or revert user changes without permission.\n")
        report["success"] = False
        report["message"] = msg
        report["dirty_worktree"] = True
        wl_add_comment(work_item_id, f"Start phase aborted: dirty working tree detected.\n```\n{status_output}\n```")
        try:
            StatusLifecycle.update_status(work_item_id, "open")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        return report

    # ── Step 5: Fetch work item details ────────────────────────────
    LOG.info("Fetching work item %s...", work_item_id)
    work_item = wl_show(work_item_id)
    if not work_item:
        msg = f"Work item {work_item_id} not found or failed to fetch"
        report["success"] = False
        report["message"] = msg
        try:
            StatusLifecycle.update_status(work_item_id, "open")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    title = work_item.get("title", work_item_id)
    slug = slug_from_title(title)

    # ── Step 6: Create worktree ────────────────────────────────────
    wt_path = worktree_path_override or worktree_path_for(work_item_id, slug)
    branch = branch_name_for(work_item_id, slug)

    LOG.info("Creating worktree at %s from branch %s...", wt_path, parent_branch)
    if not git_worktree_add(branch, wt_path, parent_branch):
        msg = f"Failed to create worktree at {wt_path} from {parent_branch}"
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        try:
            StatusLifecycle.update_status(work_item_id, "open")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        return report

    abs_wt_path = str(Path(wt_path).resolve())

    # Auto-symlink the main checkout's node_modules into the worktree so
    # dist-spawning tests resolve dependencies without manual setup. Never
    # fatal: skip when either side lacks node_modules (SA-0MSGS763C006SM1B).
    _ensure_node_modules_symlink(abs_wt_path, _get_repo_root())

    # Initialise git submodules in the worktree (git worktree add does not
    # do this automatically). Best-effort: a warning on failure, never fatal.
    _ensure_submodules(abs_wt_path, _get_repo_root())

    # Update status with stage via shared helper
    try:
        StatusLifecycle.update_status(work_item_id, "in_progress", stage="in_progress")
    except RuntimeError:
        LOG.warning("Failed to update stage for %s", work_item_id)
    wl_add_comment(
        work_item_id,
        f"Implementation started\n- Worktree: {abs_wt_path}\n- Branch: {branch}",
    )

    # ── Step 7: Register signal handlers ───────────────────────────
    repo_root = str(Path.cwd().resolve())
    _store_signal_globals(abs_wt_path, work_item_id, repo_root)
    _register_signal_handlers()

    # ── Step 8: Write state ────────────────────────────────────────
    state = ImplementState(
        work_item_id=work_item_id,
        worktree_path=abs_wt_path,
        repo_root=repo_root,
        parent_branch=parent_branch,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    write_state(state, abs_wt_path)

    report["worktree_path"] = abs_wt_path
    report["branch"] = branch
    report["title"] = title
    report["message"] = f"Worktree created at {abs_wt_path}. Switch to the worktree and implement."

    if json_output:
        print(format_json_output(report))
    else:
        print()
        print("=" * 60)
        print(f"  Implement: {title} ({work_item_id})")
        print("=" * 60)
        print(f"  Worktree:  {abs_wt_path}")
        print(f"  Branch:    {branch}")
        print(f"  Parent:    {parent_branch}")
        print()
        print("  Next steps:")
        print(f"  1. cd {abs_wt_path}")
        print("  2. Write tests and implementation code")
        print("  3. Run: python3 scripts/implement.py finish <id>")
        print()
        print("  To abort:")
        print(f"  python3 scripts/implement.py abort {work_item_id}")
        print()

    return report


def phase_finish(
    work_item_id: str,
    json_output: bool = False,
    no_refactor: bool = False,
    commit_msg_override: str | None = None,
    max_retry: int = DEFAULT_MAX_RETRY,
    verbose: bool = False,
) -> dict[str, Any]:
    """Phase 2: Complete the implementation.

    Steps:
    0. Worktree placement gate (refuse if changes were made outside the worktree)
    1. Read state to find worktree
    2. Refactor step (unless --no-refactor)
    3. Build
    4. Test (with fix-and-re-run loop)
    5. Commit
    6. Clean up worktree processes
    7. Remove worktree
    8. Push to dev
    9. Restore repo state
    10. Mark in_review

    All implementation work MUST be done inside the worktree created by
    ``phase_start``. If the current directory is outside the worktree and the
    main checkout holds uncommitted changes, finish refuses (step 0).

    Args:
        work_item_id: The work item ID.
        json_output: If True, output JSON.
        no_refactor: If True, skip refactor step.
        commit_msg_override: Custom commit message.
        max_retry: Max test-fix retries.
        verbose: Enable verbose logging.

    Returns:
        Dict with result information.
    """
    report: dict[str, Any] = {
        "phase": "finish",
        "work_item_id": work_item_id,
        "success": True,
        "steps": {},
        "message": "",
    }

    # ── Step 0: Find worktree (from state or directory scan) ──────
    worktree_path = _discover_worktree(work_item_id)
    if not worktree_path:
        msg = (
            f"Cannot find worktree for {work_item_id}. "
            f"Run `python3 scripts/implement.py start {work_item_id}` first."
        )
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # Read state for metadata
    state = read_state(worktree_path)

    report["worktree_path"] = worktree_path
    # Compute branch name from the worktree
    branch = Path(worktree_path).name  # e.g., wl-SA-xxx-slug

    # Check we're in the right directory
    if not Path(worktree_path).exists():
        msg = f"Worktree directory does not exist: {worktree_path}"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # ── Step 0.5: Worktree placement gate ──────────────────────────
    # All implementation work must happen inside the worktree created by
    # `implement.py start`. If changes were made in the main checkout
    # instead, refuse rather than silently build/test/commit an empty
    # worktree and mark the item in_review.
    violation = _worktree_placement_violation(
        worktree_path,
        parent_branch=state.parent_branch if state else DEFAULT_PARENT_BRANCH,
    )
    if violation:
        msg = violation
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        report["worktree_enforcement"] = True
        wl_add_comment(
            work_item_id,
            "Finish phase refused: implementation changes found outside the "
            f"worktree.\n```\n{msg}\n```",
        )
        try:
            StatusLifecycle.update_status(work_item_id, "open")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {msg}\n")
        return report

    # ── Step 1: Refactor step ──────────────────────────────────────
    if not no_refactor:
        LOG.info("Running refactor step...")
        refactor_result = run_refactor(work_item_id, worktree_path)
        report["steps"]["refactor"] = refactor_result
        if not refactor_result["success"]:
            LOG.warning("Refactor step reported issues; continuing workflow")
    else:
        report["steps"]["refactor"] = {"skipped": True, "reason": "no_refactor_flag"}
        LOG.info("Refactor step skipped (--no-refactor)")

    # ── Step 2: Build ──────────────────────────────────────────────
    LOG.info("Running build...")
    build_result = run_build(worktree_path)
    report["steps"]["build"] = build_result
    if not build_result["success"]:
        msg = f"Build failed (exit code {build_result['exit_code']})"
        LOG.error(msg)
        if build_result["stderr"]:
            LOG.error("Build stderr:\n%s", build_result["stderr"][:2000])
        report["success"] = False
        report["message"] = msg
        wl_add_comment(
            work_item_id,
            f"Build failed during finish phase.\n```\n{build_result['stderr'][:500]}\n```",
        )
        try:
            StatusLifecycle.update_status(work_item_id, "open")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        return report

    # ── Step 3: Test with fix-and-re-run loop ──────────────────────
    LOG.info("Running test suite...")
    test_attempts = 0
    test_result = run_tests(worktree_path)
    report["steps"]["tests"] = []
    report["steps"]["tests"].append({
        "attempt": test_attempts + 1,
        "success": test_result["success"],
        "failures": test_result.get("failures", []),
        "skipped": test_result.get("skipped", False),
        "tooling": test_result.get("tooling"),
    })

    while not test_result["success"] and test_attempts < max_retry:
        test_attempts += 1
        failures = test_result.get("failures", [])
        failure_summary = "\n".join(failures[:20]) if failures else test_result["stderr"][:1000]

        msg = (
            f"Test run {test_attempts}/{max_retry} failed.\n"
            f"Failures:\n{failure_summary}\n\n"
            f"Please fix the failures and re-run:\n"
            f"  python3 scripts/implement.py finish {work_item_id}"
        )
        LOG.warning("Test run %d/%d failed", test_attempts, max_retry)

        if json_output:
            # Report current state and let the agent loop externally
            report["success"] = False
            report["message"] = msg
            report["test_phase"] = {
                "attempt": test_attempts,
                "max_retry": max_retry,
                "failures": failures,
            }
            print(format_json_output(report))
            return report

        # Interactive mode: prompt user to fix
        print()
        print("=" * 60)
        print(f"  ⚠  Test run {test_attempts}/{max_retry} failed")
        print("=" * 60)
        print("  Failures:")
        for f in failures[:10]:
            print(f"    • {f}")
        print()
        print("  Fix the failures and press Enter to re-run tests.")
        print("  Type 'abort' to abort the finish phase, or 'skip' to skip tests.")
        try:
            choice = input("  [Enter/skip/abort]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "abort"

        if choice == "abort":
            msg = f"Aborted by user during test fix loop (attempt {test_attempts}/{max_retry})"
            report["success"] = False
            report["message"] = msg
            wl_add_comment(work_item_id, msg)
            try:
                StatusLifecycle.update_status(work_item_id, "open")
            except RuntimeError:
                LOG.error("Failed to reset work item %s status to open", work_item_id)
            if json_output:
                print(format_json_output(report))
            else:
                print(f"\n{msg}")
            return report

        if choice == "skip":
            LOG.warning("Tests skipped by user choice")
            test_result["success"] = True
            report["steps"]["tests"][-1]["skipped"] = True
            break

        # Re-run tests
        LOG.info("Re-running test suite (attempt %d/%d)...", test_attempts + 1, max_retry)
        test_result = run_tests(worktree_path)
        report["steps"]["tests"].append({
            "attempt": test_attempts + 1,
            "success": test_result["success"],
            "failures": test_result.get("failures", []),
            "skipped": test_result.get("skipped", False),
            "tooling": test_result.get("tooling"),
        })

    if not test_result["success"]:
        msg = f"Tests failed after {max_retry} retries. Aborting finish phase."
        report["success"] = False
        report["message"] = msg
        wl_add_comment(work_item_id, msg)
        try:
            StatusLifecycle.update_status(work_item_id, "open")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    LOG.info("All tests passed")

    try:
        with StatusLifecycle(work_item_id, target_stage="in_review"):
            # ── Step 4: Commit ─────────────────────────────────────────────
            commit_msg = commit_msg_override or f"{work_item_id}: Implementation complete"
            LOG.info("Committing changes...")
            if not git_commit(worktree_path, commit_msg):
                raise RuntimeError("git commit failed")

            commit_hash = git_get_commit_hash(worktree_path)
            report["steps"]["commit"] = {
                "hash": commit_hash,
                "message": commit_msg,
            }
            LOG.info("Committed at %s", commit_hash)

            # ── Step 5: Clean up worktree processes ────────────────────────
            LOG.info("Cleaning up worktree processes...")
            cleanup_result = cleanup_worktree_processes(worktree_path)
            report["steps"]["cleanup"] = cleanup_result
            if cleanup_result.get("warning"):
                LOG.warning("Process cleanup warning: %s", cleanup_result["warning"])

            # ── Step 6: Remove worktree ────────────────────────────────────
            LOG.info("Removing worktree...")
            remove_state(worktree_path)
            if not _remove_worktree(
                worktree_path,
                repo_root=state.repo_root if state else None,
            ):
                msg = f"Failed to remove worktree at {worktree_path}"
                LOG.warning(msg)
                report["steps"]["worktree_removed"] = False

            # ── Step 7: Restore repo state ─────────────────────────────────
            repo_root = (
                state.repo_root
                if state
                else (_get_repo_root() or str(Path.cwd().resolve()))
            )
            _restore_repo_state(repo_root)

            # ── Step 8: Push to dev ────────────────────────────────────────
            LOG.info("Pushing to dev...")
            if not git_push_to_dev(repo_root, branch):
                raise RuntimeError("git push to dev failed.")

            report["steps"]["push"] = {"success": True, "hash": commit_hash}
            LOG.info("Push to dev succeeded")

            # StatusLifecycle.__exit__ sets status=completed, stage=in_review

    except RuntimeError as e:
        msg = str(e)
        report["success"] = False
        report["message"] = msg
        # Status was restored to original by StatusLifecycle; override to open
        try:
            StatusLifecycle.update_status(work_item_id, "open")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if "git push to dev failed" in msg:
            wl_add_comment(
                work_item_id,
                f"Push to dev failed. Commit {commit_hash} is local. "
                f"Push manually: git push origin {branch}:refs/heads/dev",
            )
        elif "git commit failed" in msg:
            wl_add_comment(work_item_id, "Commit failed during finish phase.")
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    #── Step 9: Add completion comment ─────────────────────────────
    wl_add_comment(
        work_item_id,
        f"Implementation complete.\n- Commit: {commit_hash}\n- Branch: {branch}\n- Worktree: {worktree_path}",
    )

    report["success"] = True
    report["hash"] = commit_hash
    report["message"] = f"Implementation complete. Commit {commit_hash} pushed to dev."

    if json_output:
        print(format_json_output(report))
    else:
        print()
        print("=" * 60)
        print(f"  ✅ Implementation complete for {work_item_id}")
        print("=" * 60)
        print(f"  Commit: {commit_hash}")
        print(f"  Branch: {branch}")
        print("  Status: in_review")
        print()

    return report


def phase_abort(
    work_item_id: str,
    json_output: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Abort the implementation workflow.

    Steps:
    1. Reset work item status to open
    2. Find and cleanup worktree
    3. Remove worktree
    4. Log abort comment

    Args:
        work_item_id: The work item ID.
        json_output: If True, output JSON.
        verbose: Enable verbose logging.

    Returns:
        Dict with result information.
    """
    report: dict[str, Any] = {
        "phase": "abort",
        "work_item_id": work_item_id,
        "success": True,
        "message": "",
    }

    # ── Step 1: Reset status ───────────────────────────────────────
    LOG.info("Resetting work item %s status to open...", work_item_id)
    try:
        StatusLifecycle.update_status(work_item_id, "open")
    except RuntimeError:
        LOG.error("Failed to reset work item %s status to open", work_item_id)

    # ── Step 2: Find and cleanup worktree ──────────────────────────
    worktree_path = _discover_worktree(work_item_id)
    if worktree_path and Path(worktree_path).exists():
        LOG.info("Cleaning up worktree at %s...", worktree_path)
        cleanup_worktree_processes(worktree_path)
        remove_state(worktree_path)
        _remove_worktree(worktree_path)
        report["worktree_path"] = worktree_path

    # ── Step 3: Restore repo state ─────────────────────────────────
    repo_root = str(Path.cwd().resolve())
    _restore_repo_state(repo_root)

    # ── Step 4: Log comment ────────────────────────────────────────
    wl_add_comment(work_item_id, "Implementation aborted.")

    report["message"] = f"Work item {work_item_id} aborted and cleaned up."

    if json_output:
        print(format_json_output(report))
    else:
        print(f"\n✅ Implementation aborted for {work_item_id}. Worktree cleaned up.\n")

    return report


def phase_parent(
    work_item_id: str,
    json_output: bool = False,
    no_refactor: bool = False,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    verbose: bool = False,
) -> dict[str, Any]:
    """Phase: implement a parent work item by recursing into its children.

    Invoking the implement skill on a parent/epic item must automatically
    implement the children (and their blockers) in dependency order instead
    of dead-ending at the old Step 5.1 gate (SA-0MSQBM2FK005NW1T). Each
    invocation of this phase advances the chain by one child:

    1. Fetch the parent and its children (``wl show --children``).
    2. No children → behave like a leaf (no status change; use start/finish).
    3. Resolve the dependency graph among children (outbound
       ``wl dep list`` edges): in-chain blockers are implemented before
       the children they block; cycles fail fast; cross-project blockers
       are reported as coordination notes.
    4. Classify children: terminal → skip; in_progress elsewhere → skip+report;
       blocked by a non-terminal sibling → not startable (reported);
       otherwise → implementable.
    5. All children terminal → advance the parent to ``completed``/``in_review``
       (existing Step 5.1 advancement retained) and comment a per-child summary.
    6. Otherwise → start the next startable child (claim + worktree via
       ``phase_start``) and report the worktree path; the caller implements
       that child with the standard workflow (``implement.py finish
       <child>``), then re-invokes this phase for the next child.

    Worktree isolation is preserved per child: every child is implemented in
    its own worktree created by ``phase_start`` (never the main checkout);
    sequential children reuse/rotate the same ``.worklog/worktrees``
    machinery. No worktree is created for the parent itself.

    Args:
        work_item_id: The parent work item ID.
        json_output: If True, output JSON.
        no_refactor: If True, skip the refactor step for started children.
        parent_branch: Parent branch for child worktrees.
        verbose: Enable verbose logging.

    Returns:
        Dict with the recursion plan: per-child classifications, the next
        child started (with its worktree), or parent advancement when all
        children are terminal.
    """
    report: dict[str, Any] = {
        "phase": "parent",
        "work_item_id": work_item_id,
        "success": True,
        "children": [],
        "message": "",
    }

    # ── Step 1: Validate work item ID ──────────────────────────────
    if not WORK_ITEM_ID_PATTERN.match(work_item_id):
        msg = f"Invalid work item ID format: {work_item_id}. Expected pattern like SA-XXXXXXXXXXX"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # ── Step 2: Code Freeze gate (before claiming/starting children) ──
    if is_code_freeze_active():
        msg = (
            "Project is in Code Freeze — implementation blocked until the "
            "release completes."
        )
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        report["code_freeze"] = True
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {msg}\n")
        return report

    # ── Step 3: Fetch parent + children ────────────────────────────
    parent = wl_show(work_item_id)
    if not parent:
        msg = f"Work item {work_item_id} not found or failed to fetch"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report
    children = wl_show_children(work_item_id)

    # ── Step 4: No children → behave like a leaf ───────────────────
    if not children:
        report["leaf"] = True
        report["message"] = (
            f"Work item {work_item_id} has no children — behave as a leaf "
            "item: use `implement.py start/finish` as usual (no recursion)."
        )
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  Parent: {work_item_id} — no children (leaf)")
            print("=" * 60)
            print(f"  {report['message']}")
            print()
        return report

    # ── Step 5: Classify children ──────────────────────────────────
    classifications: list[dict[str, Any]] = []
    blockers_map: dict[str, list[dict[str, Any]]] = {}
    for child in children:
        cid = str(child.get("id", ""))
        # Outbound (depends-on) edges: what this child is blocked by.
        child_blockers = wl_dep_blockers(cid) if cid else []
        blockers_map[cid] = child_blockers
        # Blockers outside the children set (e.g. cross-project) are
        # coordination notes, not in-chain edges (SA-0MSQBM2FK005NW1T).
        in_chain_ids = {str(c.get("id")) for c in children}
        external = [
            str(b.get("id"))
            for b in child_blockers
            if str(b.get("id")) not in in_chain_ids
        ]
        classifications.append({
            "id": cid,
            "title": child.get("title", ""),
            "status": child.get("status", ""),
            "action": _classify_child(child),
            "external_blockers": external,
        })
    report["children"] = classifications

    # ── Step 5.5: Resolve dependency order (blockers first) ────────
    ordered_children, cycle_error = _resolve_implementation_order(
        children, blockers_map
    )
    if cycle_error:
        report["success"] = False
        report["message"] = cycle_error
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  ⛔ {cycle_error}")
            print("=" * 60)
            print()
        return report

    # ── Step 6: All children terminal → advance the parent ─────────
    if all(c["action"] == "skip-terminal" for c in classifications):
        parent_status = str(parent.get("status", ""))
        already_terminal = _is_terminal_status(parent_status)
        if not already_terminal:
            try:
                StatusLifecycle.update_status(
                    work_item_id, "completed", stage="in_review"
                )
            except RuntimeError:
                msg = f"Failed to advance parent {work_item_id}"
                report["success"] = False
                report["message"] = msg
                if json_output:
                    print(format_json_output(report))
                else:
                    LOG.error(msg)
                return report
        summary = "\n".join(
            f"- {c['id']} ({c['status']})" for c in classifications
        )
        wl_add_comment(
            work_item_id,
            f"All children are in a terminal stage. Parent advanced to "
            f"in_review.\n{summary}",
        )
        report["parent_advanced"] = True
        report["message"] = (
            f"All children of {work_item_id} are terminal; parent advanced "
            f"to completed/in_review."
        )
        if already_terminal:
            report["message"] = (
                f"All children of {work_item_id} are terminal; parent is "
                f"already in a terminal state ({parent_status})."
            )
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  ✅ Parent advanced: {work_item_id} → in_review")
            print("=" * 60)
            print(summary)
            print()
        return report

    # ── Step 7: Start the next child (claim + worktree) ────────────
    next_child = _next_child_to_implement(ordered_children, blockers_map)
    if next_child is None:
        # No startable child: every remaining child is terminal, in progress
        # elsewhere, or blocked by a non-terminal sibling.
        blocked = [
            c["id"]
            for c in classifications
            if c["action"] not in ("skip-terminal", "skip-in-progress")
        ]
        report["message"] = (
            f"No child of {work_item_id} is currently startable: "
            f"non-terminal children not in progress are blocked by "
            f"non-terminal siblings or unavailable. Re-run this phase "
            f"after one completes."
        )
        if blocked:
            report["blocked_children"] = blocked
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  ⏳ {report['message']}")
            print("=" * 60)
            print()
        return report

    next_id = next_child.get("id", "")
    LOG.info("Starting next child %s of parent %s...", next_id, work_item_id)
    start_result = phase_start(
        next_id,
        json_output=False,
        no_refactor=no_refactor,
        parent_branch=parent_branch,
        verbose=verbose,
    )
    if not start_result.get("success"):
        msg = (
            f"Failed to start child {next_id}: "
            f"{start_result.get('message', 'unknown error')}"
        )
        LOG.error(msg)
        report["success"] = False
        report["next_child"] = next_id
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {msg}\n")
        return report

    report["next_child"] = next_id
    report["worktree_path"] = start_result.get("worktree_path", "")
    report["branch"] = start_result.get("branch", "")
    report["message"] = (
        f"Started child {next_id}. Implement it in "
        f"{start_result.get('worktree_path', '')}, then run "
        f"`implement.py finish {next_id}`, then re-run "
        f"`implement.py parent {work_item_id}` for the next child."
    )

    if json_output:
        print(format_json_output(report))
    else:
        print()
        print("=" * 60)
        print(f"  Implement child: {next_id} (of {work_item_id})")
        print("=" * 60)
        print(f"  Worktree: {report['worktree_path']}")
        print(f"  Branch:   {report['branch']}")
        print()
        print("  Next steps:")
        print(f"  1. cd {report['worktree_path']}")
        print("  2. Write tests and implementation code")
        print(f"  3. Run: python3 scripts/implement.py finish {next_id}")
        print(f"  4. Re-run: python3 scripts/implement.py parent {work_item_id}")
        print()

    return report


def _is_worktree(path: Path) -> bool:
    """Check if *path* is a git worktree (not the main working tree).

    A git worktree has ``.git`` as a **file** containing a gitdir reference.
    The main working tree has ``.git`` as a **directory**.

    Args:
        path: The path to check.

    Returns:
        True if ``.git`` exists and is a file (worktree).
        False if ``.git`` is a directory (main working tree) or does not exist.
    """
    git_path = path / ".git"
    return git_path.is_file()


def _worktree_placement_violation(
    worktree_path: str,
    cwd: str | None = None,
    repo_root: str | None = None,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
) -> str | None:
    """Return an error message when implementation work landed outside the worktree.

    All implementation work must happen inside the worktree created by
    ``implement.py start``. A violation exists only when ALL of:

    1. the current directory is not the worktree (or a subdirectory of it),
    2. the main checkout holds uncommitted changes outside ``.worklog/``, and
    3. the worktree itself holds NO changes (clean and at the parent branch
       HEAD) — i.e. the work was done in the main checkout, not the worktree.

    Condition 3 keeps the gate precise: pre-existing/unrelated dirt in the
    main checkout does not block a legitimate finish when the work actually
    lives in the worktree.

    Args:
        worktree_path: Absolute path to the worktree.
        cwd: Directory to treat as the current directory (defaults to
            ``os.getcwd()``).
        repo_root: Main repo root (defaults to discovery from *cwd*).
        parent_branch: Branch the worktree forked from (default: ``dev``).

    Returns:
        An actionable error message string if placement is violated,
        otherwise ``None``.
    """
    wt = Path(worktree_path).resolve()
    cur = Path(cwd or os.getcwd()).resolve()
    if cur == wt or wt in cur.parents:
        return None  # already inside the worktree

    root = Path(repo_root or _get_repo_root(cur) or cur).resolve()
    if not git_has_dirty_files(git_status(cwd=str(root))):
        return None  # main checkout clean; work must be in the worktree

    if _git_path_has_changes(wt, parent_branch):
        return None  # work lives in the worktree; main-checkout dirt is unrelated

    return (
        f"Work done outside the worktree: uncommitted changes were found in "
        f"the main checkout at {root} while the worktree {wt} contains no "
        f"changes. All implementation work MUST be done in the worktree "
        f"created by `implement.py start`. Move your changes into the "
        f"worktree and re-run `implement.py finish`."
    )


def _git_path_has_changes(path: Path, parent_branch: str) -> bool:
    """True when the git directory at *path* contains implementation changes.

    Detects both uncommitted changes and commits ahead of *parent_branch*.

    Args:
        path: Directory to check (e.g. the worktree root).
        parent_branch: Branch the directory forked from (e.g. ``dev``).

    Returns:
        True if the directory is dirty or its HEAD differs from the parent
        branch; False only when it is clean AND at the parent branch HEAD.
    """
    if git_has_dirty_files(git_status(cwd=str(path))):
        return True
    head = run_cmd(
        ["git", "rev-parse", "HEAD"], cwd=str(path), check=False, capture=True
    )
    parent = run_cmd(
        ["git", "rev-parse", parent_branch], cwd=str(path), check=False, capture=True
    )
    if head.returncode == 0 and parent.returncode == 0:
        return head.stdout.strip() != parent.stdout.strip()
    return True  # cannot compare; fail open


def _discover_worktree(work_item_id: str) -> str | None:
    """Discover the worktree path for a given work item.

    Checks:
    1. State file in a known worktree path
    2. Current directory (if inside a matching worktree)
    3. Scan .worklog/worktrees/ for matching directories

    Safety: will NOT return a path that looks like the main working tree
    (where ``.git`` is a directory).  This prevents catastrophic deletion
    when ``.implement_state.json`` is accidentally present in the repo root.

    Args:
        work_item_id: The work item ID.

    Returns:
        Absolute path to the worktree, or None if not found or if the
        candidate path is the main working tree (not a worktree).
    """
    # Check if state file is in the current directory or a parent
    cwd = Path.cwd().resolve()
    state_file = cwd / STATE_FILE_NAME
    if state_file.exists():
        # SAFETY: verify .git is a FILE (worktree), not a directory (main repo)
        if _is_worktree(cwd):
            return str(cwd)
        LOG.critical(
            "Refusing to treat %s as a worktree: .git is a directory, "
            "indicating the main working tree. .implement_state.json found "
            "in repo root is likely a stale artifact.",
            cwd,
        )
        return None

    # Check if we're inside a .worklog/worktrees/ directory
    if ".worklog/worktrees" in str(cwd):
        # SAFETY: also verify .git is a file here
        if _is_worktree(cwd):
            return str(cwd)
        LOG.warning(
            "Inside a .worklog/worktrees path but .git is not a file; "
            "skipping %s",
            cwd,
        )
        return None

    # Scan .worklog/worktrees/ for directories matching wl-<work_item_id>-*
    repo_root = Path.cwd().resolve()
    worktrees_dir = repo_root / DEFAULT_WORKTREE_DIR
    if worktrees_dir.exists():
        pattern = f"wl-{work_item_id}-*"
        for match_dir in worktrees_dir.glob(pattern):
            if match_dir.is_dir():
                return str(match_dir.resolve())

    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list (default: sys.argv[1:]).

    Returns:
        Parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description="Implement skill: deterministic implementation workflow orchestration",
    )
    parser.add_argument(
        "action",
        choices=["start", "finish", "abort", "parent"],
        help="Workflow phase to execute",
    )
    parser.add_argument(
        "work_item_id",
        help="Work item ID (e.g., SA-XXXXXXXXXXX)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format",
    )
    parser.add_argument(
        "--no-refactor",
        action="store_true",
        help="Skip the refactor step",
    )
    parser.add_argument(
        "--max-retry",
        type=int,
        default=DEFAULT_MAX_RETRY,
        help=f"Max test-fix loop retries (default: {DEFAULT_MAX_RETRY})",
    )
    parser.add_argument(
        "--commit-msg",
        default=None,
        help="Commit message override",
    )
    parser.add_argument(
        "--parent-branch",
        default=DEFAULT_PARENT_BRANCH,
        help=f"Parent branch for worktree (default: {DEFAULT_PARENT_BRANCH})",
    )
    parser.add_argument(
        "--worktree-path",
        default=None,
        help="Override worktree path",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the implement orchestration.

    Args:
        argv: Command-line arguments.

    Returns:
        Exit code: 0 = success, 1 = error, 2 = aborted.
    """
    try:
        return _main(argv)
    except KeyboardInterrupt:
        LOG.warning("Interrupted by user")
        if _work_item_id_global:
            _safety_reset_if_in_progress(_work_item_id_global)
        return 2
    except Exception as exc:  # noqa: BLE001
        LOG.error("Unhandled exception: %s", exc)
        LOG.debug(traceback.format_exc())
        if _work_item_id_global:
            _safety_reset_if_in_progress(_work_item_id_global)
        return 1


def _main(argv: list[str] | None = None) -> int:
    """Internal main with proper exception handling."""
    args = parse_args(argv)

    # Track the work item for ALL phases (start/finish/abort) so that the
    # top-level exception and signal handlers can always release the item
    # back to a valid terminal state — never leave it stuck in-progress.
    # phase_start() later overwrites these globals with the real worktree
    # path and repo root once they are known.
    _store_signal_globals("", args.work_item_id, "")
    _register_signal_handlers()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.action == "start":
        result = phase_start(
            work_item_id=args.work_item_id,
            json_output=args.json,
            no_refactor=args.no_refactor,
            parent_branch=args.parent_branch,
            worktree_path_override=args.worktree_path,
            max_retry=args.max_retry,
            verbose=args.verbose,
        )
    elif args.action == "finish":
        result = phase_finish(
            work_item_id=args.work_item_id,
            json_output=args.json,
            no_refactor=args.no_refactor,
            commit_msg_override=args.commit_msg,
            max_retry=args.max_retry,
            verbose=args.verbose,
        )
    elif args.action == "abort":
        result = phase_abort(
            work_item_id=args.work_item_id,
            json_output=args.json,
            verbose=args.verbose,
        )
    elif args.action == "parent":
        result = phase_parent(
            work_item_id=args.work_item_id,
            json_output=args.json,
            no_refactor=args.no_refactor,
            parent_branch=args.parent_branch,
            verbose=args.verbose,
        )
    else:
        LOG.error("Unknown action: %s", args.action)
        return 1

    if not result.get("success"):
        return 2 if result.get("dirty_worktree") else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
