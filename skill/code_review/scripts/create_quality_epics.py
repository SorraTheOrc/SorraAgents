#!/usr/bin/env python3
"""Work Item Creator for code quality findings.

Creates or reuses a "Quality Improvement - Refactoring" epic and adds child
task items for each code quality finding, properly prioritised by severity.

Usage:
  python3 -m skill.code_review.scripts.create_quality_epics \\
      --findings '<json-array>' [--project-root <path>] [--dry-run]

Exit codes:
  0 – success
  1 – internal error or wl failure
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence, Union

# Ensure the package is importable when run as __main__
_SCRIPT_DIR = Path(__file__).resolve().parent
_PACKAGE_ROOT = _SCRIPT_DIR.parent.parent.parent  # repo root
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EPIC_TITLE = "Quality Improvement - Refactoring"
SEVERITY_TO_PRIORITY: dict[str, str] = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
}

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# A runner is a callable that accepts a list of strings (command)
# and returns a CompletedProcess-like object.
Runner = Callable[[list[str]], Any]

# ---------------------------------------------------------------------------
# Default runner
# ---------------------------------------------------------------------------


def _default_runner(cmd: list[str]) -> Any:
    """Default subprocess runner."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=-1,
            stdout="", stderr="Timed out",
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=cmd, returncode=-1,
            stdout="", stderr=f"Binary not found: {cmd[0]}",
        )


def _run_wl(runner: Runner, cmd: list[str]) -> dict[str, Any]:
    """Run a ``wl`` command and return parsed JSON."""
    proc = runner(cmd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed ({' '.join(cmd)}): {proc.stderr.strip()}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from wl: {exc}") from exc
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            f"Worklog command failed: {data.get('error', 'unknown error')}"
        )
    return data


# ---------------------------------------------------------------------------
# Epic management
# ---------------------------------------------------------------------------


def _finding_title(finding: dict[str, Any]) -> str:
    """Generate a unique title for a finding work item.

    Format: ``[SEVERITY] file:line — message (code)``
    """
    sev = finding.get("severity", "medium").upper()
    file_ = finding.get("file", "?")
    line = finding.get("line", 0)
    msg = finding.get("message", "")
    code = finding.get("code", "")
    return f"[{sev}] {file_}:{line} — {msg} ({code})"


def _severity_to_priority(severity: str) -> str:
    """Map a finding severity to a work item priority."""
    return SEVERITY_TO_PRIORITY.get(severity, "medium")


def find_or_create_epic(
    runner: Runner,
) -> tuple[str, bool]:
    """Find an existing 'Quality Improvement - Refactoring' epic or create one.

    Args:
        runner: Subprocess runner injection.

    Returns:
        A tuple of (epic_id, was_created).
        was_created is True if a new epic was created, False if reused.
    """
    # Search for existing epic with open/in_progress status
    try:
        search_result = _run_wl(runner, [
            "wl", "search", EPIC_TITLE, "--json",
        ])
    except RuntimeError as exc:
        print(f"Warning: wl search failed: {exc}", file=sys.stderr)
        search_result = {}  # Continue and create new epic

    work_items = search_result.get("workItems", [])
    if isinstance(work_items, dict):
        work_items = [work_items]

    # Look for an open or in_progress epic matching the title exactly
    for item in work_items:
        if isinstance(item, dict):
            title = (item.get("title") or item.get("name") or "").strip()
            status = (item.get("status") or "").lower()
            if title == EPIC_TITLE and status in ("open", "in_progress"):
                epic_id = item.get("id", "")
                if epic_id:
                    return epic_id, False

    # Create new epic
    create_result = _run_wl(runner, [
        "wl", "create",
        "--title", EPIC_TITLE,
        "--description", (
            "Quality Improvement epic for tracking code quality findings "
            "discovered during automated code review."
        ),
        "--issue-type", "epic",
        "--priority", "medium",
        "--json",
    ])

    work_item = create_result.get("workItem", {})
    epic_id = work_item.get("id", "")
    if not epic_id:
        raise RuntimeError("Failed to create epic: no ID returned")

    return epic_id, True


