from __future__ import annotations

import argparse
from typing import Any

from scripts.cleanup import lib


def should_close(item: dict[str, Any]) -> bool:
    status = str(item.get("status", "")).lower()
    stage = str(item.get("stage", "")).lower()
    if status in {"done", "closed", "completed"}:
        return False
    if stage in {"done", "closed", "in_review", "in-review"}:
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile worklog items and optionally close resolved ones."
    )
    lib.add_common_args(parser)
    parser.add_argument(
        "--status",
        default="in_progress",
        help="Status to query for candidate work items",
    )
    parser.add_argument(
        "--stage",
        default="in_review",
        help="Stage to treat as resolved",
    )
    args = parser.parse_args(argv)

    lib.configure_logging(args.verbose)
    runner = lib.CommandRunner()

    if not lib.ensure_tool_available("wl"):
        lib.exit_with_error("wl is required")

    proc = runner.run(["wl", "list", "--status", args.status, "--json"])
    payload = lib.parse_json_payload(proc.stdout)
    items = lib.normalize_items(payload)

    actions: list[dict[str, Any]] = []
    for item in items:
        work_id = item.get("id")
        title = item.get("title")
        stage = str(item.get("stage", ""))
        resolved = stage.lower() == args.stage.lower() or should_close(item)
        if not resolved:
            actions.append(
                {
                    "work_item": work_id,
                    "title": title,
                    "action": "skip",
                    "result": "not_resolved",
                }
            )
            continue
        if not lib.confirm_action(
            f"Close work item {work_id}?", args.yes, args.dry_run
        ):
            actions.append(
                {
                    "work_item": work_id,
                    "title": title,
                    "action": "skip",
                    "result": "declined",
                }
            )
            continue
        proc_close = lib.run_command(
            [
                "wl",
                "close",
                str(work_id),
                "--reason",
                "Resolved via cleanup reconciliation",
                "--json",
            ],
            dry_run=args.dry_run,
            destructive=True,
            runner=runner,
        )
        actions.append(
            {
                "work_item": work_id,
                "title": title,
                "action": "close",
                "result": "closed" if proc_close.returncode == 0 else "failed",
                "stderr": proc_close.stderr.strip(),
            }
        )

    report = {
        "operation": "reconcile_worklog_items",
        "dry_run": args.dry_run,
        "status_query": args.status,
        "resolved_stage": args.stage,
        "actions": actions,
        "summary": lib.render_summary(actions),
    }
    lib.write_report(report, args.report, print_output=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
