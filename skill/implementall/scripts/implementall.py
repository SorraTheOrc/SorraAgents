#!/usr/bin/env python3
"""ImplementAll: Automated Batch Implementation for plan_complete items.

Queries all work items in `plan_complete` stage and invokes the implement
workflow via `/skill:implement <id>` for each item sequentially. Detects
items that require producer input (unanswered questions) and produces a
summary report.

Usage:
    python3 skill/implementall/scripts/implementall.py [--json] [--dry-run]
        [--parent-id <id>] [--max N] [--verbose]

Related work item: SA-0MQO6YMZ3006N5MG
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# Add repo root to sys.path for shared utility access
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.scripts.failure_notice import FailureNotice

logger = logging.getLogger("implementall")

# Type alias for runners: a callable that accepts a command sequence and an
# optional timeout, returning an object with returncode, stdout, stderr attributes.
Runner = Callable[..., subprocess.CompletedProcess]


# ---------------------------------------------------------------------------
# Default subprocess runner
# ---------------------------------------------------------------------------

def _default_runner(cmd: Sequence[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    """Default runner: delegates to subprocess.run.

    Args:
        cmd: Command sequence to execute.
        timeout: Optional timeout in seconds for the subprocess call.
    """
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False, timeout=timeout)


# ---------------------------------------------------------------------------
# ImplementAllEngine
# ---------------------------------------------------------------------------

class ImplementAllEngine:
    """Orchestrates batch implementation for all plan_complete work items.

    Args:
        runner: A callable that executes shell commands. Defaults to
            subprocess.run. Tests provide a fake runner.
        dry_run: If True, log actions without making changes.
        max_items: Maximum number of items to process. 0 means no limit.
        verbose: Enable verbose logging.
    """

    def __init__(self, runner: Runner | None = None,
                 dry_run: bool = False, max_items: int = 0,
                 item_timeout: int = 600, verbose: bool = False):
        self.runner = runner or _default_runner
        self.dry_run = dry_run
        self.max_items = max_items
        self.item_timeout = item_timeout
        self.verbose = verbose
        # Track the item currently being processed for signal-handler recovery
        self._current_item_id: Optional[str] = None
        # Saved original signal handlers for restore
        self._original_sigint: Any = None
        self._original_sigterm: Any = None

    # -----------------------------------------------------------------------
    # Discovery
    # -----------------------------------------------------------------------

    def discover_items(self) -> list[dict]:
        """Query wl to discover all work items in plan_complete stage.

        Returns:
            A list of work item dicts, or an empty list on error.
        """
        cmd = ["wl", "list", "--stage", "plan_complete", "--status", "open", "--json"]
        logger.debug("implementall.discover cmd=%s", " ".join(cmd))

        try:
            result = self.runner(cmd)
        except Exception as exc:
            logger.warning("implementall.discover.error cmd=%s exc=%s", " ".join(cmd), exc)
            return []

        if result.returncode != 0:
            logger.warning(
                "implementall.discover.failed cmd=%s rc=%s stderr=%s",
                " ".join(cmd),
                result.returncode,
                (result.stderr or "").strip(),
            )
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning(
                "implementall.discover.json_error exc=%s stdout=%s",
                exc,
                result.stdout[:500],
            )
            return []

        # wl list --json may return various shapes
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = (data.get("workItems") or data.get("work_items")
                     or data.get("items", []))
            if isinstance(items, list):
                return items
        return []

    # -----------------------------------------------------------------------
    # Implement invocation
    # -----------------------------------------------------------------------

    def _invoke_implement(self, item_id: str) -> dict:
        """Claim an item and invoke /skill:implement for it.

        Args:
            item_id: The work item ID to process.

        Returns:
            A dict with keys:
                - outcome: "implemented", "needs_input", or "error"
                - error_detail: str or None
                - recovery: dict or None (with keys action, success)
        """
        result: dict[str, Any] = {
            "outcome": "",
            "error_detail": None,
            "recovery": None,
        }

        # Claim the item
        claim_cmd = [
            "wl", "update", item_id,
            "--status", "in_progress",
            "--stage", "in_progress",
            "--json",
        ]
        logger.debug("implementall.claim cmd=%s", " ".join(claim_cmd))
        try:
            claim_result = self.runner(claim_cmd)
        except Exception as exc:
            logger.warning("implementall.claim.exception item=%s exc=%s", item_id, exc)
            result["outcome"] = "error"
            result["error_detail"] = f"Claim exception: {exc}"
            return result

        if claim_result.returncode != 0:
            stderr = (claim_result.stderr or "").strip()
            logger.warning(
                "implementall.claim.failed item=%s rc=%s stderr=%s",
                item_id, claim_result.returncode, stderr,
            )
            result["outcome"] = "error"
            result["error_detail"] = f"Claim failed (rc={claim_result.returncode}): {stderr}"
            return result

        # Invoke /skill:implement via pi
        impl_cmd = ["pi", "run", f"/skill:implement {item_id}"]
        logger.debug("implementall.implement.invoke cmd=%s", " ".join(impl_cmd))
        try:
            impl_result = self.runner(impl_cmd, timeout=self.item_timeout)
        except Exception as exc:
            logger.warning("implementall.implement.exception item=%s exc=%s", item_id, exc)
            result["outcome"] = "error"
            result["error_detail"] = f"Implement exception: {exc}"
            result["recovery"] = self._attempt_recovery(item_id)
            return result

        if impl_result.returncode != 0:
            stderr = (impl_result.stderr or "").strip()
            stdout = (impl_result.stdout or "").strip()
            logger.warning(
                "implementall.implement.failed item=%s rc=%s stderr=%s stdout=%s",
                item_id,
                impl_result.returncode,
                stderr,
                stdout[:500],
            )
            # If the implement command stopped with non-zero exit, it likely
            # indicates unanswered questions (producer input needed).
            if self._contains_questions(impl_result.stdout or ""):
                result["outcome"] = "needs_input"
                result["error_detail"] = f"Implement needs input (rc={impl_result.returncode}): {stderr}"
                return result
            # Otherwise it's an error - attempt recovery
            result["outcome"] = "error"
            result["error_detail"] = f"Implement failed (rc={impl_result.returncode}): {stderr}"
            result["recovery"] = self._attempt_recovery(item_id)
            return result

        # Check for question patterns even on zero exit
        if self._contains_questions(impl_result.stdout or ""):
            result["outcome"] = "needs_input"
            result["error_detail"] = "Implement output contains unanswered questions"
            return result

        result["outcome"] = "implemented"
        return result

    def _attempt_recovery(self, item_id: str) -> dict:
        """Attempt to recover from a failed implement by resetting the item status.

        Args:
            item_id: The work item ID to recover.

        Returns:
            A dict with keys:
                - action: description of recovery action taken
                - success: whether recovery succeeded
        """
        recovery: dict[str, Any] = {
            "action": "reset_status_to_open",
            "success": False,
        }

        if self.dry_run:
            recovery["success"] = True
            return recovery

        reset_cmd = [
            "wl", "update", item_id,
            "--status", "open",
            "--json",
        ]
        logger.debug("implementall.recovery cmd=%s", " ".join(reset_cmd))
        try:
            reset_result = self.runner(reset_cmd)
        except Exception as exc:
            logger.warning("implementall.recovery.exception item=%s exc=%s", item_id, exc)
            recovery["action"] = f"reset_status_failed: {exc}"
            return recovery

        if reset_result.returncode != 0:
            logger.warning(
                "implementall.recovery.failed item=%s rc=%s stderr=%s",
                item_id,
                reset_result.returncode,
                (reset_result.stderr or "").strip(),
            )
            recovery["action"] = f"reset_status_failed (rc={reset_result.returncode})"
            return recovery

        recovery["success"] = True
        return recovery

    @staticmethod
    def _contains_questions(text: str) -> bool:
        """Heuristic: detect if the implement output contains unanswered questions."""
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
    # Signal handling
    # -----------------------------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        """Register SIGINT and SIGTERM handlers for graceful abort."""
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _restore_signal_handlers(self) -> None:
        """Restore original signal handlers."""
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)
        if self._original_sigterm is not None:
            signal.signal(signal.SIGTERM, self._original_sigterm)
        self._original_sigint = None
        self._original_sigterm = None

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle SIGINT/SIGTERM by recovering the current item and exiting."""
        logger.warning(
            "implementall.signal received signum=%s current_item=%s",
            signum,
            self._current_item_id,
        )
        if self._current_item_id is not None:
            recovery_result = self._attempt_recovery(self._current_item_id)
            if recovery_result.get("success"):
                logger.info(
                    "implementall.signal.recovered item=%s",
                    self._current_item_id,
                )
            else:
                logger.warning(
                    "implementall.signal.recovery.failed item=%s action=%s",
                    self._current_item_id,
                    recovery_result.get("action"),
                )
        self._restore_signal_handlers()
        sys.exit(128 + signum)

    # -----------------------------------------------------------------------
    # Run all
    # -----------------------------------------------------------------------

    def run_all(self) -> list[dict]:
        """Process all plan_complete items and return results.

        Returns:
            A list of result dicts, each with keys:
                - id: work item ID
                - title: work item title (or empty string)
                - outcome: "implemented", "needs_input", or "error"
                - error_detail: str or None
                - recovery: dict or None
        """
        items = self.discover_items()
        results: list[dict] = []
        processed = 0

        self._setup_signal_handlers()
        try:
            for item in items:
                item_id = item.get("id", "")
                if not item_id:
                    continue

                # Check the max limit
                if self.max_items > 0 and processed >= self.max_items:
                    break

                title = item.get("title", "")
                self._current_item_id = item_id

                try:
                    if self.dry_run:
                        results.append({
                            "id": item_id,
                            "title": title,
                            "outcome": "implemented",
                            "error_detail": None,
                            "recovery": None,
                        })
                    else:
                        impl_result = self._invoke_implement(item_id)
                        results.append({
                            "id": item_id,
                            "title": title,
                            **impl_result,
                        })
                finally:
                    self._current_item_id = None

                processed += 1
        finally:
            self._restore_signal_handlers()

        return results

    # -----------------------------------------------------------------------
    # Summary posting
    # -----------------------------------------------------------------------

    def post_summary(self, results: list[dict],
                     parent_id: str | None = None) -> None:
        """Post the summary report to stdout and optionally as a wl comment.

        Args:
            results: The list of processing results.
            parent_id: If provided, post the summary as a comment on this item.
        """
        summary_md = _wrap_with_failure_notice_if_needed(
            generate_summary(results, json_output=False),
            results,
            script_name="implementall.py",
        )
        print(summary_md)

        if parent_id:
            comment_cmd = [
                "wl", "comment", "add", parent_id,
                "--comment", summary_md,
                "--author", "implementall",
                "--json",
            ]
            logger.debug("implementall.comment cmd=%s", " ".join(comment_cmd))
            try:
                self.runner(comment_cmd)
            except Exception as exc:
                logger.warning(
                    "implementall.comment.failed parent=%s exc=%s",
                    parent_id,
                    exc,
                )


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

