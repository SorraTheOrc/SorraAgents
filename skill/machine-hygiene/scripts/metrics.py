#!/usr/bin/env python3
"""
Machine-hygiene metrics collection module.

Collects system performance metrics and classifies them against
configurable thresholds. Gracefully handles non-Linux platforms where
``/proc/pressure/`` is unavailable.

Usage
-----
    from metrics import collect_metrics

    metrics = collect_metrics()
    for name, info in metrics.items():
        print(f"{name}: {info['value']} ({info['status']})")
"""

import importlib.util
import os
from typing import Any

# ---------------------------------------------------------------------------
# Config loading (importlib by path — avoids shadowing a bare ``config``)
# ---------------------------------------------------------------------------
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_config_module():
    """Load the sibling ``scripts/config.py`` under a unique module name."""
    spec = importlib.util.spec_from_file_location(
        "machine_hygiene_config", os.path.join(_SKILL_DIR, "scripts", "config.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_config = _load_config_module()
load_config = _config.load_config


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

_STATUS_SYMBOLS = {
    "low": "🟢",
    "warning": "🟡",
    "critical": "🔴",
}


def classify_metric(value: float, thresholds: dict[str, float]) -> dict[str, Any]:
    """Classify a metric value against low/warning/critical thresholds.

    Args:
        value: The numeric metric value to classify.
        thresholds: Dict with keys ``low``, ``warning``, ``critical``.
            ``warning`` is the lower bound of the warning range and
            ``critical`` the lower bound of the critical range.

    Returns
    -------
    dict
        ``{value, status, symbol, description}``.
    """
    crit = thresholds.get("critical", float("inf"))
    warn = thresholds.get("warning", crit)

    if value >= crit:
        status = "critical"
    elif value >= warn:
        status = "warning"
    else:
        status = "low"

    return {
        "value": value,
        "status": status,
        "symbol": _STATUS_SYMBOLS[status],
        "description": "",  # filled in by caller
    }


def _multiplier_thresholds(base_cfg: dict[str, float], cores: int) -> dict[str, float]:
    """Derive absolute thresholds from core multipliers."""
    warn = cores * base_cfg.get("warning_multiplier", float("inf"))
    crit = cores * base_cfg.get("critical_multiplier", float("inf"))
    return {"low": 0, "warning": warn, "critical": crit}


# ---------------------------------------------------------------------------
# Raw data collectors
# ---------------------------------------------------------------------------

def _read_file_lines(path: str) -> list[str] | None:
    """Read a file and return lines, or None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readlines()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _read_proc_pressure(key: str) -> float | None:
    """Read a ``/proc/pressure/`` metric (cpu, memory, or io).

    Returns the avg10 stall percentage as a float, or None if unavailable.
    """
    lines = _read_file_lines(f"/proc/pressure/{key}")
    if lines is None:
        return None
    for line in lines:
        if line.startswith("avg"):
            parts = line.split()
            if len(parts) >= 4:
                return float(parts[1])
    return None


def _get_core_count() -> int:
    """Return the number of online CPU cores (never less than 1)."""
    try:
        return os.cpu_count() or 1
    except Exception:  # noqa: BLE001
        return 1


def _get_load_average() -> float:
    """Return the 1-minute load average, or 0.0 when unavailable."""
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (FileNotFoundError, ValueError, IndexError):
        return 0.0


def _get_memory_info() -> dict[str, float]:
    """Read ``/proc/meminfo`` and return memory statistics in MB."""
    result = {
        "total_mb": 0.0,
        "free_mb": 0.0,
        "available_mb": 0.0,
        "buffers_mb": 0.0,
        "cached_mb": 0.0,
        "swap_total_mb": 0.0,
        "swap_free_mb": 0.0,
    }
    lines = _read_file_lines("/proc/meminfo")
    if lines is None:
        return result

    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].rstrip(":")
        try:
            mb = int(parts[1]) / 1024.0
        except ValueError:
            continue
        mapping = {
            "MemTotal": "total_mb",
            "MemFree": "free_mb",
            "MemAvailable": "available_mb",
            "Buffers": "buffers_mb",
            "Cached": "cached_mb",
            "SwapTotal": "swap_total_mb",
            "SwapFree": "swap_free_mb",
        }
        if name in mapping:
            result[mapping[name]] = mb
    return result


def _get_process_counts() -> tuple[int, int]:
    """Count total processes and total threads.

    Returns ``(process_count, thread_count)``.
    """
    proc_count = 0
    thread_count = 0
    try:
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                proc_count += 1
                status_lines = _read_file_lines(f"/proc/{entry}/status")
                if status_lines:
                    for line in status_lines:
                        if line.startswith("Threads:"):
                            try:
                                thread_count += int(line.split()[1])
                            except (IndexError, ValueError):
                                pass
    except (PermissionError, OSError):
        pass
    return proc_count, thread_count


def _get_rss_top10() -> list[dict[str, Any]]:
    """Aggregate per-process RSS by **process name**, top 10 by total RSS.

    Each returned dict has ``name``, ``total_rss_bytes``, ``process_count``
    and ``pids`` (a list of ``(pid, rss_bytes)`` tuples).
    """
    aggregates: dict[str, dict[str, Any]] = {}

    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            status_lines = _read_file_lines(f"/proc/{pid}/status")
            if status_lines is None:
                continue
            proc_name = ""
            rss_kb = 0
            for line in status_lines:
                if line.startswith("Name:"):
                    proc_name = line.split(None, 1)[1].strip()
                elif line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        rss_kb = int(parts[1])
            if not proc_name:
                continue

            rss_bytes = rss_kb * 1024
            agg = aggregates.setdefault(
                proc_name,
                {"total_rss_bytes": 0, "process_count": 0, "pids": []},
            )
            agg["total_rss_bytes"] += rss_bytes
            agg["process_count"] += 1
            agg["pids"].append((pid, rss_bytes))
    except (PermissionError, OSError):
        pass

    sorted_items = sorted(
        aggregates.items(),
        key=lambda item: item[1]["total_rss_bytes"],
        reverse=True,
    )[:10]

    return [
        {
            "name": name,
            "total_rss_bytes": info["total_rss_bytes"],
            "process_count": info["process_count"],
            "pids": info["pids"],
        }
        for name, info in sorted_items
    ]


def _detect_stale_devices() -> dict[str, Any]:
    """Detect zombie processes and stale lock files.

    Returns ``{zombie_count, zombie_details, lock_files}``.
    """
    zombies: list[tuple[int, str]] = []
    lock_files: list[str] = []

    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            status_lines = _read_file_lines(f"/proc/{entry}/status")
            if status_lines is None:
                continue
            proc_name = ""
            state = ""
            for line in status_lines:
                if line.startswith("Name:"):
                    proc_name = line.split(None, 1)[1].strip()
                elif line.startswith("State:"):
                    state = line.split(None, 1)[1].strip()
            if state.startswith("Z"):
                zombies.append((int(entry), proc_name))
    except (PermissionError, OSError):
        pass

    lock_dir = "/var/lock"
    if os.path.isdir(lock_dir):
        try:
            for name in os.listdir(lock_dir):
                full_path = os.path.join(lock_dir, name)
                if os.path.isfile(full_path):
                    lock_files.append(full_path)
        except PermissionError:
            pass

    return {
        "zombie_count": len(zombies),
        "zombie_details": zombies,
        "lock_files": lock_files,
    }


def _get_runnable_count() -> int:
    """Count processes in R (runnable) or D (uninterruptible sleep) state."""
    count = 0
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            status_lines = _read_file_lines(f"/proc/{entry}/status")
            if status_lines is None:
                continue
            for line in status_lines:
                if line.startswith("State:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1][0] in ("R", "D"):
                        count += 1
                    break
    except (PermissionError, OSError):
        pass
    return count


# ---------------------------------------------------------------------------
# Main collection function
# ---------------------------------------------------------------------------

def collect_metrics() -> dict[str, Any]:
    """Collect and classify all system performance metrics.

    Returns
    -------
    dict
        ``metric_name -> {value, status, description, symbol}``.
    """
    cfg = load_config()
    cores = _get_core_count()
    metrics: dict[str, Any] = {}

    load_thresholds = _multiplier_thresholds(cfg["load_average"], cores)
    runnable_thresholds = _multiplier_thresholds(cfg["runnable_processes"], cores)

    # --- Load average ---
    load = _get_load_average()
    metrics["load_average"] = {
        "value": round(load, 2),
        "status": classify_metric(load, load_thresholds)["status"],
        "description": (
            f"{load} (cores: {cores}, warn: {load_thresholds['warning']:.1f}, "
            f"crit: {load_thresholds['critical']:.1f})"
        ),
    }

    # --- Pressure stall metrics ---
    # cpu_pressure/memory_usage/io_pressure hold the percentage thresholds
    # applied to the corresponding /proc/pressure avg10 stall percentage.
    pressure_specs = (
        ("cpu", "cpu_pressure", "cpu_pressure", "CPU stall pressure"),
        ("memory", "memory_pressure", "memory_usage", "Memory stall pressure"),
        ("io", "io_pressure", "io_pressure", "I/O stall pressure"),
    )
    for proc_key, metric_key, cfg_key, label in pressure_specs:
        value = _read_proc_pressure(proc_key)
        if value is not None:
            metrics[metric_key] = {
                "value": round(value, 2),
                "status": classify_metric(value, cfg[cfg_key])["status"],
                "description": label,
            }
        else:
            metrics[metric_key] = {
                "value": None,
                "status": "warning",
                "description": (
                    f"/proc/pressure/{proc_key} unavailable "
                    "(non-Linux or kernel missing pressure stall info)"
                ),
            }

    # --- Memory usage ---
    mem_info = _get_memory_info()
    used_pct = 0.0
    if mem_info["total_mb"] > 0:
        used_pct = (
            (mem_info["total_mb"] - mem_info["available_mb"]) / mem_info["total_mb"]
        ) * 100
    metrics["memory_usage"] = {
        "value": round(used_pct, 1),
        "status": classify_metric(used_pct, cfg["memory_usage"])["status"],
        "description": (
            f"Used: {round(mem_info['total_mb'] - mem_info['available_mb'], 0)} MB / "
            f"{round(mem_info['total_mb'], 0)} MB"
        ),
    }

    # --- Swap usage ---
    swap_pct = 0.0
    if mem_info["swap_total_mb"] > 0:
        swap_pct = (
            (mem_info["swap_total_mb"] - mem_info["swap_free_mb"])
            / mem_info["swap_total_mb"]
        ) * 100
    metrics["swap_usage"] = {
        "value": round(swap_pct, 1),
        "status": classify_metric(swap_pct, cfg["swap_usage"])["status"],
        "description": (
            f"Swap used: {round(mem_info['swap_total_mb'] - mem_info['swap_free_mb'], 0)} MB / "
            f"{round(mem_info['swap_total_mb'], 0)} MB"
        ),
    }

    # --- Process / thread counts ---
    proc_count, thread_count = _get_process_counts()
    metrics["process_count"] = {
        "value": proc_count,
        "status": classify_metric(proc_count, runnable_thresholds)["status"],
        "description": (
            f"Total processes (threads: {thread_count}; "
            f"warn: {runnable_thresholds['warning']:.0f}, "
            f"crit: {runnable_thresholds['critical']:.0f})"
        ),
    }
    metrics["thread_count"] = {
        "value": thread_count,
        "status": classify_metric(thread_count, runnable_thresholds)["status"],
        "description": "Total threads across all processes",
    }

    # --- Runnable processes ---
    runnable = _get_runnable_count()
    metrics["runnable_processes"] = {
        "value": runnable,
        "status": classify_metric(runnable, runnable_thresholds)["status"],
        "description": (
            f"R+D state processes (cores: {cores}, "
            f"warn: {runnable_thresholds['warning']:.0f}, "
            f"crit: {runnable_thresholds['critical']:.0f})"
        ),
    }

    # --- RSS top 10 ---
    rss_top10 = _get_rss_top10()
    total_rss = sum(item["total_rss_bytes"] for item in rss_top10)
    metrics["rss_top10"] = {
        "value": total_rss,
        "status": "low",
        "description": (
            f"Total RSS of top-10: {total_rss / (1024 ** 2):.1f} MB"
        ),
    }
    metrics["rss_breakdown"] = {
        "value": rss_top10,
        "status": "low",
        "description": "Top 10 processes by RSS, grouped by name",
    }

    # --- Stale devices ---
    stale = _detect_stale_devices()
    metrics["zombie_processes"] = {
        "value": stale["zombie_count"],
        "status": classify_metric(
            stale["zombie_count"], cfg["zombie_processes"]
        )["status"],
        "description": (
            f"Zombie processes: {stale['zombie_count']}. "
            + (
                f"Details: {stale['zombie_details']}"
                if stale["zombie_details"]
                else "None detected."
            )
        ),
    }
    metrics["stale_lock_files"] = {
        "value": len(stale["lock_files"]),
        "status": "low",
        "description": f"Lock files in /var/lock: {len(stale['lock_files'])}",
    }

    for info in metrics.values():
        info["symbol"] = _STATUS_SYMBOLS.get(info["status"], "")

    return metrics
