#!/usr/bin/env python3
"""Shared status lifecycle helper for skills.

Provides a reusable context manager that manages work-item status and stage
transitions consistently across all skills (audit, implement, plan, etc.).

Usage::

    from skill.shared.status_lifecycle import StatusLifecycle

    with StatusLifecycle(
        work_item_id,
        assignee="agent_name",
        target_stage="in_review",
    ) as ctx:
        # ... do work ...
        # On normal exit: status → completed
        # On exception: status → original value (rollback)

The helper is idempotent — safe to call when the work item is already in the
target state. It logs all transitions and raises exceptions on ``wl`` command
failures so callers can handle them explicitly.

For testing or custom runners, an injectable ``runner`` callable can be passed::

    def my_runner(cmd: list[str]) -> subprocess.CompletedProcess:
        ...

    with StatusLifecycle(id, runner=my_runner):
        ...
"""  # noqa: EXE001

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

LOG = logging.getLogger("skill.shared.status_lifecycle")

# Type alias for an injectable command runner.
# Takes a command list, returns a CompletedProcess (like subprocess.run).
Runner = Callable[[list[str]], subprocess.CompletedProcess]


# ======================================================================
# Worklog-dir resolution
# ======================================================================


def _is_initialized_worklog(path: Path) -> bool:
    """True when *path* is a usable (initialized) worklog directory.

    An initialized worklog has an ``initialized`` marker file (or a
    ``worklog.db``). A directory holding only ``config.yaml`` — e.g. a
    git worktree's committed copy of ``.worklog/`` — is NOT initialized
    and pointing ``wl`` at it fails with "Worklog system is not initialized".

    Args:
        path: Candidate ``.worklog`` directory.

    Returns:
        True if the directory exists and is initialized.
    """
    return path.is_dir() and (
        (path / "initialized").is_file() or (path / "worklog.db").is_file()
    )


def _detect_worklog_dir() -> Path | None:
    """Detect the target project's ``.worklog`` directory.

    Resolution order (mirrors ``audit_runner.TARGET_PROJECT_ROOT``):
      1. ``<cwd>/.worklog``
      2. ``<git root>/.worklog`` via ``git rev-parse --show-toplevel``
      3. nearest ancestor directory containing ``.worklog``

    Returns ``None`` when no worklog directory can be resolved (the caller
    should then run ``wl`` without ``--worklog-dir`` and surface the error).
    """
    cwd = Path.cwd()
    cand = cwd / ".worklog"
    if cand.is_dir():
        return cand
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            cand = Path(proc.stdout.strip()) / ".worklog"
            if cand.is_dir():
                return cand
    except OSError:
        pass
    for parent in cwd.parents:
        cand = parent / ".worklog"
        if cand.is_dir():
            return cand
    return None


def worklog_dir_flag() -> list[str]:
    """Return ``["--worklog-dir", <path>]`` when cwd is not a worklog root.

    Skill scripts shell out to the ``wl`` CLI which resolves ``.worklog``
    relative to the caller's cwd. When the cwd is not already a worklog
    project root (e.g. the skill install dir), pass the explicit
    ``--worklog-dir`` so the command succeeds regardless of cwd.

    A git worktree's ``.worklog/`` contains only the committed
    ``config.yaml`` and is NOT initialized, so ``wl`` fails from inside a
    worktree. Since worktrees live under the main checkout's
    ``.worklog/worktrees/``, the nearest ancestor with an initialized
    ``.worklog`` is the main checkout — point ``wl`` at it.

    Returns an empty list when no worklog directory is resolvable (the
    command will run as-is and any failure will surface real error detail).
    """
    cwd_worklog = Path.cwd() / ".worklog"
    if cwd_worklog.is_dir():
        if _is_initialized_worklog(cwd_worklog):
            return []
        # cwd/.worklog exists but is not initialized (e.g. a git worktree
        # with only the committed config.yaml): resolve the nearest
        # ancestor project .worklog that IS initialized (the main checkout).
        for parent in Path.cwd().parents:
            cand = parent / ".worklog"
            if _is_initialized_worklog(cand):
                return ["--worklog-dir", str(cand)]
        return []
    wl_dir = _detect_worklog_dir()
    if wl_dir is None:
        return []
    return ["--worklog-dir", str(wl_dir)]


def _wl_error_detail(proc: subprocess.CompletedProcess) -> str:
    """Extract error detail from a failed ``wl`` subprocess.

    ``wl`` prints its error as JSON on **stdout** (e.g.
    ``{"success": false, "error": "..."}``) with a non-zero exit code and
    usually empty stderr. Parse the stdout JSON ``error`` field first, then
    fall back to stdout text, then stderr. Never returns an empty string.
    """
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        try:
            data = json.loads(out)
            if isinstance(data, dict) and data.get("error"):
                return str(data["error"])
        except json.JSONDecodeError:
            pass
        return out
    if err:
        return err
    return "(no output)"


