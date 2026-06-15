#!/usr/bin/env python3
"""Refactor orchestration script.

Runs the refactor pipeline: session boundary detection → hybrid smell
detection → remediation (fix session-introduced smells, create work items
and inject REFACTOR comments for pre-existing smells).

Usage:
  refactor.py                          # Auto-detect session, run all
  refactor.py <work-item-id>           # Explicit work item context
  refactor.py --dry-run                # Show what would be changed
  refactor.py --json                   # JSON output for agents
  refactor.py --no-llm                 # Linter only
  refactor.py --no-linter              # LLM only

Exit codes:
  0 – success (no smells or all handled)
  1 – error during execution
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.refactor.session_boundary import get_changed_files, get_untracked_files  # noqa: E402
from skill.refactor.smell_detection import detect_smells, load_rules  # noqa: E402
from skill.refactor.workitem_creation import create_smell_work_items  # noqa: E402
from skill.refactor.comment_injection import inject_refactor_comment  # noqa: E402


LOG = logging.getLogger("refactor.scripts.refactor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PARENT_BRANCH = "dev"


# ---------------------------------------------------------------------------
# Refactor Pipeline
# ---------------------------------------------------------------------------


def detect_session_files(parent_branch: str) -> dict[str, Any]:
    """Detect files modified in the current session.

    Args:
        parent_branch: The parent branch to diff against.

    Returns:
        A dict with ``changed``, ``untracked``, and ``all_files`` lists.
    """
    changed = get_changed_files(parent_branch=parent_branch)
    untracked = get_untracked_files()
    all_files: list[str] = []

    for entry in changed:
        all_files.append(entry["file"])
    all_files.extend(untracked)

    return {
        "changed": changed,
        "untracked": untracked,
        "all_files": all_files,
    }


def run_smell_detection(
    files: list[str],
    config: dict[str, Any] | None,
    no_linter: bool = False,
    no_llm: bool = False,
) -> list[dict[str, Any]]:
    """Run hybrid smell detection on a list of files.

    Args:
        files: List of file paths to analyze.
        config: Optional configuration dict.
        no_linter: If True, skip linter detection.
        no_llm: If True, skip LLM detection.

    Returns:
        A list of smell finding dicts.
    """
    if not files:
        return []

    mode = "hybrid"
    if no_linter and not no_llm:
        mode = "llm"
    elif no_llm and not no_linter:
        mode = "linter"
    elif no_linter and no_llm:
        LOG.warning("Both linter and LLM disabled; no detection will run")
        return []

    rules = config or load_rules()

    return detect_smells(
        files=files,
        mode=mode,
        rules=rules,
        llm_client=None,  # Will be injected by the caller when available
    )


def remediate_pre_existing(
    smells: list[dict[str, Any]],
    work_item_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create work items and inject REFACTOR comments for pre-existing smells.

    Args:
        smells: List of smell finding dicts identified as pre-existing.
        work_item_id: Optional work item ID for context (for logging).
        dry_run: If True, log actions without executing.

    Returns:
        A dict with ``work_items_created`` list and ``comments_injected`` count.
    """
    result: dict[str, Any] = {
        "work_items_created": [],
        "comments_injected": 0,
        "comment_errors": 0,
    }

    if not smells:
        return result

    if dry_run:
        LOG.info(
            "[DRY RUN] Would create %d work items and inject %d comments",
            len(smells),
            len(smells),
        )
        return result

    # Create work items for pre-existing smells
    created_ids = create_smell_work_items(smells)
    result["work_items_created"] = created_ids

    # Inject REFACTOR comments for successfully created work items
    for smell in smells:
        file_path = smell.get("file", "")
        if not file_path:
            continue

        # Find the matching work item ID (by index)
        smell_index = smells.index(smell)
        smell_work_item_id = (
            created_ids[smell_index]
            if smell_index < len(created_ids)
            else None
        )

        if not smell_work_item_id:
            LOG.warning(
                "No work item ID for smell %s in %s; skipping comment",
                smell.get("smell_type", "unknown"),
                file_path,
            )
            result["comment_errors"] += 1
            continue

        success = inject_refactor_comment(file_path, smell, smell_work_item_id)
        if success:
            result["comments_injected"] += 1
        else:
            LOG.warning(
                "Failed to inject comment for %s in %s",
                smell.get("smell_type", "unknown"),
                file_path,
            )
            result["comment_errors"] += 1

    return result


