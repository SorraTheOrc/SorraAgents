#!/usr/bin/env python3
"""Status lifecycle script for the intake skill.

Manages work-item status transitions for the intake process using the shared
StatusLifecycle context manager. The SKILL.md instructions reference this
script instead of using ad-hoc ``wl update --status`` commands.

Usage::

    python3 skill/intake/scripts/intake.py start <work-item-id> [--assignee <name>]
    python3 skill/intake/scripts/intake.py finish <work-item-id> [--description-file <path>]
    python3 skill/intake/scripts/intake.py auto-complete <work-item-id>
    python3 skill/intake/scripts/intake.py abort <work-item-id>

Exit codes:
    0 — success
    1 — error during execution

All commands produce JSON output for agent consumption.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

# Ensure the repository root is on sys.path so skill package imports work
_REPO_ROOT = Path(__file__).resolve().parents[3]  # e.g. <repo>/skill/intake/scripts/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.shared.status_lifecycle import StatusLifecycle  # noqa: E402

LOG = logging.getLogger("intake.scripts.intake")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_start(item_id: str, assignee: str | None = None) -> dict:
    """Claim the work item by setting status to in_progress.

    Uses StatusLifecycle.update_status to set the status without the
    overhead of a full context manager (the context manager is more
    appropriate when wrapping a block of code).

    Args:
        item_id: The work item ID.
        assignee: Optional assignee name.

    Returns:
        A dict with action and item_id keys.
    """
    kwargs: dict = {"status": "in_progress"}
    if assignee:
        kwargs["assignee"] = assignee
    StatusLifecycle.update_status(item_id, **kwargs)
    LOG.info("Intake started for %s (assignee=%s)", item_id, assignee)
    return {"success": True, "action": "started", "item_id": item_id}


def cmd_finish(item_id: str, description_file: str | None = None) -> dict:
    """Complete the intake process: set status to open, stage to intake_complete.

    Optionally applies a description file before the status update.

    Args:
        item_id: The work item ID.
        description_file: Optional path to a markdown description file to apply
            via ``wl update --description-file`` before the status transition.

    Returns:
        A dict with action and item_id keys.
    """
    if description_file:
        _run_wl_update_description(item_id, description_file)

    StatusLifecycle.update_status(item_id, "open", stage="intake_complete")
    LOG.info("Intake finished for %s", item_id)
    return {"success": True, "action": "finished", "item_id": item_id}


def cmd_auto_complete(item_id: str) -> dict:
    """Auto-complete a well-defined work item without a full intake interview.

    Transitions: in_progress → open with stage=intake_complete.

    Args:
        item_id: The work item ID.

    Returns:
        A dict with action and item_id keys.
    """
    StatusLifecycle.update_status(item_id, "in_progress")
    StatusLifecycle.update_status(item_id, "open", stage="intake_complete")
    LOG.info("Intake auto-completed for %s", item_id)
    return {"success": True, "action": "auto_completed", "item_id": item_id}


def cmd_abort(item_id: str) -> dict:
    """Reset work item status to open on abort/interrupt.

    Args:
        item_id: The work item ID.

    Returns:
        A dict with action and item_id keys.
    """
    StatusLifecycle.update_status(item_id, "open")
    LOG.info("Intake aborted for %s", item_id)
    return {"success": True, "action": "aborted", "item_id": item_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_wl_update_description(item_id: str, description_file: str) -> None:
    """Run ``wl update --description-file`` to apply the intake draft.

    Args:
        item_id: The work item ID.
        description_file: Path to the description file.

    Raises:
        RuntimeError: If the wl command fails.
    """
    cmd = [
        "wl", "update", item_id,
        "--description-file", description_file,
        "--json",
    ]
    LOG.debug("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl update --description-file failed: {proc.stderr.strip()}"
        )
    LOG.info("Description file applied for %s: %s", item_id, description_file)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        description="Intake skill: StatusLifecycle CLI for the intake process",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    start_parser = subparsers.add_parser("start", help="Claim the work item")
    start_parser.add_argument("item_id", help="Work item ID (e.g. SA-XXXX)")
    start_parser.add_argument(
        "--assignee", "-a",
        default=None,
        help="Optional assignee name",
    )

    # finish
    finish_parser = subparsers.add_parser("finish", help="Complete intake")
    finish_parser.add_argument("item_id", help="Work item ID")
    finish_parser.add_argument(
        "--description-file", "-d",
        default=None,
        help="Path to the intake draft description file",
    )

    # auto-complete
    auto_parser = subparsers.add_parser("auto-complete", help="Auto-complete intake")
    auto_parser.add_argument("item_id", help="Work item ID")

    # abort
    abort_parser = subparsers.add_parser("abort", help="Abort intake")
    abort_parser.add_argument("item_id", help="Work item ID")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for success, 1 for error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "start":
            result = cmd_start(args.item_id, assignee=args.assignee)
        elif args.command == "finish":
            result = cmd_finish(args.item_id, description_file=args.description_file)
        elif args.command == "auto-complete":
            result = cmd_auto_complete(args.item_id)
        elif args.command == "abort":
            result = cmd_abort(args.item_id)
        else:
            print(json.dumps({"success": False, "error": f"Unknown command: {args.command}"}))
            return 1
    except Exception as exc:
        LOG.error("Command failed: %s", exc)
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    sys.exit(main())
