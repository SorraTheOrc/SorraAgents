# Cleanup skill scripts

This directory contains the scripted, non-interactive implementations used by the
cleanup skill. They mirror the high-level behaviour described in SKILL.md but
are intended to be run directly by automation or from CI.

Location:

- skill/cleanup/scripts/

Files / entrypoints:

- skill/cleanup/scripts/prune_local_branches.py
- skill/cleanup/scripts/cleanup_stale_remote_branches.py
- skill/cleanup/scripts/reconcile_worklog_items.py
- skill/cleanup/scripts/run_cleanup.py

Common flags:

- `--dry-run`: list actions but do not perform deletes or closes.
- `--yes`: assume yes for prompts (use with caution).
- `--report <path>`: emit a JSON machine-readable report to path.
- `--quiet`: suppress printed JSON to stdout (useful when invoking via `run_cleanup.py`).
- `--verbose`: increase logging output.

Safety notes:

- The default CI configuration runs these scripts in `--dry-run` mode.
- Remote deletions are destructive; ensure proper permissions and explicit opt-in.