def refactor_pipeline(
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    config: dict[str, Any] | None = None,
    no_linter: bool = False,
    no_llm: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the full refactor pipeline.

    Args:
        parent_branch: Parent branch for git diff.
        config: Optional configuration dict.
        no_linter: Skip linter detection.
        no_llm: Skip LLM detection.
        dry_run: Show what would be changed without making changes.

    Returns:
        A dict with the full refactor report.
    """
    report: dict[str, Any] = {
        "success": True,
        "session_files": {"changed": [], "untracked": [], "all_files": []},
        "smells_detected": [],
        "pre_existing_smells": [],
        "session_introduced_smells": [],
        "remediation": {
            "work_items_created": [],
            "comments_injected": 0,
            "comment_errors": 0,
        },
        "summary": {
            "files_analyzed": 0,
            "total_smells": 0,
            "session_introduced": 0,
            "pre_existing": 0,
            "work_items_created": 0,
            "comments_injected": 0,
        },
    }

    # Step 1: Detect session files
    session = detect_session_files(parent_branch)
    report["session_files"] = session

    if not session["all_files"]:
        report["summary"]["files_analyzed"] = 0
        LOG.info("No files modified in current session; nothing to analyze")
        return report

    LOG.info(
        "Session files: %d changed, %d untracked",
        len(session["changed"]),
        len(session["untracked"]),
    )
    report["summary"]["files_analyzed"] = len(session["all_files"])

    # Step 2: Run smell detection
    smells = run_smell_detection(
        files=session["all_files"],
        config=config,
        no_linter=no_linter,
        no_llm=no_llm,
    )
    report["smells_detected"] = smells
    report["summary"]["total_smells"] = len(smells)

    if not smells:
        LOG.info("No code smells detected")
        return report

    # Step 3: Classify smells
    # For now, all detected smells are treated as pre-existing since we lack
    # a reliable mechanism to distinguish session-introduced from pre-existing
    # at the per-smell level. Future improvements can diff against the parent
    # branch's lint output.
    report["pre_existing_smells"] = smells
    report["summary"]["pre_existing"] = len(smells)

    # Step 4: Remediate pre-existing smells
    remediation = remediate_pre_existing(
        smells=smells,
        work_item_id=None,
        dry_run=dry_run,
    )
    report["remediation"] = remediation
    report["summary"]["work_items_created"] = len(remediation["work_items_created"])
    report["summary"]["comments_injected"] = remediation["comments_injected"]

    LOG.info(
        "Refactor complete: %d smells, %d work items created, %d comments injected",
        len(smells),
        len(remediation["work_items_created"]),
        remediation["comments_injected"],
    )

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Refactor skill: detect and remediate code smells",
    )
    parser.add_argument(
        "work_item_id",
        nargs="?",
        default=None,
        help="Work item ID for context (optional)",
    )
    parser.add_argument(
        "--parent-branch",
        default=DEFAULT_PARENT_BRANCH,
        help=f"Parent branch for git diff (default: {DEFAULT_PARENT_BRANCH})",
    )
    parser.add_argument(
        "--no-linter",
        action="store_true",
        help="Skip linter-based detection",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM-based detection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without making changes",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to custom .refactor.json config file",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the refactor orchestration."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # Load config if specified
    config = None
    if args.config:
        config_path = Path(args.config)
        if config_path.is_file():
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                LOG.warning("Failed to load config %s: %s", args.config, exc)

    # Run the pipeline
    report = refactor_pipeline(
        parent_branch=args.parent_branch,
        config=config,
        no_linter=args.no_linter,
        no_llm=args.no_llm,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        summary = report["summary"]
        print("=== Refactor Report ===")
        print(f"Files analyzed: {summary['files_analyzed']}")
        print(f"Total smells:   {summary['total_smells']}")
        print(f"Pre-existing:   {summary['pre_existing']}")
        print(f"Work items:     {summary['work_items_created']}")
        print(f"Comments:       {summary['comments_injected']}")
        if report["smells_detected"]:
            print("\nSmells detected:")
            for smell in report["smells_detected"]:
                file_path = smell.get("file", "?")
                smell_type = smell.get("smell_type", "?")
                severity = smell.get("severity", "?")
                print(f"  [{severity}] {smell_type} in {file_path}")

    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
