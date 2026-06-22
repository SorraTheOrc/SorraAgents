#!/usr/bin/env python3
"""IntakeAll: Automated Batch Intake for idea-stage items.

Queries all work items in `idea` stage and runs `/intake` for each item
sequentially, auto-completing well-defined items that already have sufficient
detail. Detects items that require producer input (unanswered questions) and
produces a summary report.

Usage:
    python3 skill/intakeall/scripts/intakeall.py [--json] [--dry-run]
        [--parent-id <id>] [--verbose]

Related work item: SA-0MQK9SWN6008DWVQ
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


logger = logging.getLogger("intakeall")

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
# Sufficient-detail indicators (mirrors PlanAll v2)
# ---------------------------------------------------------------------------

SUFFICIENT_INDICATORS = [
    "Acceptance Criteria",
    "Success Criteria",
    "## Implementation",
    "## Desired Change",
    "## Proposed Approach",
]


def has_sufficient_detail(item: dict) -> bool:
    """Check if a work item has sufficient detail for auto-complete.

    Criteria (adapted from plan.md Step 0):
    - Not an epic
    - Description contains measurable acceptance criteria or success criteria
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

    # Having explicit AC is the strongest signal
    return has_ac or (has_impl and len(desc) > 500)


# ---------------------------------------------------------------------------
# IntakeAllEngine
# ---------------------------------------------------------------------------

