#!/usr/bin/env python3
"""find_related — deterministic related-work discovery for Worklog work items.

Fetches a work item, derives keywords from its title/description, searches
Worklog and the repository for related items, generates a concise Markdown
report, and updates the work-item description.

Usage:
    python3 skill/find-related/scripts/find_related.py --work-item-id <id>
    python3 skill/find-related/scripts/find_related.py --work-item-id <id> --json
    python3 skill/find-related/scripts/find_related.py --work-item-id <id> --verbose
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_HEADING = "Related work (automated report)"


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Discover related work for a Worklog work item.",
    )
    parser.add_argument(
        "--work-item-id",
        required=True,
        help="ID of the work item to find related items for.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON.",
    )
    parser.add_argument(
        "--repo-path",
        default=str(REPO_ROOT),
        help="Path to the repository root (default: auto-detected).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()

    if args.verbose:
        print(f"[find-related] Work item: {args.work_item_id}", file=sys.stderr)
        print(f"[find-related] Repo path: {args.repo_path}", file=sys.stderr)

    # Placeholder — core logic will be implemented in subsequent work items
    result: Dict[str, Any] = {
        "workItemId": args.work_item_id,
        "found": False,
        "addedIds": [],
        "reportInserted": False,
        "message": "Skeleton implementation — no related-item logic yet.",
    }

    if args.json_output:
        print(json.dumps(result))
    else:
        print(f"Work item: {args.work_item_id}")
        print(f"Status: {result['message']}")

    sys.exit(0)


if __name__ == "__main__":
    main()