def _wrap_with_failure_notice_if_needed(report: str, results: list[dict],
                                       script_name: str = "implementall.py") -> str:
    """Wrap the report with a FailureNotice if any results have errors."""
    errors = sum(1 for r in results if r.get("outcome") == "error")
    if errors == 0:
        return report
    return FailureNotice(
        script_name=script_name,
        reason=f"{errors} item(s) failed during batch implementation",
    ).wrap(report)


def generate_summary(results: list[dict], json_output: bool = False) -> str:
    """Generate a summary report from processing results.

    Args:
        results: List of result dicts with id, title, outcome, error_detail,
                 recovery keys.
        json_output: If True, produce JSON instead of Markdown.

    Returns:
        A Markdown or JSON string.
    """
    total = len(results)
    implemented = sum(1 for r in results
                      if r.get("outcome") == "implemented")
    needs_input = sum(1 for r in results
                      if r.get("outcome") == "needs_input")
    errors = sum(1 for r in results
                 if r.get("outcome") == "error")

    if json_output:
        report: dict[str, Any] = {
            "total": total,
            "implemented": implemented,
            "needs_input": needs_input,
            "errors": errors,
            "items": [
                {
                    "id": r["id"],
                    "title": r.get("title", ""),
                    "outcome": r.get("outcome", "unknown"),
                    "error_detail": r.get("error_detail"),
                    "recovery": r.get("recovery"),
                }
                for r in results
            ],
        }
        return json.dumps(report, indent=2)

    lines = [
        "# ImplementAll Summary",
        "",
        f"**Total processed**: {total}",
        f"**Implemented**: {implemented}",
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
            # Include error details if present
            error_detail = r.get("error_detail")
            if error_detail:
                lines.append(f"  - Error: {error_detail}")
            recovery = r.get("recovery")
            if recovery:
                action = recovery.get("action", "unknown")
                success = recovery.get("success", False)
                status = "✓" if success else "✗"
                lines.append(f"  - Recovery: `{action}` {status}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        description="ImplementAll: Automated batch implementation for plan_complete items",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Produce JSON output instead of Markdown",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate processing without making any changes",
    )
    parser.add_argument(
        "--parent-id",
        type=str,
        default=None,
        help="Post the summary as a comment on the specified parent work item",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Maximum number of items to process (0 = no limit)",
    )
    parser.add_argument(
        "--item-timeout",
        type=int,
        default=600,
        help="Timeout in seconds for each item's subprocess call (default: 600)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ImplementAll.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        return _main(argv)
    except Exception as exc:
        notice = FailureNotice(
            script_name="implementall.py",
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

    engine = ImplementAllEngine(
        dry_run=args.dry_run,
        max_items=args.max,
        item_timeout=args.item_timeout,
        verbose=args.verbose,
    )
    results = engine.run_all()

    if args.json:
        report = generate_summary(results, json_output=True)
        print(report)
    else:
        engine.post_summary(results, parent_id=args.parent_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
