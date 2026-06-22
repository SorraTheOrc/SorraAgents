#!/usr/bin/env python3
"""PlanAll: Automated Batch Planning for intake_complete items.

Queries all work items in `intake_complete` status and invokes the `/plan`
command for each item sequentially. Detects items that require producer
input (unanswered questions) and produces a summary report.

Usage:
    python3 skill/planall/scripts/planall.py [--json] [--parent-id <id>]

Related work item: SA-0MQA6ECEU003GUKH
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Callable, Sequence

# Add repo root to sys.path for shared utility access
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.scripts.failure_notice import FailureNotice


logger = logging.getLogger("planall")

# Type alias for runners: a callable that accepts a command sequence and
# returns an object with returncode, stdout, stderr attributes.
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


# ---------------------------------------------------------------------------
# Default subprocess runner
# ---------------------------------------------------------------------------

def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    """Default runner: delegates to subprocess.run."""
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# PlanAllEngine
# ---------------------------------------------------------------------------

class PlanAllEngine:
    """Orchestrates batch planning for all intake_complete work items.

    Args:
        runner: A callable that executes shell commands. Defaults to
            subprocess.run. Tests provide a fake runner.
        verbose: Enable verbose logging.
    """

    def __init__(self, runner: Runner | None = None, verbose: bool = False):
        self.runner = runner or _default_runner
        self.verbose = verbose

    # -----------------------------------------------------------------------
    # Discovery
    # -----------------------------------------------------------------------

    def discover_items(self) -> list[dict]:
        """Query wl to discover all work items in intake_complete stage.

        Returns:
            A list of work item dicts, or an empty list on error.
        """
        cmd = ["wl", "list", "--stage", "intake_complete", "--json"]
        logger.debug("planall.discover cmd=%s", " ".join(cmd))

        try:
            result = self.runner(cmd)
        except Exception as exc:
            logger.warning("planall.discover.error cmd=%s exc=%s", " ".join(cmd), exc)
            return []

        if result.returncode != 0:
            logger.warning(
                "planall.discover.failed cmd=%s rc=%s stderr=%s",
                " ".join(cmd),
                result.returncode,
                (result.stderr or "").strip(),
            )
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("planall.discover.json_error exc=%s stdout=%s", exc, result.stdout[:500])
            return []

        # wl list --json may return various shapes
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("workItems") or data.get("work_items") or data.get("items", [])
            if isinstance(items, list):
                return items
        return []

    # -----------------------------------------------------------------------
    # Plan invocation
    # -----------------------------------------------------------------------

    def _invoke_plan(self, item_id: str) -> str:
        """Claim an item and invoke /plan for it.

        Args:
            item_id: The work item ID to process.

        Returns:
            One of "planned", "needs_input", or "error".
        """
        # Claim the item
        claim_cmd = [
            "wl", "update", item_id,
            "--status", "in_progress",
            "--stage", "in_progress",
            "--json",
        ]
        logger.debug("planall.claim cmd=%s", " ".join(claim_cmd))
        try:
            claim_result = self.runner(claim_cmd)
        except Exception as exc:
            logger.warning("planall.claim.exception item=%s exc=%s", item_id, exc)
            return "error"

        if claim_result.returncode != 0:
            logger.warning(
                "planall.claim.failed item=%s rc=%s stderr=%s",
                item_id,
                claim_result.returncode,
                (claim_result.stderr or "").strip(),
            )
            return "error"

        # Invoke /plan via pi
        plan_cmd = ["pi", "run", f"/plan {item_id}"]
        logger.debug("planall.plan.invoke cmd=%s", " ".join(plan_cmd))
        try:
            plan_result = self.runner(plan_cmd)
        except Exception as exc:
            logger.warning("planall.plan.exception item=%s exc=%s", item_id, exc)
            return "error"

        if plan_result.returncode != 0:
            stderr = (plan_result.stderr or "").strip()
            stdout = (plan_result.stdout or "").strip()
            logger.warning(
                "planall.plan.failed item=%s rc=%s stderr=%s stdout=%s",
                item_id,
                plan_result.returncode,
                stderr,
                stdout[:500],
            )
            # If the plan command stopped with non-zero exit, it likely
            # indicates unanswered questions (producer input needed).
            # Also check stdout for question-like patterns.
            if self._contains_questions(plan_result.stdout or ""):
                return "needs_input"
            return "error"

        return "planned"

    @staticmethod
    def _contains_questions(text: str) -> bool:
        """Heuristic: detect if the plan output contains unanswered questions."""
        question_indicators = [
            "? (yes/no)",
            "? (y/n)",
            "? [y/n]",
            "? (Y/n)",
            "unanswered",
            "requires input",
            "producer input",
            "What should",
            "Should we",
            "Do you want",
            "Please answer",
            "Choose",
            "Select",
        ]
        lower_text = text.lower()
        for indicator in question_indicators:
            if indicator.lower() in lower_text:
                return True
        return False

    # -----------------------------------------------------------------------
    # Run all
    # -----------------------------------------------------------------------

    def run_all(self) -> list[dict]:
        """Process all intake_complete items and return results.

        Returns:
            A list of result dicts, each with keys:
                - id: work item ID
                - title: work item title (or empty string if not available)
                - outcome: "planned", "needs_input", or "error"
        """
        items = self.discover_items()
        results: list[dict] = []

        for item in items:
            item_id = item.get("id", "")
            if not item_id:
                continue

            title = item.get("title", "")
            outcome = self._invoke_plan(item_id)
            results.append({
                "id": item_id,
                "title": title,
                "outcome": outcome,
            })

        return results

    # -----------------------------------------------------------------------
    # Summary posting
    # -----------------------------------------------------------------------

    def post_summary(self, results: list[dict], parent_id: str | None = None) -> None:
        """Post the summary report to stdout and optionally as a wl comment.

        Args:
            results: The list of processing results.
            parent_id: If provided, post the summary as a comment on this item.
        """
        summary_md = _wrap_with_failure_notice_if_needed(
            generate_summary(results, json_output=False),
            results,
            script_name="planall.py",
        )
        print(summary_md)

        if parent_id:
            comment_cmd = [
                "wl", "comment", "add", parent_id,
                "--comment", summary_md,
                "--author", "planall",
                "--json",
            ]
            logger.debug("planall.comment cmd=%s", " ".join(comment_cmd))
            try:
                self.runner(comment_cmd)
            except Exception as exc:
                logger.warning(
                    "planall.comment.failed parent=%s exc=%s",
                    parent_id,
                    exc,
                )


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

def _wrap_with_failure_notice_if_needed(report: str, results: list[dict],
                                       script_name: str = "planall.py") -> str:
    """Wrap the report with a FailureNotice if any results have errors."""
    errors = sum(1 for r in results if r.get("outcome") == "error")
    if errors == 0:
        return report
    return FailureNotice(
        script_name=script_name,
        reason=f"{errors} item(s) failed during batch planning",
    ).wrap(report)


def generate_summary(results: list[dict], json_output: bool = False) -> str:
    """Generate a summary report from processing results.

    Args:
        results: List of result dicts with id, title, outcome keys.
        json_output: If True, produce JSON instead of Markdown.

    Returns:
        A Markdown or JSON string.
    """
    total = len(results)
    planned = sum(1 for r in results if r.get("outcome") == "planned")
    needs_input = sum(1 for r in results if r.get("outcome") == "needs_input")
    errors = sum(1 for r in results if r.get("outcome") == "error")

    if json_output:
        report = {
            "total": total,
            "planned": planned,
            "needs_input": needs_input,
            "errors": errors,
            "items": [
                {"id": r["id"], "title": r.get("title", ""), "outcome": r.get("outcome", "unknown")}
                for r in results
            ],
        }
        return json.dumps(report, indent=2)

    lines = [
        "# PlanAll Summary",
        "",
        f"**Total processed**: {total}",
        f"**Planned**: {planned}",
        f"**Needs input**: {needs_input}",
        f"**Errors**: {errors}",
        "",
    ]
    if results:
        lines.append("## Results")
        lines.append("")
        for r in results:
            item_id = r.get("id", "?")
            title = r.get("title", "")
            outcome = r.get("outcome", "unknown")
            title_part = f" — {title}" if title else ""
            lines.append(f"- **{item_id}**{title_part}: `{outcome}`")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        description="PlanAll: Automated batch planning for intake_complete items",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Produce JSON output instead of Markdown",
    )
    parser.add_argument(
        "--parent-id",
        type=str,
        default=None,
        help="Post the summary as a comment on the specified parent work item",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for PlanAll.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        return _main(argv)
    except Exception as exc:
        notice = FailureNotice(
            script_name="planall.py",
            reason=f"Unhandled exception: {exc}",
            stderr_context=traceback.format_exc(),
        )
        print(notice.wrap(
            f"An unexpected error occurred: {exc}"
        ))
        return 1


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=logging_level,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    engine = PlanAllEngine(verbose=args.verbose)
    results = engine.run_all()

    if args.json:
        report = generate_summary(results, json_output=True)
        print(report)
    else:
        engine.post_summary(results, parent_id=args.parent_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
