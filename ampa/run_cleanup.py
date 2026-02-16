"""Run repository cleanup scripts bundled in the user's AMPA install.

This module is intended to be executed as a module so the scheduler can
invoke it from any working directory: `python -m ampa.run_cleanup`.

It will execute all `*.py` scripts in
`~/.config/opencode/skill/cleanup/scripts/` except `__init__.py` and
`lib.py`, passing `--yes --quiet` to make them non-interactive and
concise. The number of days threshold for remote cleanup can be set via
the `AMPA_CLEANUP_DAYS` environment variable or by passing a single
positional argument to the module (e.g. `python -m ampa.run_cleanup 60`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List


DEFAULT_DAYS = "30"


def _call(cmd: List[str]) -> int:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    return int(proc.returncode or 0)


def main(argv: List[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    # allow overriding days via arg or env
    days = os.getenv("AMPA_CLEANUP_DAYS") or (args[0] if args else DEFAULT_DAYS)

    scripts_dir = Path(os.path.expanduser("~/.config/opencode/skill/cleanup/scripts"))
    if not scripts_dir.exists() or not scripts_dir.is_dir():
        print(f"Cleanup scripts directory not found: {scripts_dir}", file=sys.stderr)
        return 1

    # Exclude utility modules that change working branch or perform non-delete actions.
    # In particular `switch_to_default_and_update.py` should not be run by the
    # periodic cleanup because it will change the current branch (e.g. switch to
    # main). The scheduled cleanup must only delete merged branches and remote
    # branches — not move HEAD.
    excluded = {"__init__.py", "lib.py", "switch_to_default_and_update.py"}
    py_files = sorted([p for p in scripts_dir.glob("*.py") if p.name not in excluded])
    if not py_files:
        print("No cleanup scripts found to run.")
        return 0

    exit_codes: List[int] = []
    for p in py_files:
        cmd = [sys.executable, str(p), "--yes", "--quiet"]
        # Add --days for scripts that accept it (best-effort)
        if p.name == "cleanup_stale_remote_branches.py":
            cmd.extend(["--days", str(days)])
        print("Running:", " ".join(cmd))
        rc = _call(cmd)
        exit_codes.append(rc)
        if rc != 0:
            print(f"Command failed: {p} (rc={rc})", file=sys.stderr)

    return 0 if all(rc == 0 for rc in exit_codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
