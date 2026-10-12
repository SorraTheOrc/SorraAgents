#!/usr/bin/env python3
"""
Machine-hygiene pressure analysis and remediation proposal.

Turns collected metrics into a ranked list of remediation actions. For each
pressure source it identifies the contributing processes and their attributable
ancestry (parent process, session, systemd unit, uid), then proposes concrete
actions (kill, renice, prune, pace) with expected impact and a risk assessment.

Read-only: this module never executes anything. Actions are proposals for the
approval gate (Feature 4).

Usage
-----
    from metrics import collect_metrics
    from pressure_analysis import analyze_pressures, propose_remediations, rank_actions

    pressures = analyze_pressures(collect_metrics())
    actions = rank_actions(propose_remediations(pressures))
"""

import os
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# RSS above which an ephemeral process is considered a runaway candidate.
RUNAWAY_RSS_BYTES = 1024 ** 3  # 1 GiB
# How many top RSS contributors to attach to each pressure.
TOP_CONTRIBUTORS = 3
# Max ancestry depth to walk.
MAX_ANCESTRY_DEPTH = 8

# Process names that are typically safe to terminate when they are runaway.
EPHEMERAL_TOOL_NAMES = {
    "grep", "egrep", "fgrep", "rg", "ripgrep", "find", "fd", "sed", "awk",
    "sort", "du", "tar", "gzip", "gunzip", "cat", "tee", "xargs",
}

# Metrics that, when elevated, constitute an actionable pressure.
_PRESSURE_METRICS = (
    "load_average",
    "cpu_pressure",
    "memory_pressure",
    "io_pressure",
    "memory_usage",
    "swap_usage",
    "runnable_processes",
    "process_count",
    "thread_count",
    "zombie_processes",
)


# ---------------------------------------------------------------------------
# /proc ancestry
# ---------------------------------------------------------------------------

