#!/usr/bin/env python3
"""
Machine-hygiene action execution.

Executes **only** the explicitly-approved subset of remediation actions. Every
kill is preceded by an ancestry safety check (uid match, non system-critical,
parent/session context available). All functions are injectable
(``kill_fn``/``renice_fn``/``ancestry_fn``) so the safety logic is testable
without touching real processes.
"""

import os
import signal as _signal
from collections.abc import Callable
from typing import Any

# Processes that must never be killed by this skill.
SYSTEM_CRITICAL_NAMES = {
    "systemd", "init", "kthreadd", "ksoftirqd", "migration", "watchdog",
    "systemd-journald", "systemd-logind", "systemd-udevd", "dbus-daemon",
    "dbus-broker", "sshd", "launchd", "kernel", "rcu_sched",
}

# PID 1 and 2 are always off-limits.
PROTECTED_PIDS = {1, 2}


def _load_ancestry_fn() -> Callable[[int], dict[str, Any]]:
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pressure_analysis.py")
    spec = importlib.util.spec_from_file_location("machine_hygiene_pressure_analysis", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.find_ancestry


def _default_renice(pid: int, increment: int) -> None:
    current = os.getpriority(os.PRIO_PROCESS, pid)
    os.setpriority(os.PRIO_PROCESS, pid, current + increment)


# ---------------------------------------------------------------------------
# Safety verification (AC4)
# ---------------------------------------------------------------------------

def verify_kill_safety(
    pid: int,
    ancestry: dict[str, Any] | None = None,
    operator_uid: int | None = None,
) -> dict[str, Any]:
    """Verify it is safe to kill *pid*.

    Checks: uid matches the operator, not a system-critical process, and
    ancestry/session context is available. Returns ``{safe, reasons}``.
    """
    reasons: list[str] = []
    if operator_uid is None:
        operator_uid = os.getuid()

    if pid in PROTECTED_PIDS:
        reasons.append(f"pid {pid} is protected (init/kernel)")

    if not ancestry:
        reasons.append("ancestry unavailable")
    else:
        if not ancestry.get("available", False):
            reasons.append("ancestry unavailable")
        uid = ancestry.get("uid")
        if uid is not None and uid != operator_uid:
            reasons.append(f"uid {uid} does not match operator uid {operator_uid}")
        name = str(ancestry.get("name") or "").lower()
        if name in SYSTEM_CRITICAL_NAMES:
            reasons.append(f"system-critical process '{name}'")
        if ancestry.get("session") is None:
            reasons.append("parent session context unavailable")

    return {"safe": not reasons, "reasons": reasons}


# ---------------------------------------------------------------------------
# Individual actions (AC3)
# ---------------------------------------------------------------------------

def kill_process(
    pid: int,
    ancestry: dict[str, Any] | None = None,
    *,
    operator_uid: int | None = None,
    sig: int = _signal.SIGTERM,
    dry_run: bool = True,
    kill_fn: Callable[[int, int], None] = os.kill,
) -> dict[str, Any]:
    """Kill *pid* after a safety check. Never kills when unsafe."""
    safety = verify_kill_safety(pid, ancestry, operator_uid)
    if not safety["safe"]:
        return {
            "pid": pid, "action": "kill", "status": "skipped",
            "detail": "; ".join(safety["reasons"]),
        }
    if dry_run:
        return {"pid": pid, "action": "kill", "status": "dry-run",
                "detail": f"would send signal {sig}"}
    try:
        kill_fn(pid, sig)
        return {"pid": pid, "action": "kill", "status": "executed",
                "detail": f"sent signal {sig}"}
    except ProcessLookupError:
        return {"pid": pid, "action": "kill", "status": "failed",
                "detail": "no such process"}
    except PermissionError:
        return {"pid": pid, "action": "kill", "status": "failed",
                "detail": "permission denied"}


def renice_process(
    pid: int,
    increment: int = 10,
    *,
    dry_run: bool = True,
    renice_fn: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Lower the scheduling priority of *pid* (non-destructive)."""
    renice_fn = renice_fn or _default_renice
    if dry_run:
        return {"pid": pid, "action": "renice", "status": "dry-run",
                "detail": f"would renice +{increment}"}
    try:
        renice_fn(pid, increment)
        return {"pid": pid, "action": "renice", "status": "executed",
                "detail": f"reniced +{increment}"}
    except ProcessLookupError:
        return {"pid": pid, "action": "renice", "status": "failed",
                "detail": "no such process"}
    except (PermissionError, OSError) as exc:
        return {"pid": pid, "action": "renice", "status": "failed",
                "detail": str(exc)}


def prune_stale_sessions(
    sessions: list[Any] | None = None,
    *,
    dry_run: bool = True,
    prune_fn: Callable[[list[Any]], None] | None = None,
) -> dict[str, Any]:
    """Prune stale sessions (delegates to *prune_fn* when supplied)."""
    sessions = sessions or []
    if not sessions:
        return {"action": "prune", "status": "skipped",
                "detail": "no stale sessions identified", "targets": []}
    if dry_run:
        return {"action": "prune", "status": "dry-run",
                "detail": f"would prune {len(sessions)} stale session(s)",
                "targets": list(sessions)}
    if prune_fn is None:
        return {"action": "prune", "status": "skipped",
                "detail": "no session pruner configured", "targets": list(sessions)}
    prune_fn(sessions)
    return {"action": "prune", "status": "executed",
            "detail": f"pruned {len(sessions)} session(s)", "targets": list(sessions)}


def pace_concurrent_work(
    scope: str = "concurrent-sessions",
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Recommend pacing concurrent work (advisory, never destructive)."""
    return {
        "action": "pace", "status": "reported",
        "detail": f"recommend reducing concurrency for {scope}",
        "target": {"scope": scope},
    }


# ---------------------------------------------------------------------------
# Batch execution (AC1/AC2)
# ---------------------------------------------------------------------------

def _dispatch(
    action: dict[str, Any],
    *,
    dry_run: bool,
    operator_uid: int | None,
    kill_fn: Callable[[int, int], None],
    renice_fn: Callable[[int, int], None] | None,
    prune_fn: Callable[[list[Any]], None] | None,
    ancestry_fn: Callable[[int], dict[str, Any]],
) -> list[dict[str, Any]]:
    kind = action.get("action")
    target = action.get("target", {}) or {}
    results: list[dict[str, Any]] = []

    if kind == "kill":
        pids = target.get("pids") or ([target["pid"]] if "pid" in target else [])
        for pid in pids:
            ancestry = ancestry_fn(pid)
            results.append(kill_process(
                pid, ancestry, operator_uid=operator_uid,
                dry_run=dry_run, kill_fn=kill_fn,
            ))
        if not pids:
            results.append({"action": "kill", "status": "skipped",
                            "detail": "no target pid"})
    elif kind == "renice":
        pids = target.get("pids") or ([target["pid"]] if "pid" in target else [])
        for pid in pids:
            results.append(renice_process(
                pid, dry_run=dry_run, renice_fn=renice_fn,
            ))
    elif kind == "prune":
        if target.get("scope") == "stale-lock-files":
            results.append({"action": "prune", "status": "skipped",
                            "detail": "stale lock removal is operator-managed"})
        else:
            results.append(prune_stale_sessions(
                target.get("sessions"), dry_run=dry_run, prune_fn=prune_fn,
            ))
    elif kind == "pace":
        results.append(pace_concurrent_work(
            target.get("scope", "concurrent-sessions"), dry_run=dry_run,
        ))
    else:
        results.append({"action": kind, "status": "skipped",
                        "detail": "unknown action type"})
    return results


def execute_actions(
    approved_actions: list[dict[str, Any]],
    declined_actions: list[dict[str, Any]] | None = None,
    *,
    dry_run: bool = True,
    operator_uid: int | None = None,
    kill_fn: Callable[[int, int], None] = os.kill,
    renice_fn: Callable[[int, int], None] | None = None,
    prune_fn: Callable[[list[Any]], None] | None = None,
    ancestry_fn: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute only *approved_actions* and report the outcome.

    ``declined_actions`` are recorded but never executed (AC2).
    """
    ancestry_fn = ancestry_fn or _load_ancestry_fn()
    declined_actions = declined_actions or []

    results: list[dict[str, Any]] = []
    for action in approved_actions:
        results.extend(_dispatch(
            action, dry_run=dry_run, operator_uid=operator_uid,
            kill_fn=kill_fn, renice_fn=renice_fn, prune_fn=prune_fn,
            ancestry_fn=ancestry_fn,
        ))

    executed = [r for r in results if r["status"] == "executed"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]
    dry = [r for r in results if r["status"] == "dry-run"]

    return {
        "approved_count": len(approved_actions),
        "declined_count": len(declined_actions),
        "declined": declined_actions,
        "results": results,
        "executed": executed,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry,
    }
