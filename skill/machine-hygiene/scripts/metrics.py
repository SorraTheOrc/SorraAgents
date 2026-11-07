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
import time
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


def _classify_swap_usage(
    swap_pct: float,
    memory_used_pct: float,
    memory_pressure: float | None,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Classify swap occupancy gated on *current* memory pressure.

    Swap occupancy is a historical figure: pages accumulate over days of
    paging and stay in swap after pressure has passed. It is therefore only
    allowed to warn when memory is under current pressure — either
    ``memory_usage`` (capacity) or the PSI ``memory_pressure`` ``full`` line
    (stall percentage) sits at/above its own warning threshold. When PSI
    memory data is unavailable the capacity figure is the sole signal
    (graceful degradation).

    Returns ``{status, gated, escalated}``:

    - ``gated`` — swap was downgraded to ``low`` because memory is not under
      pressure (historical accumulation);
    - ``escalated`` — ``memory_usage`` is critical, so a warning-level swap
      reading is escalated to ``critical``.
    """
    swap_level = classify_metric(swap_pct, cfg["swap_usage"])["status"]
    mem_level = classify_metric(memory_used_pct, cfg["memory_usage"])["status"]
    if memory_pressure is None:
        pressure_level = None
    else:
        pressure_level = classify_metric(
            memory_pressure, cfg["memory_pressure"]
        )["status"]

    memory_under_pressure = mem_level != "low" or (
        pressure_level is not None and pressure_level != "low"
    )
    if not memory_under_pressure:
        # Swap occupancy without current pressure is not actionable.
        return {"status": "low", "gated": True, "escalated": False}
    if swap_level == "low":
        return {"status": "low", "gated": False, "escalated": False}
    if mem_level == "critical":
        return {
            "status": "critical",
            "gated": False,
            "escalated": swap_level != "critical",
        }
    return {"status": swap_level, "gated": False, "escalated": False}


# ---------------------------------------------------------------------------
# Raw data collectors
# ---------------------------------------------------------------------------

def _read_file_lines(path: str) -> list[str] | None:
    """Read a file and return lines, or None on failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readlines()
    except (FileNotFoundError, PermissionError, OSError):
        return None


#: PSI line selectors — ``some`` (at least one task stalled) and ``full``
#: (all tasks stalled). The value is the line prefix in /proc/pressure/*.
_PSI_LINES = {"some": "some ", "full": "full "}

#: Human-readable basis for each PSI line, used in metric descriptions.
_PSI_BASIS = {
    "some": "% of time any task stalled",
    "full": "% of time all tasks stalled",
}


def _read_proc_pressure(key: str, which: str = "some") -> float | None:
    """Read a PSI stall percentage from ``/proc/pressure/<key>``.

    Pressure stall information (PSI) files expose up to two lines:

    * ``some`` — at least one task was stalled on the resource;
    * ``full`` — *all* tasks were stalled (the CPU file has no ``full``
      line, because a CPU is never entirely idle-stalled for every task).

    ``which`` selects the line and ``avg10`` (the 10-second rolling
    percentage) is returned. There is **no fallback** between the lines: a
    requested ``full`` line the kernel does not expose returns ``None``
    rather than silently reporting the less severe ``some`` value, because
    mixing the two would misstate severity.

    Args:
        key: The pressure resource — ``cpu``, ``memory`` or ``io``.
        which: Which PSI line to read — ``some`` (default) or ``full``.

    Returns
    -------
    float | None
        The ``avg10`` stall percentage, or ``None`` when the file is
        missing, empty or unparsable, or the requested line is absent.

    Raises
    ------
    ValueError
        If ``which`` is not ``some`` or ``full`` (a programming error).
    """
    if which not in _PSI_LINES:
        raise ValueError(
            f"which must be one of {sorted(_PSI_LINES)}, got {which!r}"
        )

    lines = _read_file_lines(f"/proc/pressure/{key}")
    if not lines:
        return None

    prefix = _PSI_LINES[which]
    for line in lines:
        if not line.startswith(prefix):
            continue
        for token in line.split():
            if not token.startswith("avg10="):
                continue
            try:
                return float(token.split("=", 1)[1])
            except ValueError:
                return None
        # The requested line is present but carries no avg10 token.
        return None
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
        with open("/proc/loadavg", encoding="utf-8") as fh:
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


#: Lock files created by the ALSA kernel driver at boot. They are permanently
#: present, root-owned and never stale — they must never be reported or pruned.
KERNEL_LOCK_FILES = frozenset({
    "asound.state.lock",
    "card0.lock",
    "card1.lock",
    "card2.lock",
})

#: Minimum age before an unheld, non-root, non-kernel lock file is stale.
DEFAULT_STALE_LOCK_AGE_SECONDS = 24 * 60 * 60


def _scan_lock_files(lock_dir: str) -> list[str]:
    """Return the regular-file paths directly inside *lock_dir*.

    Best effort: a missing or unreadable directory yields an empty list.
    """
    paths: list[str] = []
    try:
        with os.scandir(lock_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        paths.append(entry.path)
                except OSError:
                    continue
    except (FileNotFoundError, PermissionError, OSError):
        return []
    return paths


def _lock_file_pid(path: str) -> int | None:
    """Return the PID recorded in a lock file, when its content is one.

    Many lock files hold a bare PID (optionally newline-terminated). Only a
    short, wholly numeric payload is treated as a PID — binary lock payloads
    (e.g. ALSA's) must not be misread as an owner.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(64)
    except OSError:
        return None
    token = text.strip()
    if not token or len(token) > 10 or not token.isdigit():
        return None
    pid = int(token)
    return pid if pid > 0 else None


def _pid_is_alive(pid: int, proc_root: str = "/proc") -> bool:
    """Return True when *pid* still has a ``/proc/<pid>`` entry."""
    return os.path.isdir(os.path.join(proc_root, str(pid)))


def _classify_lock_file(
    path: str,
    *,
    now: float | None = None,
    min_age_seconds: float = DEFAULT_STALE_LOCK_AGE_SECONDS,
    proc_root: str = "/proc",
    stat_fn=os.stat,
    pid_alive_fn=None,
) -> dict[str, Any]:
    """Decide whether a ``/var/lock`` entry is genuinely stale.

    A lock is stale only when it is **not** a known kernel boot lock, **not**
    root-owned, **older** than *min_age_seconds*, and its recorded owning PID
    (when present) is no longer running. Each rejection carries a ``reason``.

    Returns ``{path, stale, reason, age_seconds, uid, pid}``.
    """
    alive = pid_alive_fn or (
        lambda candidate: _pid_is_alive(candidate, proc_root)
    )
    if now is None:
        now = time.time()

    name = os.path.basename(path)
    detail: dict[str, Any] = {
        "path": path, "stale": False, "reason": "",
        "age_seconds": None, "uid": None, "pid": None,
    }
    try:
        st = stat_fn(path)
        uid = st.st_uid
        age = max(0.0, now - st.st_mtime)
    except OSError as exc:
        detail["reason"] = f"unreadable: {exc}"
        return detail

    pid = _lock_file_pid(path)
    detail.update(uid=uid, pid=pid, age_seconds=round(age, 1))

    if name in KERNEL_LOCK_FILES:
        detail["reason"] = "known kernel-owned boot lock (ALSA)"
        return detail
    if uid == 0:
        detail["reason"] = "root-owned (operator/system-managed)"
        return detail
    if age < min_age_seconds:
        detail["reason"] = f"recent (age {age:.0f}s < {min_age_seconds:.0f}s)"
        return detail
    if pid is not None and alive(pid):
        detail["reason"] = f"owning process {pid} is alive"
        return detail

    detail["stale"] = True
    if pid is not None:
        detail["reason"] = f"age {age:.0f}s and owning process {pid} is dead"
    else:
        detail["reason"] = f"age {age:.0f}s with no owning PID"
    return detail


def _inspect_lock_files(
    lock_dir: str = "/var/lock",
    *,
    now: float | None = None,
    min_age_seconds: float = DEFAULT_STALE_LOCK_AGE_SECONDS,
    proc_root: str = "/proc",
    stat_fn=os.stat,
    pid_alive_fn=None,
) -> list[dict[str, Any]]:
    """Classify every regular file in *lock_dir* (stale and non-stale)."""
    if now is None:
        now = time.time()
    return [
        _classify_lock_file(
            path, now=now, min_age_seconds=min_age_seconds,
            proc_root=proc_root, stat_fn=stat_fn, pid_alive_fn=pid_alive_fn,
        )
        for path in _scan_lock_files(lock_dir)
    ]


def _find_stale_lock_files(
    lock_dir: str = "/var/lock",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Return only the genuinely stale entries in *lock_dir*."""
    return [
        detail for detail in _inspect_lock_files(lock_dir, **kwargs)
        if detail["stale"]
    ]


def _detect_stale_devices(lock_dir: str = "/var/lock") -> dict[str, Any]:
    """Detect zombie processes and *genuinely* stale lock files.

    Returns ``{zombie_count, zombie_details, lock_files,
    lock_file_details, lock_files_inspected}``. ``lock_files`` contains only
    stale entries — kernel-owned boot locks, root-owned locks, recently
    touched files and locks whose recorded owner is alive are excluded.
    """
    zombies: list[tuple[int, str]] = []

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

    inspections = _inspect_lock_files(lock_dir) if os.path.isdir(lock_dir) else []
    stale_details = [detail for detail in inspections if detail["stale"]]

    return {
        "zombie_count": len(zombies),
        "zombie_details": zombies,
        "lock_files": [detail["path"] for detail in stale_details],
        "lock_file_details": stale_details,
        "lock_files_inspected": len(inspections),
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
    process_thresholds = _multiplier_thresholds(cfg["process_count"], cores)
    thread_thresholds = _multiplier_thresholds(cfg["thread_count"], cores)

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
    # Each spec is (proc file key, metric key, config key, PSI line, label).
    # cpu reads the ``some`` line (current kernels expose no ``full`` line);
    # memory and io read ``full`` — total stall is the actionable signal.
    pressure_specs = (
        ("cpu", "cpu_pressure", "cpu_pressure", "some", "CPU stall pressure"),
        ("memory", "memory_pressure", "memory_pressure", "full",
         "Memory stall pressure"),
        ("io", "io_pressure", "io_pressure", "full", "I/O stall pressure"),
    )
    for proc_key, metric_key, cfg_key, which, label in pressure_specs:
        value = _read_proc_pressure(proc_key, which)
        if value is not None:
            metrics[metric_key] = {
                "value": round(value, 2),
                "status": classify_metric(value, cfg[cfg_key])["status"],
                "description": (
                    f"{label} ({which} avg10, {_PSI_BASIS[which]})"
                ),
            }
        else:
            metrics[metric_key] = {
                "value": None,
                "status": "warning",
                "description": (
                    f"/proc/pressure/{proc_key} unavailable "
                    "(non-Linux or kernel missing pressure stall info; "
                    f"requested line: {which} avg10)"
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

    # --- Swap usage (gated on current memory pressure) ---
    swap_pct = 0.0
    if mem_info["swap_total_mb"] > 0:
        swap_pct = (
            (mem_info["swap_total_mb"] - mem_info["swap_free_mb"])
            / mem_info["swap_total_mb"]
        ) * 100
    swap_result = _classify_swap_usage(
        swap_pct,
        metrics["memory_usage"]["value"],
        metrics["memory_pressure"]["value"],
        cfg,
    )
    swap_desc = (
        f"Swap used: {round(mem_info['swap_total_mb'] - mem_info['swap_free_mb'], 0)} MB / "
        f"{round(mem_info['swap_total_mb'], 0)} MB; memory usage "
        f"{metrics['memory_usage']['value']}%"
    )
    if swap_result["gated"]:
        swap_desc += (
            " (no current memory pressure — historical accumulation, "
            "not actionable)"
        )
    elif swap_result["escalated"]:
        swap_desc += " (escalated: memory usage is critical)"
    metrics["swap_usage"] = {
        "value": round(swap_pct, 1),
        "status": swap_result["status"],
        "description": swap_desc,
    }

    # --- Process / thread counts (dedicated core-derived blocks) ---
    proc_count, thread_count = _get_process_counts()
    metrics["process_count"] = {
        "value": proc_count,
        "status": classify_metric(proc_count, process_thresholds)["status"],
        "description": (
            f"Total processes (threads: {thread_count}; "
            f"warn: {process_thresholds['warning']:.0f}, "
            f"crit: {process_thresholds['critical']:.0f})"
        ),
    }
    metrics["thread_count"] = {
        "value": thread_count,
        "status": classify_metric(thread_count, thread_thresholds)["status"],
        "description": (
            f"Total threads across all processes (cores: {cores}, "
            f"warn: {thread_thresholds['warning']:.0f}, "
            f"crit: {thread_thresholds['critical']:.0f})"
        ),
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
        "description": (
            f"Stale lock files in /var/lock: {len(stale['lock_files'])} "
            f"(inspected {stale.get('lock_files_inspected', 0)}; kernel-owned, "
            "root-owned, recent and live-PID locks excluded)"
        ),
    }

    for info in metrics.values():
        info["symbol"] = _STATUS_SYMBOLS.get(info["status"], "")

    return metrics