class IntakeAllEngine:
    """Orchestrates batch intake for all idea-stage work items.

    Args:
        runner: A callable that executes shell commands. Defaults to
            subprocess.run. Tests provide a fake runner.
        dry_run: If True, log actions without making changes.
        verbose: Enable verbose logging.
    """

    def __init__(self, runner: Runner | None = None,
                 dry_run: bool = False, verbose: bool = False):
        self.runner = runner or _default_runner
        self.dry_run = dry_run
        self.verbose = verbose
        # Track the item currently being processed for signal-handler recovery
        self._current_item_id: Optional[str] = None

    # -----------------------------------------------------------------------
    # Discovery
    # -----------------------------------------------------------------------

    def discover_items(self) -> list[dict]:
        """Query wl to discover all work items in idea stage.

        Discovers items regardless of status (open, completed, in_progress,
        etc.) so that orphaned items stuck in contradictory states can be
        found and recovered before processing.

        Returns:
            A list of work item dicts, or an empty list on error.
        """
        # NOTE: No --status filter — we query ALL items in idea stage so
        # that orphaned items (e.g. completed+idea) are not invisible.
        cmd = ["wl", "list", "--stage", "idea", "--json"]
        logger.debug("intakeall.discover cmd=%s", " ".join(cmd))

        try:
            result = self.runner(cmd)
        except Exception as exc:
            logger.warning("intakeall.discover.error cmd=%s exc=%s", " ".join(cmd), exc)
            return []

        if result.returncode != 0:
            logger.warning(
                "intakeall.discover.failed cmd=%s rc=%s stderr=%s",
                " ".join(cmd),
                result.returncode,
                (result.stderr or "").strip(),
            )
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning(
                "intakeall.discover.json_error exc=%s stdout=%s",
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
    # Auto-complete
    # -----------------------------------------------------------------------

    def auto_complete(self, item: dict) -> str:
        """Auto-complete a well-defined work item to intake_complete.

        Skips /intake entirely for items with sufficient detail.

        Args:
            item: The work item dict.

        Returns:
            "completed", "skipped", or "error".
        """
        item_id = item.get("id", "")
        if not item_id:
            return "skipped"

        if not self.dry_run:
            # Claim the item first
            claim_cmd = ["wl", "update", item_id, "--status", "in_progress", "--json"]
            logger.debug("intakeall.claim cmd=%s", " ".join(claim_cmd))
            try:
                claim_result = self.runner(claim_cmd)
            except Exception as exc:
                logger.warning("intakeall.claim.error item=%s exc=%s", item_id, exc)
                return "error"

            if claim_result.returncode != 0:
                logger.warning(
                    "intakeall.claim.failed item=%s rc=%s stderr=%s",
                    item_id,
                    claim_result.returncode,
                    (claim_result.stderr or "").strip(),
                )
                return "error"

            # Mark as intake_complete
            complete_cmd = [
                "wl", "update", item_id,
                "--stage", "intake_complete",
                "--status", "open",
                "--json",
            ]
            logger.debug("intakeall.auto_complete cmd=%s", " ".join(complete_cmd))
            try:
                result = self.runner(complete_cmd)
            except Exception as exc:
                logger.warning("intakeall.auto_complete.error item=%s exc=%s",
                               item_id, exc)
                return "error"

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                logger.warning(
                    "intakeall.auto_complete.failed item=%s rc=%s stderr=%s",
                    item_id, result.returncode, stderr,
                )
                return "error"

            # Add a comment explaining the auto-complete
            comment_cmd = [
                "wl", "comment", "add", item_id,
                "--comment",
                "Intake auto-completed: work item has sufficient detail "
                "(acceptance criteria + implementation guidance) for direct implementation.",
                "--author", "intakeall",
                "--json",
            ]
            try:
                self.runner(comment_cmd)
            except Exception as exc:
                logger.warning("intakeall.comment.error item=%s exc=%s", item_id, exc)

        return "completed"

    # -----------------------------------------------------------------------
    # Intake invocation
    # -----------------------------------------------------------------------

    def _invoke_intake(self, item_id: str) -> dict:
        """Claim an item and invoke /intake for it.

        Args:
            item_id: The work item ID to process.

        Returns:
            A dict with keys:
                - outcome: "intake_completed", "needs_input", or "error"
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
        logger.debug("intakeall.claim cmd=%s", " ".join(claim_cmd))
        try:
            claim_result = self.runner(claim_cmd)
        except Exception as exc:
            logger.warning("intakeall.claim.exception item=%s exc=%s", item_id, exc)
            result["outcome"] = "error"
            result["error_detail"] = f"Claim exception: {exc}"
            return result

        if claim_result.returncode != 0:
            stderr = (claim_result.stderr or "").strip()
            logger.warning(
                "intakeall.claim.failed item=%s rc=%s stderr=%s",
                item_id, claim_result.returncode, stderr,
            )
            result["outcome"] = "error"
            result["error_detail"] = f"Claim failed (rc={claim_result.returncode}): {stderr}"
            return result

        # Invoke /intake via pi (canonical pattern: pi -p --mode json <prompt>)
        intake_cmd = ["pi", "-p", "--mode", "json", f"/intake {item_id}"]
        logger.debug("intakeall.intake.invoke cmd=%s", " ".join(intake_cmd))
        try:
            intake_result = self.runner(intake_cmd)
        except Exception as exc:
            logger.warning("intakeall.intake.exception item=%s exc=%s", item_id, exc)
            result["outcome"] = "error"
            result["error_detail"] = f"Intake exception: {exc}"
            result["recovery"] = self._attempt_recovery(item_id)
            return result

        # Extract user-facing text from pi's JSON-stream output
        raw_stdout = intake_result.stdout or ""
        intake_text = self._extract_pi_text(raw_stdout)

        if intake_result.returncode != 0:
            stderr = (intake_result.stderr or "").strip()
            logger.warning(
                "intakeall.intake.failed item=%s rc=%s stderr=%s stdout=%s",
                item_id,
                intake_result.returncode,
                stderr,
                raw_stdout[:500],
            )
            # If the intake command stopped with non-zero exit, it likely
            # indicates unanswered questions (producer input needed).
            if self._contains_questions(intake_text):
                result["outcome"] = "needs_input"
                result["error_detail"] = f"Intake needs input (rc={intake_result.returncode}): {stderr}"
                return result
            # Otherwise it's an error - attempt recovery
            result["outcome"] = "error"
            result["error_detail"] = f"Intake failed (rc={intake_result.returncode}): {stderr}"
            result["recovery"] = self._attempt_recovery(item_id)
            return result

        # Check for question patterns even on zero exit
        if self._contains_questions(intake_text):
            result["outcome"] = "needs_input"
            result["error_detail"] = "Intake output contains unanswered questions"
            return result

        # Mark the item as intake_complete
        if not self.dry_run:
            complete_cmd = [
                "wl", "update", item_id,
                "--stage", "intake_complete",
                "--status", "open",
                "--json",
            ]
            logger.debug("intakeall.intake_complete cmd=%s", " ".join(complete_cmd))
            try:
                complete_result = self.runner(complete_cmd)
                if complete_result.returncode != 0:
                    stderr = (complete_result.stderr or "").strip()
                    logger.warning(
                        "intakeall.intake_complete.failed item=%s rc=%s stderr=%s",
                        item_id, complete_result.returncode, stderr,
                    )
            except Exception as exc:
                logger.warning(
                    "intakeall.intake_complete.error item=%s exc=%s",
                    item_id, exc,
                )

        result["outcome"] = "intake_completed"
        return result

    def _attempt_recovery(self, item_id: str) -> dict:
        """Attempt to recover from a failed intake by resetting the item status.

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
            "--stage", "idea",
            "--status", "open",
            "--json",
        ]
        logger.debug("intakeall.recovery cmd=%s", " ".join(reset_cmd))
        try:
            reset_result = self.runner(reset_cmd)
        except Exception as exc:
            logger.warning("intakeall.recovery.exception item=%s exc=%s", item_id, exc)
            recovery["action"] = f"reset_status_failed: {exc}"
            return recovery

        if reset_result.returncode != 0:
            logger.warning(
                "intakeall.recovery.failed item=%s rc=%s stderr=%s",
                item_id,
                reset_result.returncode,
                (reset_result.stderr or "").strip(),
            )
            recovery["action"] = f"reset_status_failed (rc={reset_result.returncode})"
            return recovery

        recovery["success"] = True
        return recovery

    @staticmethod
    def _extract_pi_text(raw: str) -> str:
        """Extract user-facing text from pi --mode json JSON-stream output.

        Parses JSON lines looking for assistant message content (text_delta,
        text_end, message_end, turn_end events). Returns the last complete
        block of text found, or accumulated delta text if no complete blocks.

        Mirrors the canonical pattern from skill/audit/scripts/audit_runner.py.
        """
        delta_parts: list[str] = []
        complete_blocks: list[str] = []

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue

            event_type = obj.get("type", "")

            if event_type == "message_update":
                assistant = obj.get("assistantMessageEvent")
                if isinstance(assistant, dict):
                    inner = assistant.get("type", "")
                    if inner == "text_delta":
                        delta = assistant.get("delta", "")
                        if delta:
                            delta_parts.append(delta)
                    elif inner == "text_end":
                        content = assistant.get("content", "")
                        if content:
                            complete_blocks.append(content)

            if event_type in ("message_end", "turn_end"):
                message = obj.get("message")
                if isinstance(message, dict):
                    content = message.get("content", "")
                    if content:
                        complete_blocks.append(content)
                    for part in (message.get("parts") or []):
                        if isinstance(part, dict):
                            part_text = part.get("text", "") or part.get("content", "")
                            if part_text:
                                complete_blocks.append(part_text)

        if complete_blocks:
            return complete_blocks[-1]
        return "".join(delta_parts)

    @staticmethod
    def _contains_questions(text: str) -> bool:
        """Heuristic: detect if the intake output contains unanswered questions."""
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
    # Orphan recovery
    # -----------------------------------------------------------------------

    def _recover_orphans(self, items: list[dict]) -> list[dict]:
        """Reset orphaned items in idea stage to open status.

        Items stuck in contradictory states (e.g. status=completed/
        in_progress while stage=idea) are reset to status=open so they
        can be discovered and processed on subsequent runs.

        Orphan detection is resilient:
        - If wl rejects the status transition (e.g. completed->open),
          the error is logged and the item is still included in the
          returned list with its in-memory status updated to open.
        - Items already at status=open pass through unchanged.
        - During dry-run, no actual wl calls are made.

        Args:
            items: List of work item dicts from discover_items().

        Returns:
            The same list with orphan statuses reset to 'open' in memory.
        """
        remaining: list[dict] = []
        for item in items:
            status = item.get("status", "")
            stage = item.get("stage", "")
            if status in ("completed", "in_progress") and stage == "idea":
                result = self._attempt_recovery(item["id"])
                logger.info(
                    "intakeall.orphan_recovery item=%s success=%s action=%s",
                    item["id"],
                    result.get("success", False),
                    result.get("action", "unknown"),
                )
                # Update in-memory status regardless of wl success
                item["status"] = "open"
            remaining.append(item)
        return remaining

    # -----------------------------------------------------------------------
    # Signal handling
    # -----------------------------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        """Register signal handlers for graceful abort on SIGINT/SIGTERM.

        When a signal is caught while processing an item, the handler
        attempts to recover that item (reset to status=open, stage=idea)
        before exiting.
        """
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _restore_signal_handlers(self) -> None:
        """Restore original signal handlers."""
        if hasattr(self, "_original_sigint"):
            signal.signal(signal.SIGINT, self._original_sigint)
        if hasattr(self, "_original_sigterm"):
            signal.signal(signal.SIGTERM, self._original_sigterm)

    def _signal_handler(self, signum: int, _frame: Any) -> None:
        """Handle abort signals by recovering the current item.

        Args:
            signum: Signal number received.
            _frame: Current stack frame (unused).
        """
        if self._current_item_id:
            logger.warning(
                "intakeall.signal received=%s recovering item=%s",
                signum,
                self._current_item_id,
            )
            self._attempt_recovery(self._current_item_id)
        raise SystemExit(128 + signum)

    # -----------------------------------------------------------------------
    # Run all
    # -----------------------------------------------------------------------

    def run_all(self) -> list[dict]:
        """Process all idea-stage items and return results.

        Pre-processing:
        1. Discover ALL items in idea stage (including orphans)
        2. Recover orphaned items (reset status=completed/in_progress
           to status=open)

        For each item:
        1. Check if it has sufficient detail for auto-complete
        2. If yes, auto-complete to intake_complete (skip /intake)
        3. If no, invoke /intake and classify the outcome

        Signal handlers are registered so that an external abort
        (SIGINT/SIGTERM) triggers recovery for the in-progress item.

        Returns:
            A list of result dicts, each with keys:
                - id: work item ID
                - title: work item title (or empty string)
                - outcome: "auto_completed", "intake_completed",
                  "needs_input", or "error"
                - error_detail: str or None
                - recovery: dict or None
        """
        items = self.discover_items()

        # Recover orphans before processing
        items = self._recover_orphans(items)

        # Register signal handlers for graceful abort
        self._setup_signal_handlers()

        results: list[dict] = []

        try:
            for item in items:
                item_id = item.get("id", "")
                if not item_id:
                    continue

                # Track current item for signal-handler recovery
                self._current_item_id = item_id

                title = item.get("title", "")

                # Check if item can be auto-completed
                if has_sufficient_detail(item):
                    if self.dry_run:
                        outcome = "auto_completed"
                        result: dict[str, Any] = {
                            "id": item_id,
                            "title": title,
                            "outcome": outcome,
                            "error_detail": None,
                            "recovery": None,
                        }
                    else:
                        ac_result = self.auto_complete(item)
                        if ac_result == "completed":
                            result = {
                                "id": item_id,
                                "title": title,
                                "outcome": "auto_completed",
                                "error_detail": None,
                                "recovery": None,
                            }
                        else:
                            result = {
                                "id": item_id,
                                "title": title,
                                "outcome": "error",
                                "error_detail": f"Auto-complete failed: {ac_result}",
                                "recovery": None,
                            }
                else:
                    # Item needs /intake
                    if self.dry_run:
                        result = {
                            "id": item_id,
                            "title": title,
                            "outcome": "intake_completed",
                            "error_detail": None,
                            "recovery": None,
                        }
                    else:
                        intake_result = self._invoke_intake(item_id)
                        result = {
                            "id": item_id,
                            "title": title,
                            **intake_result,
                        }

                results.append(result)

                # Clear current item now that it's done
                self._current_item_id = None
        finally:
            self._restore_signal_handlers()
            self._current_item_id = None

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
            script_name="intakeall.py",
        )
        print(summary_md)

        if parent_id:
            comment_cmd = [
                "wl", "comment", "add", parent_id,
                "--comment", summary_md,
                "--author", "intakeall",
                "--json",
            ]
            logger.debug("intakeall.comment cmd=%s", " ".join(comment_cmd))
            try:
                self.runner(comment_cmd)
            except Exception as exc:
                logger.warning(
                    "intakeall.comment.failed parent=%s exc=%s",
                    parent_id,
                    exc,
                )


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

def _wrap_with_failure_notice_if_needed(report: str, results: list[dict],
                                       script_name: str = "intakeall.py") -> str:
    """Wrap the report with a FailureNotice if any results have errors."""
    errors = sum(1 for r in results if r.get("outcome") == "error")
    if errors == 0:
        return report
    return FailureNotice(
        script_name=script_name,
        reason=f"{errors} item(s) failed during batch intake",
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
    auto_completed = sum(1 for r in results
                         if r.get("outcome") == "auto_completed")
    intake_completed = sum(1 for r in results
                           if r.get("outcome") == "intake_completed")
    needs_input = sum(1 for r in results
                      if r.get("outcome") == "needs_input")
    errors = sum(1 for r in results
                 if r.get("outcome") == "error")

    if json_output:
        report: dict[str, Any] = {
            "total": total,
            "auto_completed": auto_completed,
            "intake_completed": intake_completed,
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
        "# IntakeAll Summary",
        "",
        f"**Total processed**: {total}",
        f"**Auto-completed**: {auto_completed}",
        f"**Intake completed**: {intake_completed}",
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
        description="IntakeAll: Automated batch intake for idea-stage items",
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
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for IntakeAll.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        return _main(argv)
    except Exception as exc:
        notice = FailureNotice(
            script_name="intakeall.py",
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

    engine = IntakeAllEngine(
        dry_run=args.dry_run,
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