def _read(relpath: str, proc_root: str) -> str | None:
    try:
        with open(os.path.join(proc_root, relpath), "r", encoding="utf-8",
                  errors="replace") as fh:
            return fh.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _parse_status(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _session_id(pid: int, proc_root: str) -> int | None:
    """Return the process session id from ``/proc/<pid>/stat`` (field 6)."""
    text = _read(f"{pid}/stat", proc_root)
    if not text:
        return None
    # comm may contain spaces/parens — parse after the final ')'.
    close = text.rfind(")")
    if close == -1:
        return None
    fields = text[close + 2:].split()
    # fields[0] = state, [1] = ppid, [2] = pgrp, [3] = session
    if len(fields) >= 4:
        try:
            return int(fields[3])
        except ValueError:
            return None
    return None


def _systemd_unit(pid: int, proc_root: str) -> str | None:
    """Extract a systemd unit from ``/proc/<pid>/cgroup`` (best effort)."""
    text = _read(f"{pid}/cgroup", proc_root)
    if not text:
        return None
    for line in text.splitlines():
        path = line.split(":", 2)[-1].strip()
        for part in path.split("/"):
            if part.endswith((".service", ".scope")):
                return part
    return None


def _cmdline(pid: int, proc_root: str) -> str:
    text = _read(f"{pid}/cmdline", proc_root)
    if not text:
        return ""
    return " ".join(text.split("\x00")).strip()


def find_ancestry(pid: int, proc_root: str = "/proc") -> dict[str, Any]:
    """Gather ancestry information for *pid*.

    Returns a dict with ``pid``, ``name``, ``ppid``, ``uid``, ``session``,
    ``systemd_unit``, ``cmdline`` and ``ancestors`` (a list of
    ``{pid, name}`` from the immediate parent upwards). Missing data degrades
    to ``None``/empty values rather than raising.
    """
    status_text = _read(f"{pid}/status", proc_root)
    if status_text is None:
        return {
            "pid": pid,
            "name": None,
            "ppid": None,
            "uid": None,
            "session": None,
            "systemd_unit": None,
            "cmdline": "",
            "ancestors": [],
            "available": False,
        }

    status = _parse_status(status_text)
    name = status.get("Name")
    try:
        ppid: int | None = int(status.get("PPid", ""))
    except ValueError:
        ppid = None
    try:
        uid: int | None = int(status.get("Uid", "").split()[0])
    except (ValueError, IndexError):
        uid = None

    ancestors: list[dict[str, Any]] = []
    current = ppid
    seen = {pid}
    depth = 0
    while current and current not in seen and depth < MAX_ANCESTRY_DEPTH:
        seen.add(current)
        parent_text = _read(f"{current}/status", proc_root)
        if parent_text is None:
            break
        parent_status = _parse_status(parent_text)
        ancestors.append({"pid": current, "name": parent_status.get("Name")})
        try:
            current = int(parent_status.get("PPid", ""))
        except ValueError:
            break
        depth += 1

    return {
        "pid": pid,
        "name": name,
        "ppid": ppid,
        "uid": uid,
        "session": _session_id(pid, proc_root),
        "systemd_unit": _systemd_unit(pid, proc_root),
        "cmdline": _cmdline(pid, proc_root),
        "ancestors": ancestors,
        "available": True,
    }


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------

def assess_risk(ancestry: dict[str, Any], terminating: bool = False) -> str:
    """Assess the risk of acting on a process: ``low``, ``medium`` or ``high``.

    Heuristics (deterministic, ancestry-based):

    - Unknown/unavailable ancestry with termination → ``high``.
    - Root-owned process → ``high`` (system-critical).
    - Interactive-systemd ancestors (``user@*.service``/``session-*.scope``)
      or a pi/agent ancestor → ``medium`` when terminating, else ``low``.
    - Everything else → ``low``.
    """
    if not ancestry.get("available", False):
        return "high" if terminating else "medium"

    if ancestry.get("uid") == 0:
        return "high"

    unit = ancestry.get("systemd_unit") or ""
    ancestor_names = {str(a.get("name") or "").lower() for a in ancestry.get("ancestors", [])}

    interactive_markers = ("session-", "user@", "gnome", "sshd", "tmux")
    is_interactive = any(marker in unit for marker in interactive_markers)
    is_agent_related = any(
        marker in name for name in ancestor_names for marker in ("pi", "node", "herder")
    )
    is_agent_related = is_agent_related or any(
        marker in (ancestry.get("name") or "").lower() for marker in ("node",)
    )

    if terminating:
        if is_interactive or is_agent_related:
            return "medium"
        return "low"

    if is_interactive and ancestry.get("uid") not in (None, os.getuid()):
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Pressure analysis
# ---------------------------------------------------------------------------

def analyze_pressures(
    metrics: dict[str, Any],
    ancestry_fn: Callable[[int], dict[str, Any]] = find_ancestry,
) -> list[dict[str, Any]]:
    """Identify the primary pressure sources from collected *metrics*.

    A pressure is any metric classified ``warning`` or ``critical``. RSS
    contributors are attached to each pressure and their ancestry resolved.

    Returns a list of pressure dicts:
    ``{metric, status, value, summary, contributors}``.
    """
    pressures: list[dict[str, Any]] = []

    for metric in _PRESSURE_METRICS:
        info = metrics.get(metric)
        if not info or info.get("status") not in ("warning", "critical"):
            continue
        pressures.append({
            "metric": metric,
            "status": info["status"],
            "value": info.get("value"),
            "summary": info.get("description", ""),
            "contributors": [],
        })

    contributors = _rss_contributors(metrics, ancestry_fn)
    for pressure in pressures:
        pressure["contributors"] = contributors

    return pressures


def _rss_contributors(
    metrics: dict[str, Any],
    ancestry_fn: Callable[[int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the top RSS contributors with ancestry attached."""
    breakdown = metrics.get("rss_breakdown", {}).get("value") or []
    contributors: list[dict[str, Any]] = []
    for item in breakdown[:TOP_CONTRIBUTORS]:
        pids = [pid for pid, _ in item.get("pids", [])]
        top_pid = item["pids"][0][0] if item.get("pids") else None
        ancestry: dict[str, Any] = {}
        if top_pid is not None:
            try:
                ancestry = ancestry_fn(top_pid)
            except Exception:  # noqa: BLE001 — best-effort enrichment
                ancestry = {}
        contributors.append({
            "name": item.get("name"),
            "total_rss_bytes": item.get("total_rss_bytes", 0),
            "process_count": item.get("process_count", 0),
            "pids": pids,
            "ancestry": ancestry,
        })
    return contributors


# ---------------------------------------------------------------------------
# Remediation proposal
# ---------------------------------------------------------------------------

_IMPACT_SCORE = {"low": 1, "medium": 2, "high": 3}
_RISK_SCORE = {"low": 1, "medium": 2, "high": 3}


def _action(
    action: str,
    target: dict[str, Any],
    reason: str,
    expected_impact: str,
    impact: str,
    risk: str,
) -> dict[str, Any]:
    return {
        "action": action,
        "target": target,
        "reason": reason,
        "expected_impact": expected_impact,
        "impact": impact,
        "impact_score": _IMPACT_SCORE[impact],
        "risk": risk,
        "risk_score": _RISK_SCORE[risk],
    }


def propose_remediations(pressures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Propose remediation actions for *pressures*.

    Emits, per pressure: ``kill`` for runaway ephemeral tools, ``renice`` for
    other large contributors, ``prune`` for zombies/stale locks and ``pace``
    for sustained CPU/load pressure.
    """
    actions: list[dict[str, Any]] = []

    for pressure in pressures:
        metric = pressure.get("metric")
        status = pressure.get("status")

        # Runaway / large memory contributors → kill or renice.
        for contributor in pressure.get("contributors", []):
            name = (contributor.get("name") or "").lower()
            rss = contributor.get("total_rss_bytes", 0)
            if rss <= 0:
                continue
            ancestry = contributor.get("ancestry") or {}
            target = {
                "name": contributor.get("name"),
                "pids": contributor.get("pids", []),
            }
            is_ephemeral = name in EPHEMERAL_TOOL_NAMES
            if is_ephemeral and rss >= RUNAWAY_RSS_BYTES:
                risk = assess_risk(ancestry, terminating=True)
                actions.append(_action(
                    "kill",
                    target,
                    f"Runaway {contributor.get('name')} holding {rss / (1024**2):.0f} MB RSS",
                    f"Free {rss / (1024**2):.0f} MB RSS and reduce pressure",
                    "high",
                    risk,
                ))
            elif metric in ("memory_usage", "memory_pressure", "swap_usage"):
                risk = assess_risk(ancestry, terminating=False)
                actions.append(_action(
                    "renice",
                    target,
                    f"{contributor.get('name')} consumes {rss / (1024**2):.0f} MB RSS",
                    "Lower scheduling priority; reduce memory churn",
                    "medium",
                    risk,
                ))

        # Sustained CPU/load pressure → pace concurrent work.
        if metric in ("cpu_pressure", "load_average", "runnable_processes") and status == "critical":
            actions.append(_action(
                "pace",
                {"scope": "concurrent-sessions"},
                "Sustained CPU/load saturation with many runnable processes",
                "Reduce concurrent test/agent load; free CPU for active work",
                "medium",
                "low",
            ))

        # Zombies → prune (reap via parent).
        if metric == "zombie_processes" and (pressure.get("value") or 0) > 0:
            actions.append(_action(
                "prune",
                {"scope": "zombie-processes"},
                f"{pressure.get('value')} zombie process(es) accumulating",
                "Signal parent processes to reap defunct children",
                "low",
                "medium",
            ))

    # Stale lock files are reported by metrics but not a pressure metric; add
    # a prune proposal when present.
    return actions


def propose_remediations_from_metrics(
    metrics: dict[str, Any],
    pressures: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper: propose actions from *metrics* (and pressures).

    Adds a prune action for stale lock files, which are not modelled as a
    pressure metric.
    """
    pressures = pressures if pressures is not None else analyze_pressures(metrics)
    actions = propose_remediations(pressures)

    lock_info = metrics.get("stale_lock_files") or {}
    lock_count = lock_info.get("value") or 0
    if lock_count:
        actions.append(_action(
            "prune",
            {"scope": "stale-lock-files", "count": lock_count},
            f"{lock_count} stale lock file(s) present",
            "Remove stale locks to unblock waiting processes",
            "low",
            "medium",
        ))
    return actions


def rank_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank *actions* by impact-to-risk ratio (highest first).

    Ties are broken by impact score, then by action name for determinism.
    """
    def key(action: dict[str, Any]) -> Any:
        ratio = action["impact_score"] / max(action["risk_score"], 1)
        return (-ratio, -action["impact_score"], action["action"], str(action.get("target")))

    return sorted(actions, key=key)
