"""Measure process fan-out on the host (pi, node/vitest, wl sync, audits).

Read-only diagnostic for the concurrent-audit fan-out investigation
(SA-0MSAEKOQE009TEB4). Captures:

- per-type process counts (pi, node, vitest/tinypool, wl sync, audit_runner)
- load average (1/5/15 min)
- memory + swap usage
- CPU count and timestamp

Outputs a single JSON document on stdout by default (machine-readable),
a human-readable summary with ``--human``, and can persist the JSON artifact
to a file with ``--out <path>`` for baseline comparison.

The script is intentionally non-intrusive: it only reads process table and
system statistics via ``psutil`` / ``os`` — it never writes, spawns, or
modifies anything except an explicit ``--out`` target.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - only when psutil missing
    psutil = None

DEFAULT_OUT_SUFFIX = "fanout-baseline.json"


def _norm_path(value: str) -> str:
    """Normalize a path-ish string for matching (strip quotes, trailing /)."""
    return value.strip().strip("'\"") .rstrip("/")


def classify_processes() -> dict[str, int]:
    """Count processes by fan-out category.

    Returns a dict with keys: ``pi``, ``node``, ``vitest``, ``wl_sync``,
    ``audit``. Categories are mutually exclusive; the most specific match
    wins (audit > wl_sync > vitest > pi > node).
    """
    counts = {"pi": 0, "node": 0, "vitest": 0, "wl_sync": 0, "audit": 0}
    if psutil is None:  # pragma: no cover
        return counts

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = [_norm_path(a) for a in (proc.info.get("cmdline") or [])]
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue

        cmd_joined = " ".join(cmdline).lower()

        # Specific first: audit_runner.py invocation
        if "audit_runner.py" in cmd_joined:
            counts["audit"] += 1
            continue

        # wl sync: worklog cli invoked with the sync subcommand
        if cmdline and cmdline[-1] == "sync" and (
            "cli.js" in cmd_joined or "worklog" in cmd_joined or "wl" in cmd_joined
        ):
            counts["wl_sync"] += 1
            continue

        # vitest / tinypool workers
        if "tinypool" in cmd_joined or "vitest" in cmd_joined:
            counts["vitest"] += 1
            continue

        # pi agent invocations: binary named pi, or `pi -p`, or node wrapper
        # pointing at a pi binary
        is_pi = False
        if cmdline:
            first = os.path.basename(cmdline[0])
            if first == "pi" or first.startswith("pi-") or any(
                os.path.basename(a) == "pi" or a.endswith("/pi")
                for a in cmdline[1:3]
            ):
                is_pi = True
        if is_pi:
            counts["pi"] += 1
            continue

        # Plain node processes
        if name == "node" or (cmdline and "node" in os.path.basename(cmdline[0])):
            counts["node"] += 1
            continue

    return counts


def _swap_stats() -> dict[str, Any]:
    """Return swap usage as {total, used, free} bytes (0s if unavailable)."""
    if psutil is not None:
        try:
            sm = psutil.swap_memory()
            return {"total": sm.total, "used": sm.used, "free": sm.free}
        except Exception:  # noqa: S110, BLE001 -- psutil unavailable; fall back to /proc/meminfo
            pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            data = {}
            for line in fh:
                key, _, rest = line.partition(":")
                data[key.strip()] = int(rest.split()[0]) * 1024
        total = data.get("SwapTotal", 0)
        free = data.get("SwapFree", 0)
        return {"total": total, "used": max(total - free, 0), "free": free}
    except OSError:  # pragma: no cover
        return {"total": 0, "used": 0, "free": 0}


def collect() -> dict[str, Any]:
    """Collect a full fan-out measurement snapshot."""
    try:
        load = os.getloadavg()
        load_vals = {"1min": load[0], "5min": load[1], "15min": load[2]}
    except (AttributeError, OSError):  # pragma: no cover
        load_vals = {"1min": 0.0, "5min": 0.0, "15min": 0.0}

    cpu_count = os.cpu_count() or 0

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "cpu_count": cpu_count,
        "load": load_vals,
        "swap": _swap_stats(),
        "processes": classify_processes(),
    }


def format_human(data: dict[str, Any]) -> str:
    """Render a human-readable summary of a measurement snapshot."""
    proc = data["processes"]
    load = data["load"]
    swap = data["swap"]
    swap_gib = swap["used"] / (1024 ** 3)
    swap_total_gib = swap["total"] / (1024 ** 3)

    lines = [
        f"Fan-out snapshot at {data['timestamp']} on {data['host']}",
        f"  CPU count : {data['cpu_count']}",
        f"  Load      : {load['1min']:.2f} (1m) {load['5min']:.2f} (5m) {load['15min']:.2f} (15m)",
        f"  Swap      : {swap_gib:.2f} GiB used / {swap_total_gib:.2f} GiB total",
        (
            f"  Processes : pi={proc['pi']} node={proc['node']} vitest={proc['vitest']} "
            f"wl_sync={proc['wl_sync']} audit={proc['audit']}"
        ),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure process fan-out (pi, node/vitest, wl sync, audits), "
            "load average, and swap usage. Read-only; emits JSON by default."
        )
    )
    parser.add_argument(
        "--human",
        action="store_true",
        help="Emit a human-readable summary instead of JSON.",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        help=f"Also write the JSON snapshot to FILE (default: {DEFAULT_OUT_SUFFIX}).",
    )
    args = parser.parse_args(argv)

    data = collect()

    if args.out:
        out_path = args.out
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        if not args.human:
            print(json.dumps(data, indent=2))

    if args.human:
        print(format_human(data))
    elif not args.out:
        print(json.dumps(data, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
