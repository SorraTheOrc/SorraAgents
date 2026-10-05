#!/usr/bin/env python3
"""Sync the Worklog store from git before worklog-reading skills run.

Skills such as ``refactor`` and ``standup`` read and (for refactor) create
work items. If their local SQLite store is stale they can file duplicate
``REFACTOR`` work items or report a queue the operator never sees. Running
``wl sync`` first pulls remote changes into the local store so analysis and
reporting reflect the shared state.

The sync is deliberately **non-fatal** and **lock-aware**:

* ``wl sync --if-idle`` is used so overlapping/concurrent invocations skip
  (exit 0, JSON ``skipped: true``) instead of blocking on the sync lock and
  piling up.
* Any failure — offline, unborn HEAD, author gate, lock contention, or a
  missing ``.worklog`` context — is logged as a warning and returned as a
  status dict. The calling skill continues with whatever local data it has,
  preserving its existing exit-code semantics.
* ``no_sync=True`` (the ``--no-sync`` escape hatch) short-circuits without
  invoking ``wl`` at all, so tests and deterministic/offline runs never
  perform a real network sync.
* The invocation is bounded by :data:`SYNC_TIMEOUT` seconds so a hung sync
  can never hang an agent session.

The helper routes the ``wl`` invocation through
:func:`shared.status_lifecycle.run_wl`, which resolves and injects the correct
``--worklog-dir`` (explicit > prefix-to-sibling > cwd chain) and surfaces
detailed errors.

Usage::

    from shared.worklog_sync import sync_worklog

    sync_worklog(worklog_dir=worklog_dir, no_sync=args.no_sync)
"""  # noqa: EXE001

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from shared.status_lifecycle import (
    Runner,
    resolve_worklog_dir,
    run_wl,
    worklog_dir_flag,
)

LOG = logging.getLogger("skill.shared.worklog_sync")

#: Upper bound (seconds) on a single ``wl sync`` invocation. The sync is a
#: background freshness step, never a blocking gate — if it runs longer the
#: skill proceeds with local data.
SYNC_TIMEOUT = 60


def _bounded_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Default runner: run ``wl`` with a hard timeout so it cannot hang.

    Args:
        cmd: The fully-formed command (including any injected
            ``--worklog-dir`` flags).

    Returns:
        The completed process.

    Raises:
        subprocess.TimeoutExpired: If the command exceeds :data:`SYNC_TIMEOUT`.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=SYNC_TIMEOUT,
        check=False,
    )


def _has_worklog_context() -> bool:
    """True when an initialized ``.worklog`` store is resolvable from the cwd.

    Mirrors :func:`shared.status_lifecycle.worklog_dir_flag` (cwd, git root,
    nearest initialized ancestor) without invoking ``wl``.
    """
    if worklog_dir_flag():
        return True
    cwd_worklog = Path.cwd() / ".worklog"
    return (cwd_worklog / "initialized").is_file() or (
        cwd_worklog / "worklog.db"
    ).is_file()


def sync_worklog(
    worklog_dir: str | None = None,
    runner: Runner | None = None,
    no_sync: bool = False,
    work_item_id: str | None = None,
) -> dict[str, Any]:
    """Sync the Worklog store from git before the caller reads/creates items.

    Args:
        worklog_dir: Optional explicit ``.worklog`` directory to target
            (e.g. standup's resolved ``--worklog-dir``). When omitted, the
            shared prefix-to-sibling/cwd-chain resolution is used.
        runner: Optional injectable command runner (for tests). When omitted
            a bounded subprocess runner is used.
        no_sync: When ``True`` (``--no-sync``), skip the sync entirely and
            return immediately without invoking ``wl``.
        work_item_id: Optional work-item id whose owning store should be
            synced. Used when the caller operates on a work item that may
            live in a sibling project's store (prefix resolution). Ignored
            when *worklog_dir* is given.

    Returns:
        A status dict — never raises:

        * ``{"status": "synced"}`` — the sync ran to completion.
        * ``{"status": "skipped", "reason": ...}`` — ``--no-sync``, a
          contended lock (another sync in progress), or no worklog context.
        * ``{"status": "failed", "error": ...}`` — the sync failed; the
          caller should continue with local data.
    """
    if no_sync:
        LOG.info("wl sync skipped (--no-sync)")
        return {"status": "skipped", "reason": "no_sync"}

    if worklog_dir is None and work_item_id:
        resolved = resolve_worklog_dir(work_item_id)
        if resolved is not None:
            worklog_dir = str(resolved)

    if worklog_dir is None and not _has_worklog_context():
        LOG.info("wl sync skipped: no worklog context resolvable")
        return {"status": "skipped", "reason": "no worklog context"}

    cmd = ["wl", "sync", "--if-idle", "--json"]
    try:
        result = run_wl(
            cmd,
            runner=runner or _bounded_runner,
            explicit_dir=worklog_dir,
        )
    except Exception as exc:  # noqa: BLE001 - sync must never abort the skill
        LOG.warning("wl sync failed; continuing with local worklog data: %s", exc)
        return {"status": "failed", "error": str(exc)}

    if isinstance(result, dict) and result.get("skipped"):
        reason = str(result.get("reason") or "another sync is already in progress")
        LOG.info("wl sync skipped: %s", reason)
        return {"status": "skipped", "reason": reason}

    LOG.info("wl sync completed")
    return {"status": "synced"}
