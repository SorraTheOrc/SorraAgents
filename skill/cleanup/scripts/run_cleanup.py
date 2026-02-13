from __future__ import annotations

import argparse
import json
from typing import Any

from skill.cleanup.scripts import lib
from skill.cleanup.scripts import prune_local_branches
from skill.cleanup.scripts import cleanup_stale_remote_branches
from skill.cleanup.scripts import reconcile_worklog_items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run cleanup routines and emit a combined report."
    )
    lib.add_common_args(parser)
    parser.add_argument(
        "--skip-local", action="store_true", help="Skip local branch pruning"
    )
    parser.add_argument(
        "--skip-remote", action="store_true", help="Skip remote branch cleanup"
    )
    parser.add_argument(
        "--skip-worklog", action="store_true", help="Skip worklog reconciliation"
    )
    parser.add_argument("--default", help="Override default branch name")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--worklog-status", default="in_progress")
    parser.add_argument("--worklog-stage", default="in_review")
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)

    combined: dict[str, Any] = {
        "operation": "run_cleanup",
        "dry_run": args.dry_run,
        "steps": [],
    }

    if not args.skip_local:
        cmd = []
        if args.default:
            cmd.extend(["--default", args.default])
        if args.dry_run:
            cmd.append("--dry-run")
        if args.yes:
            cmd.append("--yes")
        cmd.append("--quiet")
        if args.verbose:
            cmd.extend(["--verbose"] * args.verbose)
        exit_code = prune_local_branches.main(cmd)
        combined["steps"].append(
            {"name": "prune_local_branches", "exit_code": exit_code}
        )

    if not args.skip_remote:
        cmd = ["--days", str(args.days)]
        if args.default:
            cmd.extend(["--default", args.default])
        if args.dry_run:
            cmd.append("--dry-run")
        if args.yes:
            cmd.append("--yes")
        cmd.append("--quiet")
        if args.verbose:
            cmd.extend(["--verbose"] * args.verbose)
        exit_code = cleanup_stale_remote_branches.main(cmd)
        combined["steps"].append(
            {"name": "cleanup_stale_remote_branches", "exit_code": exit_code}
        )

    if not args.skip_worklog:
        cmd = ["--status", args.worklog_status, "--stage", args.worklog_stage]
        if args.dry_run:
            cmd.append("--dry-run")
        if args.yes:
            cmd.append("--yes")
        cmd.append("--quiet")
        if args.verbose:
            cmd.extend(["--verbose"] * args.verbose)
        exit_code = reconcile_worklog_items.main(cmd)
        combined["steps"].append(
            {"name": "reconcile_worklog_items", "exit_code": exit_code}
        )

    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(combined, indent=2))
            handle.write("\n")
    print(json.dumps(combined, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