# ======================================================================
# Default runner
# ======================================================================


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Default command runner using ``subprocess.run``."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_wl_with_runner(runner: Runner, cmd: list[str]) -> dict:
    """Run a ``wl`` command via an injectable runner and return parsed JSON.

    Args:
        runner: A callable that takes a command list and returns a CompletedProcess.
        cmd: The command as a list of strings.

    Returns:
        The parsed JSON response dict.

    Raises:
        RuntimeError: If the command fails or returns invalid JSON. The error
            message includes the underlying ``wl`` error detail (stdout JSON
            ``error`` field, stdout text, or stderr).
    """
    full_cmd = list(cmd)
    if full_cmd and full_cmd[0] == "wl":
        # Inject --worklog-dir when cwd is not the target worklog root so the
        # command succeeds regardless of the caller's cwd.
        full_cmd[1:1] = worklog_dir_flag()
    LOG.debug("Running: %s", " ".join(full_cmd))
    proc = runner(full_cmd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed ({' '.join(full_cmd)}): {_wl_error_detail(proc)}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from wl: {exc}") from exc
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            f"Worklog command failed: {data.get('error', 'unknown error')}"
        )
    return data


# Backwards-compatible convenience wrapper using the default runner
def _run_wl(cmd: list[str]) -> dict:
    """Run a ``wl`` command using the default subprocess runner.

    See :func:`_run_wl_with_runner` for details.
    """
    return _run_wl_with_runner(_default_runner, cmd)


def run_wl(cmd: list[str], runner: Runner | None = None) -> dict:
    """Run a ``wl`` command via the shared runner (public helper).

    Thin public wrapper over :func:`_run_wl_with_runner` for skill scripts
    that shell out to ``wl`` (worklog-dir injection + detailed errors).

    Args:
        cmd: The ``wl`` command as a list of strings.
        runner: Optional injectable command runner for testing.

    Returns:
        The parsed JSON response dict.

    Raises:
        RuntimeError: If the command fails or returns invalid JSON.
    """
    return _run_wl_with_runner(runner or _default_runner, cmd)


# ======================================================================
# StatusLifecycle context manager
# ======================================================================


