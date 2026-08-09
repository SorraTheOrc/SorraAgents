#!/usr/bin/env python3
"""cleanup_debug_logs.py — retention sweep for audit debug logs.

Work item: SA-0MSBSOAEM0078LAO (parent SA-0MSAEJCP7002LTIM — "Optimize audit
grep scans: only perform required and efficient scans").

Audit debug logs (``audit_debug_*.jsonl``, written by ``audit_runner.py`` on
parse_failure/provider_error or explicit ``--debug-log``) live under
``~/.audit_debug/<project>/`` — outside ``.worklog/`` and the repo tree — and
are transient forensics. This script sweeps files older than a configurable
retention age.

Defaults to **dry-run** (lists what would be removed). Use ``--apply`` to
actually delete. Retention defaults to 14 days (``--older-than N`` overrides).

Stdlib only; no network. Exit codes: 0 success, 1 usage/error.
"""  # noqa: EXE001
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DEFAULT_RETENTION_DAYS = 14
DEFAULT_DEBUG_ROOT = Path.home() / ".audit_debug"


def _is_debug_log(name: str) -> bool:
    return name.startswith("audit_debug_") and name.endswith(".jsonl")


def collect_candidates(root: Path, older_than_days: int) -> list[Path]:
    """Return debug files under *root* older than the retention age."""
    now = time.time()
    cutoff = now - older_than_days * 86400
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.rglob("audit_debug_*.jsonl"):
        if not p.is_file() or not _is_debug_log(p.name):
            continue
        try:
            if p.stat().st_mtime < cutoff:
                out.append(p)
        except OSError:
            continue
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cleanup_debug_logs.py",
        description=(
            "Sweep audit debug logs older than a retention age. "
            "Defaults to dry-run; pass --apply to delete."
        ),
    )
    parser.add_argument("--dir", default=str(DEFAULT_DEBUG_ROOT),
                        help=f"Debug log directory (default {DEFAULT_DEBUG_ROOT})")
    parser.add_argument("--older-than", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"Retention age in days (default {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete files (default is dry-run)")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if args.older_than < 0:
        print("error: --older-than must be >= 0", file=sys.stderr)
        return 1

    candidates = collect_candidates(root, args.older_than)
    total_bytes = sum(p.stat().st_size for p in candidates if p.exists())

    if not candidates:
        print(f"No debug logs older than {args.older_than}d under {root}.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(candidates)} debug log(s) older than {args.older_than}d "
          f"under {root} ({total_bytes / (1024**3):.2f} GB):")
    for p in candidates:
        if args.apply:
            try:
                p.unlink()
                print(f"  removed {p}")
            except OSError as exc:
                print(f"  error removing {p}: {exc}", file=sys.stderr)
        else:
            print(f"  would remove {p}")

    if not args.apply:
        print("\nDry-run: no files were changed. Re-run with --apply to delete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
