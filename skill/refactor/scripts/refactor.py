#!/usr/bin/env python3
"""Refactor orchestration script.

Runs the refactor pipeline: session boundary detection → auto-fix
session-introduced smells → hybrid smell detection → remediation
(create work items and inject REFACTOR comments for pre-existing smells).

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
import traceback
from pathlib import Path
from typing import Any, Callable

# Add repo root to sys.path for shared utility access
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.scripts.failure_notice import FailureNotice  # noqa: E402


# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.code_review.scripts.linter_runner import probe_linter  # noqa: E402
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
# Linter fix helpers
# ---------------------------------------------------------------------------


def _build_ruff_fix_cmd(files: list[str]) -> list[str]:
    """Build the ruff check --fix command for the given files."""
    return ["ruff", "check", "--fix", "--output-format", "json", "--quiet"] + files


def _build_eslint_fix_cmd(files: list[str]) -> list[str]:
    """Build the eslint --fix command for the given files."""
    return ["eslint", "--fix", "--format", "json", "--quiet"] + files


def _parse_ruff_fix_output(raw: list[Any]) -> list[dict[str, Any]]:
    """Parse ruff --fix JSON output into standard finding dicts.

    Args:
        raw: Parsed JSON list from ruff --fix output.

    Returns:
        A list of finding dicts.
    """
    findings: list[dict[str, Any]] = []
    for item in raw:
        findings.append({
            "file": str(item.get("filename", "")),
            "line": item.get("location", {}).get("row", 0) if isinstance(
                item.get("location"), dict
            ) else 0,
            "code": str(item.get("code", "")),
            "message": item.get("message", ""),
            "severity": "low",
            "source": "linter",
            "smell_type": "unused_import" if str(item.get("code", "")).startswith("F4") else "formatting",
        })
    return findings


def _parse_eslint_fix_output(raw: list[Any]) -> list[dict[str, Any]]:
    """Parse eslint --fix JSON output into standard finding dicts.

    Args:
        raw: Parsed JSON list from eslint --fix output.

    Returns:
        A list of finding dicts.
    """
    findings: list[dict[str, Any]] = []
    for file_result in raw:
        file_path = file_result.get("filePath", "")
        messages = file_result.get("messages", [])
        if isinstance(messages, list):
            for msg in messages:
                findings.append({
                    "file": file_path,
                    "line": msg.get("line", 0),
                    "code": str(msg.get("ruleId", "")),
                    "message": msg.get("message", ""),
                    "severity": "medium",
                    "source": "linter",
                    "smell_type": "lint",
                })
    return findings


def _run_linter_fix(
    files: list[str],
    linter_name: str,
    cmd_builder: Callable[[list[str]], list[str]],
    finding_parser: Callable[[list[Any]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Probe, run, and parse results for a single auto-fix linter.

    Checks whether the linter is available, executes it with ``--fix``,
    and parses the JSON output using the provided parser.

    Args:
        files: Files to pass to the linter (already filtered by language).
        linter_name: Name for probing (e.g. ``"ruff"``, ``"eslint"``).
        cmd_builder: Callable that builds the full command list from files.
        finding_parser: Callable that parses raw JSON output into findings.

    Returns:
        A list of fixed finding dicts.
    """
    if not files:
        return []

    linter_probe = probe_linter(linter_name)
    if not linter_probe.get("available"):
        return []

    try:
        import subprocess
        cmd = cmd_builder(files)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if proc.returncode in (0, 1):
            output = proc.stdout.strip()
            if output:
                try:
                    raw = json.loads(output)
                    if isinstance(raw, list):
                        return finding_parser(raw)
                except (json.JSONDecodeError, ValueError):
                    pass
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("%s --fix failed: %s", linter_name, exc)

    return []


# ---------------------------------------------------------------------------
# Auto-fix public API
# ---------------------------------------------------------------------------