class StatusLifecycle:
    """Context manager for work-item status lifecycle management.

    Captures original status on entry, sets ``in_progress``, and:

    - **Normal exit:** transitions to ``completed`` (optionally advancing stage)
    - **Exception exit:** restores original status (rollback)
    - **restore_on_exit=True:** restores the original status on success too —
      for read-only analysis skills (find-related, refactor, effort-and-risk)
      that must NOT advance the workflow stage. This avoids setting
      ``status=completed`` on items still in ``idea``/``intake_complete``/
      ``plan_complete`` stages, which the wl CLI rejects (``completed`` is
      only compatible with stages ``in_review``/``done``).
    - **Idempotent:** safe to call when work item is already in the target state

    Args:
        work_item_id: The work item ID (e.g. ``SA-XXXX``).
        assignee: Optional assignee name. Set on entry; cleared on failure exit.
        target_stage: Optional stage value (e.g. ``in_review``) to set
            on successful exit.
        restore_on_exit: If True, restore the original status on successful
            exit instead of advancing to ``completed``.
        runner: Optional injectable command runner for testing.
            Must have signature ``(cmd: list[str]) -> subprocess.CompletedProcess``.

    Raises:
        RuntimeError: If a ``wl`` command fails. Callers can catch and handle.

    Example::

        with StatusLifecycle("SA-0ABC123"):
            run_audit()

        # Status is now ``completed``

        with StatusLifecycle("SA-0ABC123", target_stage="in_review"):
            implement()

        # Status is now ``completed``, stage is ``in_review``

        with StatusLifecycle("SA-0ABC123", restore_on_exit=True):
            find_related_work()

        # Status is restored to its pre-run value (never ``completed``)
    """

    def __init__(
        self,
        work_item_id: str,
        *,
        assignee: str | None = None,
        target_stage: str | None = None,
        restore_on_exit: bool = False,
        runner: Runner | None = None,
    ) -> None:
        self._work_item_id = work_item_id
        self._assignee = assignee
        self._target_stage = target_stage
        self._restore_on_exit = restore_on_exit
        self._runner = runner or _default_runner
        self._original_status: str = "open"  # safe default
        self._did_set_in_progress: bool = False

    # ------------------------------------------------------------------
    # Public helpers (usable outside context manager too)
    # ------------------------------------------------------------------

    @staticmethod
    def show(work_item_id: str, runner: Runner | None = None) -> dict:
        """Fetch a work item via ``wl show`` and return the parsed JSON.

        Args:
            work_item_id: The work item ID.
            runner: Optional injectable runner for testing.

        Returns:
            The parsed JSON dict from ``wl show``.

        Raises:
            RuntimeError: If the ``wl`` command fails.
        """
        r = runner or _default_runner
        return _run_wl_with_runner(r, ["wl", "show", work_item_id, "--json"])

    @staticmethod
    def update_status(
        work_item_id: str,
        status: str,
        stage: str | None = None,
        assignee: str | None = None,
        needs_producer_review: bool | None = None,
        runner: Runner | None = None,
    ) -> dict:
        """Update a work item's status (and optionally stage/assignee/needs_producer_review).

        Args:
            work_item_id: The work item ID.
            status: New status value (e.g. ``open``, ``in_progress``, ``completed``).
            stage: Optional new stage value.
            assignee: Optional new assignee value.
            needs_producer_review: Optional boolean. When set, passes
                ``--needs-producer-review yes|no`` to ``wl update``.
                ``None`` (default) omits the flag entirely, preserving
                backward compatibility.
            runner: Optional injectable runner for testing.

        Returns:
            The parsed JSON dict from ``wl update``.

        Raises:
            RuntimeError: If the ``wl`` command fails.
        """
        cmd = ["wl", "update", work_item_id, "--status", status, "--json"]
        if stage is not None:
            cmd.extend(["--stage", stage])
        if assignee is not None:
            cmd.extend(["--assignee", assignee])
        if needs_producer_review is not None:
            npr_value = "yes" if needs_producer_review else "no"
            cmd.extend(["--needs-producer-review", npr_value])
        r = runner or _default_runner
        return _run_wl_with_runner(r, cmd)

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> StatusLifecycle:  # noqa: PYI034
        """Capture original status, set ``in_progress`` (and optional assignee)."""
        # Capture original status
        try:
            data = self.show(self._work_item_id, runner=self._runner)
            wi = data.get("workItem", {})
            self._original_status = wi.get("status", "open")
            LOG.info(
                "Captured original status for %s: %s",
                self._work_item_id,
                self._original_status,
            )
        except RuntimeError:
            LOG.warning(
                "Could not fetch work item %s; defaulting original status to 'open'",
                self._work_item_id,
            )
            self._original_status = "open"

        # Set in_progress
        try:
            kwargs: dict = {"status": "in_progress", "runner": self._runner}
            if self._assignee is not None:
                kwargs["assignee"] = self._assignee
            self.update_status(self._work_item_id, **kwargs)
            self._did_set_in_progress = True
            LOG.info(
                "Set status=in_progress for %s (original: %s)",
                self._work_item_id,
                self._original_status,
            )
        except RuntimeError:
            LOG.error(
                "Failed to set in_progress for %s",
                self._work_item_id,
            )
            raise

        return self

    def __exit__(
        self,
        exc_type: type | None,  # noqa: PYI036
        exc_val: BaseException | None,
        exc_tb: object | None,  # noqa: PYI036
    ) -> bool | None:
        """On success: set completed (or restore original if restore_on_exit).

        On exception: restore original status.
        """
        if exc_type is not None:
            # Exception path — restore original status
            self._restore_original()
            # Do NOT suppress the exception
            return None

        if self._restore_on_exit:
            # Read-only mode — restore the original status on success too so
            # the item is never advanced to ``completed`` (which the wl CLI
            # rejects for idea/intake_complete/plan_complete stages).
            self._restore_original()
            LOG.info(
                "Restored original status=%s for %s (restore_on_exit)",
                self._original_status,
                self._work_item_id,
            )
            return None

        # Success path — transition to completed
        try:
            kwargs: dict = {"status": "completed", "runner": self._runner}
            if self._target_stage is not None:
                kwargs["stage"] = self._target_stage
            self.update_status(self._work_item_id, **kwargs)
            LOG.info(
                "Set status=completed for %s (stage=%s)",
                self._work_item_id,
                self._target_stage or "unchanged",
            )
        except RuntimeError:
            LOG.error(
                "Failed to set completed for %s",
                self._work_item_id,
            )
            raise

        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _restore_original(self) -> None:
        """Restore the original status (and clear assignee on failure)."""
        if not self._did_set_in_progress:
            return  # Nothing was changed, nothing to restore

        try:
            kwargs: dict = {"status": self._original_status, "runner": self._runner}
            # Clear assignee on failure to release the item
            if self._assignee is not None:
                kwargs["assignee"] = ""
            self.update_status(self._work_item_id, **kwargs)
            extra = ", assignee cleared" if self._assignee is not None else ""
            LOG.info(
                "Restored status=%s for %s (failure exit%s)",
                self._original_status,
                self._work_item_id,
                extra,
            )
        except RuntimeError:
            LOG.warning(
                "Failed to restore status=%s for %s (suppressed)",
                self._original_status,
                self._work_item_id,
            )