def create_child_tasks(
    epic_id: str,
    findings: list[dict[str, Any]],
    runner: Runner,
    existing_titles: set[str] | None = None,
) -> int:
    """Create child work items under *epic_id* for each finding.

    Args:
        epic_id: The epic work item ID.
        findings: List of code quality finding dicts.
        runner: Subprocess runner injection.
        existing_titles: Optional set of already-existing child task titles
                         to avoid duplicates.

    Returns:
        The number of child tasks created.
    """
    if existing_titles is None:
        existing_titles = set()

    created = 0

    for finding in findings:
        title = _finding_title(finding)

        # Idempotency check: skip if title already exists
        if title in existing_titles:
            continue

        severity = finding.get("severity", "medium")
        priority = _severity_to_priority(severity)

        description_lines = [
            f"## Code quality finding",
            f"",
            f"- **Severity**: {severity}",
            f"- **File**: {finding.get('file', '?')}",
            f"- **Line**: {finding.get('line', 0)}",
            f"- **Message**: {finding.get('message', '')}",
            f"- **Linter**: {finding.get('linter', '?')}",
            f"- **Code**: {finding.get('code', '')}",
            f"",
            f"Discovered during automated code quality review.",
        ]
        description = "\n".join(description_lines)

        try:
            _run_wl(runner, [
                "wl", "create",
                "--parent", epic_id,
                "--title", title,
                "--description", description,
                "--issue-type", "task",
                "--priority", priority,
                "--tags", "Refactor",
                "--json",
            ])
            created += 1
            existing_titles.add(title)
        except RuntimeError as exc:
            print(
                f"Warning: failed to create child task for finding '{title}': "
                f"{exc}",
                file=sys.stderr,
            )

    return created


def get_existing_child_titles(
    epic_id: str,
    runner: Runner,
) -> set[str]:
    """Get the set of existing child task titles under *epic_id*.

    Used for idempotency checks.
    """
    try:
        result = _run_wl(runner, [
            "wl", "show", epic_id, "--children", "--json",
        ])
    except RuntimeError:
        return set()

    children = result.get("children", [])
    if isinstance(children, dict):
        children = [children]

    titles: set[str] = set()
    for child in children:
        if isinstance(child, dict):
            title = (child.get("title") or "").strip()
            if title:
                titles.add(title)

    return titles


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def create_epics_for_findings(
    findings: list[dict[str, Any]],
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Create or reuse a Quality Improvement epic and add child tasks.

    Args:
        findings: List of finding dicts (must have ``severity``, ``file``,
                  ``line``, ``message``, ``linter``, ``code`` keys).
        runner: Optional injectable subprocess runner.

    Returns:
        A dict with keys:
          - ``epic_id``: the epic work item ID
          - ``epic_created``: bool — True if a new epic was created
          - ``children_created``: int — number of child tasks created
    """
    if runner is None:
        runner = _default_runner

    # 1. Find or create epic
    epic_id, was_created = find_or_create_epic(runner)

    # 2. Get existing child titles for idempotency
    existing_titles = get_existing_child_titles(epic_id, runner)

    # 3. Create child tasks
    children_created = create_child_tasks(
        epic_id, findings, runner, existing_titles=existing_titles,
    )

    return {
        "epic_id": epic_id,
        "epic_created": was_created,
        "children_created": children_created,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Create or reuse a Quality Improvement epic for findings.",
    )
    p.add_argument(
        "--findings",
        required=True,
        help="JSON string of findings array",
    )
    p.add_argument(
        "--project-root",
        default=None,
        help="Project root directory (default: cwd, used for context)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making changes",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Parse findings JSON
    try:
        findings = json.loads(args.findings)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --findings JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(findings, list):
        print("Error: --findings must be a JSON array", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"Dry run: would process {len(findings)} findings")
        for f in findings:
            print(f"  - {_finding_title(f)}")
        print(json.dumps({"epic_id": "(dry-run)", "children_created": len(findings)}, indent=2))
        return 0

    try:
        result = create_epics_for_findings(findings)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(main())