def auto_fix_files(
    files: list[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Auto-fix session-introduced smells by running linters with --fix.

    Runs auto-fixable linters (ruff --fix for Python, eslint --fix for JS/TS)
    on the given session files. Fixable issues (unused imports, formatting,
    etc.) are resolved in-place, reducing the number of smells that need work
    item creation.

    Args:
        files: List of file paths to auto-fix.
        dry_run: If True, log actions without making changes.

    Returns:
        A dict with:
          - ``fixes_applied``: True if any fixes were applied.
          - ``fixed_findings``: List of dicts describing what was fixed.
    """
    result: dict[str, Any] = {
        "fixes_applied": False,
        "fixed_findings": [],
    }

    if not files:
        return result

    if dry_run:
        LOG.info("[DRY RUN] Would auto-fix %d files with linter --fix", len(files))
        return result

    fixed_findings: list[dict[str, Any]] = []

    # Group files by type for targeted linting
    python_files = [f for f in files if f.endswith(".py")]
    js_ts_files = [f for f in files if f.endswith((".js", ".jsx", ".ts", ".tsx"))]

    # Ruff --fix for Python files
    fixed_findings.extend(
        _run_linter_fix(python_files, "ruff", _build_ruff_fix_cmd, _parse_ruff_fix_output)
    )

    # ESLint --fix for JS/TS files
    fixed_findings.extend(
        _run_linter_fix(js_ts_files, "eslint", _build_eslint_fix_cmd, _parse_eslint_fix_output)
    )

    result["fixes_applied"] = len(fixed_findings) > 0
    result["fixed_findings"] = fixed_findings

    if fixed_findings:
        LOG.info(
            "Auto-fix applied %d fixes via linters on %d files",
            len(fixed_findings),
            len(python_files) + len(js_ts_files),
        )

    return result


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
        "auto_fix": {
            "fixes_applied": False,
            "fixed_findings": [],
        },
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
            "auto_fixed": 0,
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

    # Step 2: Auto-fix session-introduced smells (before detection)
    # Run linters with --fix to resolve auto-fixable issues in-place.
    # This handles simple, mechanical issues (unused imports, formatting)
    # before the detection phase, so only non-auto-fixable smells remain.
    auto_fix_result = auto_fix_files(
        files=session["all_files"],
        dry_run=dry_run,
    )
    report["auto_fix"] = auto_fix_result
    report["summary"]["auto_fixed"] = len(auto_fix_result["fixed_findings"])

    if auto_fix_result["fixes_applied"]:
        LOG.info(
            "Auto-fixed %d issues before smell detection",
            len(auto_fix_result["fixed_findings"]),
        )

    # Step 3: Run smell detection (on auto-fixed files)
    smells = run_smell_detection(
        files=session["all_files"],
        config=config,
        no_linter=no_linter,
        no_llm=no_llm,
    )
    report["smells_detected"] = smells
    report["summary"]["total_smells"] = len(smells)

    if not smells:
        LOG.info("No code smells detected after auto-fix")
        return report

    # Step 4: Classify smells
    # After auto-fix, any remaining smells are non-auto-fixable and treated
    # as pre-existing (e.g., design/architectural smells from LLM analysis,
    # or linter issues that cannot be auto-fixed).
    report["pre_existing_smells"] = smells
    report["summary"]["pre_existing"] = len(smells)

    # Step 5: Remediate pre-existing smells
    remediation = remediate_pre_existing(
        smells=smells,
        work_item_id=None,
        dry_run=dry_run,
    )
    report["remediation"] = remediation
    report["summary"]["work_items_created"] = len(remediation["work_items_created"])
    report["summary"]["comments_injected"] = remediation["comments_injected"]

    LOG.info(
        "Refactor complete: %d auto-fixed, %d smells, %d work items, %d comments injected",
        len(auto_fix_result["fixed_findings"]),
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
    try:
        return _main(argv)
    except Exception as exc:
        notice = FailureNotice(
            script_name="refactor.py",
            reason=f"Unhandled exception: {exc}",
            stderr_context=traceback.format_exc(),
        )
        print(notice.wrap(
            f"An unexpected error occurred: {exc}"
        ))
        return 1


def _main(argv: list[str] | None = None) -> int:
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
        print(f"Auto-fixed:     {summary['auto_fixed']}")
        print(f"Remaining smells: {summary['total_smells']}")
        print(f"Pre-existing:   {summary['pre_existing']}")
        print(f"Work items:     {summary['work_items_created']}")
        print(f"Comments:       {summary['comments_injected']}")
        if report.get("auto_fix", {}).get("fixed_findings"):
            print("\nAuto-fixes applied:")
            for fix in report["auto_fix"]["fixed_findings"]:
                file_path = fix.get("file", "?")
                code = fix.get("code", "?")
                message = fix.get("message", "?")
                print(f"  [{code}] {message} in {file_path}")
        if report["smells_detected"]:
            print("\nRemaining smells (non-auto-fixable):")
            for smell in report["smells_detected"]:
                file_path = smell.get("file", "?")
                smell_type = smell.get("smell_type", "?")
                severity = smell.get("severity", "?")
                print(f"  [{severity}] {smell_type} in {file_path}")

    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
