#!/usr/bin/env python3
"""PlanAll v2: Batch planning for intake_complete items with auto-complete for well-defined items.

Processes all work items in `intake_complete` stage:
- Tasks/bugs/features with sufficient detail (acceptance criteria + implementation guidance)
  are auto-completed to `plan_complete`.
- Epics and items lacking sufficient detail are flagged for manual planning.

Usage:
    python3 skill/planall/scripts/planall_v2.py [--json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from typing import Callable, Sequence

logger = logging.getLogger("planall_v2")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


SUFFICIENT_INDICATORS = [
    "Acceptance Criteria",
    "Success Criteria",
    "## Implementation",
    "## Desired Change",
    "## Proposed Approach",
]


def has_sufficient_detail(item: dict) -> bool:
    """Check if a work item has sufficient detail for auto-complete.

    Criteria (from plan.md step 0):
    - Not an epic
    - Description contains measurable acceptance criteria
    - Description has an implementation sketch or desired change section
    """
    issue_type = (item.get("issueType") or "").lower()
    if issue_type == "epic":
        return False

    desc = item.get("description") or ""
    if not desc:
        return False

    has_ac = False
    has_impl = False
    for indicator in SUFFICIENT_INDICATORS:
        if indicator.lower() in desc.lower():
            if "criteria" in indicator.lower() or "acceptance" in indicator.lower():
                has_ac = True
            else:
                has_impl = True

    # If we have explicit AC, that's the most important signal
    # Having either AC + any implementation signal is sufficient
    return has_ac or (has_impl and len(desc) > 500)


class PlanAllV2Engine:
    """Orchestrates batch planning for all intake_complete work items using auto-complete."""

    def __init__(self, runner: Runner | None = None, dry_run: bool = False, verbose: bool = False):
        self.runner = runner or _default_runner
        self.dry_run = dry_run
        self.verbose = verbose

    def discover_items(self) -> list[dict]:
        """Query wl to discover all work items in intake_complete stage."""
        cmd = ["wl", "list", "--stage", "intake_complete", "--json"]
        logger.debug("discover cmd=%s", " ".join(cmd))

        try:
            result = self.runner(cmd)
        except Exception as exc:
            logger.warning("discover.error cmd=%s exc=%s", " ".join(cmd), exc)
            return []

        if result.returncode != 0:
            logger.warning(
                "discover.failed cmd=%s rc=%s stderr=%s",
                " ".join(cmd),
                result.returncode,
                (result.stderr or "").strip(),
            )
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("discover.json_error exc=%s", exc)
            return []

        # Expect { "success": true, "workItems": [...] }
        if isinstance(data, dict):
            return data.get("workItems") or data.get("work_items") or data.get("items") or []

        if isinstance(data, list):
            return data

        return []

    def auto_complete(self, item: dict) -> str:
        """Auto-complete a work item by updating its stage to plan_complete.

        Returns: "completed", "skipped", or "error"
        """
        item_id = item.get("id", "")
        issue_type = (item.get("issueType") or "").lower()

        if not item_id:
            return "skipped"

        # Epic items need full planning
        if issue_type == "epic":
            logger.info("item=%s skipping epic (needs full planning)", item_id)
            return "needs_planning"

        # Check if it needs planning or can be auto-completed
        if not has_sufficient_detail(item):
            logger.info("item=%s insufficient detail for auto-complete", item_id)
            return "needs_planning"

        # Auto-complete: update stage to plan_complete
        if not self.dry_run:
            # Claim the item first
            claim_cmd = ["wl", "update", item_id, "--status", "in_progress", "--json"]
            logger.debug("claim cmd=%s", " ".join(claim_cmd))
            try:
                self.runner(claim_cmd)
            except Exception as exc:
                logger.warning("claim.error item=%s exc=%s", item_id, exc)
                return "error"

            # Mark as plan_complete
            complete_cmd = [
                "wl", "update", item_id,
                "--stage", "plan_complete",
                "--status", "open",
                "--json",
            ]
            logger.debug("complete cmd=%s", " ".join(complete_cmd))
            try:
                result = self.runner(complete_cmd)
            except Exception as exc:
                logger.warning("complete.error item=%s exc=%s", item_id, exc)
                return "error"

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                logger.warning(
                    "complete.failed item=%s rc=%s stderr=%s",
                    item_id, result.returncode, stderr,
                )
                return "error"

            # Add a comment explaining the auto-complete
            comment_cmd = [
                "wl", "comment", "add", item_id,
                "--comment",
                "Plan auto-completed: work item has sufficient detail "
                "(acceptance criteria + implementation guidance) for direct implementation. "
                "See plan.md Step 0 for the auto-complete criteria.",
                "--author", "planall",
                "--json",
            ]
            try:
                self.runner(comment_cmd)
            except Exception as exc:
                logger.warning("comment.error item=%s exc=%s", item_id, exc)

        return "completed"

    def run_all(self) -> list[dict]:
        """Process all intake_complete items and return results."""
        items = self.discover_items()
        results: list[dict] = []

        for item in items:
            item_id = item.get("id", "")
            if not item_id:
                continue

            title = item.get("title", "")
            issue_type = (item.get("issueType") or "").lower()
            outcome = self.auto_complete(item)

            results.append({
                "id": item_id,
                "title": title,
                "issueType": issue_type,
                "outcome": outcome,
            })

            if self.verbose:
                print(f"  {item_id}: {outcome}")

        return results


def generate_summary(results: list[dict], json_output: bool = False) -> str:
    """Generate a summary report from processing results."""
    total = len(results)
    outcomes = {}
    for r in results:
        o = r.get("outcome", "unknown")
        outcomes[o] = outcomes.get(o, 0) + 1

    completed = outcomes.get("completed", 0)
    needs_planning = outcomes.get("needs_planning", 0)
    errors = outcomes.get("error", 0)
    skipped = outcomes.get("skipped", 0)

    if json_output:
        report = {
            "total": total,
            "completed": completed,
            "needs_planning": needs_planning,
            "errors": errors,
            "skipped": skipped,
            "items": [
                {
                    "id": r["id"],
                    "title": r.get("title", ""),
                    "issueType": r.get("issueType", ""),
                    "outcome": r.get("outcome", "unknown"),
                }
                for r in results
            ],
        }
        return json.dumps(report, indent=2)

    lines = [
        "# PlanAll Summary",
        "",
        f"**Total processed**: {total}",
        f"**Auto-completed**: {completed}",
        f"**Needs planning**: {needs_planning}",
        f"**Errors**: {errors}",
        f"**Skipped**: {skipped}",
        "",
    ]
    if results:
        lines.append("## Results")
        lines.append("")
        for r in results:
            item_id = r.get("id", "?")
            title = r.get("title", "")
            outcome = r.get("outcome", "unknown")
            itype = r.get("issueType", "")
            title_part = f" — {title}" if title else ""
            lines.append(f"- **{item_id}**{title_part} (`{itype}`): `{outcome}`")
        lines.append("")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PlanAll v2: Batch planning with auto-complete",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--dry-run", action="store_true", help="Dry run (no changes)")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=logging_level, format="%(levelname)s:%(name)s:%(message)s")

    engine = PlanAllV2Engine(dry_run=args.dry_run, verbose=args.verbose)
    results = engine.run_all()

    if args.json:
        report = generate_summary(results, json_output=True)
        print(report)
    else:
        print(generate_summary(results, json_output=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
