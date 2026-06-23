#!/usr/bin/env python3
"""Work Item Creator for code quality findings.

Creates or reuses a "Quality Improvement - Refactoring" epic and adds child
task items for each code quality finding, properly prioritised by severity.

Newly created epics and child tasks are created at stage ``intake_complete``
so that they are ready for planning without manual intake.

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
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

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

# Priority order from highest to lowest
PRIORITY_ORDER = ["critical", "high", "medium", "low"]

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


def _highest_priority(findings: list[dict[str, Any]]) -> str:
    """Return the highest priority among findings.

    Priority order: critical > high > medium > low.
    Returns ``medium`` for empty findings.
    """
    if not findings:
        return "medium"
    highest_idx = len(PRIORITY_ORDER)  # sentinel: higher than any valid index
    for f in findings:
        sev = f.get("severity", "medium")
        pri = _severity_to_priority(sev)
        if pri in PRIORITY_ORDER:
            idx = PRIORITY_ORDER.index(pri)
            if idx < highest_idx:
                highest_idx = idx
    # If no finding had a valid priority, default to medium
    if highest_idx >= len(PRIORITY_ORDER):
        return "medium"
    return PRIORITY_ORDER[highest_idx]


def find_or_create_epic(
    runner: Runner,
    priority: str = "medium",
) -> tuple[str, bool]:
    """Find an existing 'Quality Improvement - Refactoring' epic or create one.

    Uses ``wl list`` (not ``wl search``) because ``wl search`` does not reliably
    find matching epics (known issue).  Queries for open/in-progress items,
    filters by exact title and issueType="epic", and picks the oldest by
    creation date if multiple are found.

    When creating a new epic, the *priority* parameter is used as the epic's
    priority (by default "medium").

    Args:
        runner: Subprocess runner injection.
        priority: Priority to use when creating a new epic.

    Returns:
        A tuple of (epic_id, was_created).
        was_created is True if a new epic was created, False if reused.
    """
    # Use wl list instead of wl search — wl search may return 0 results
    # even when matching epics exist.
    for status_filter in ("open", "in_progress"):
        try:
            list_result = _run_wl(runner, [
                "wl", "list", "--status", status_filter, "--json",
            ])
        except RuntimeError as exc:
            print(f"Warning: wl list --status {status_filter} failed: {exc}", file=sys.stderr)
            continue

        work_items = list_result.get("workItems", [])
        if isinstance(work_items, dict):
            work_items = [work_items]

        # Filter for epics matching the exact title
        matching: list[dict[str, Any]] = []
        for item in work_items:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            issue_type = (item.get("issueType") or "").lower()
            if title == EPIC_TITLE and issue_type == "epic":
                matching.append(item)

        if matching:
            # Pick the oldest epic by creation date (defensive against
            # duplicates caused by previous script bugs).
            def _created_at(item: dict[str, Any]) -> str:
                return item.get("createdAt") or ""
            matching.sort(key=_created_at)
            epic_id = matching[0].get("id", "")
            if epic_id:
                if len(matching) > 1:
                    # Log a warning so operators know duplicates exist
                    extra_ids = [m.get("id", "?") for m in matching[1:]]
                    print(
                        f"Warning: found {len(matching)} open/in-progress epics "
                        f"with title '{EPIC_TITLE}'. Using oldest ({epic_id}). "
                        f"Duplicate IDs: {', '.join(extra_ids)}",
                        file=sys.stderr,
                    )
                return epic_id, False

    # Create new epic with the computed priority
    create_result = _run_wl(runner, [
        "wl", "create",
        "--title", EPIC_TITLE,
        "--description", (
            "Quality Improvement epic for tracking code quality findings "
            "discovered during automated code review. "
            "Closed when all child work items are resolved; "
            "a new epic is created if new findings arrive after closure."
        ),
        "--issue-type", "epic",
        "--priority", priority,
        "--stage", "intake_complete",
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
            "## Code quality finding",
            "",
            f"- **Severity**: {severity}",
            f"- **File**: {finding.get('file', '?')}",
            f"- **Line**: {finding.get('line', 0)}",
            f"- **Message**: {finding.get('message', '')}",
            f"- **Linter**: {finding.get('linter', '?')}",
            f"- **Code**: {finding.get('code', '')}",
            "",
            "Discovered during automated code quality review.",
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
                "--stage", "intake_complete",
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


def _update_epic_priority(
    epic_id: str,
    new_priority: str,
    runner: Runner,
) -> None:
    """Update the epic's priority if *new_priority* is higher than the current.

    Priority escalation only: the epic's priority is never reduced.
    Current priority is retrieved via ``wl show <epic_id> --json``.

    Args:
        epic_id: The epic work item ID.
        new_priority: The desired priority to escalate to.
        runner: Subprocess runner injection.
    """
    try:
        show_result = _run_wl(runner, [
            "wl", "show", epic_id, "--json",
        ])
    except RuntimeError:
        return  # If we can't read the epic, skip the update

    work_item = show_result.get("workItem", {})
    current_priority = work_item.get("priority", "medium")

    # Only escalate: never reduce priority
    if new_priority in PRIORITY_ORDER and current_priority in PRIORITY_ORDER:
        if PRIORITY_ORDER.index(new_priority) < PRIORITY_ORDER.index(current_priority):
            try:
                _run_wl(runner, [
                    "wl", "update", epic_id,
                    "--priority", new_priority,
                    "--json",
                ])
            except RuntimeError as exc:
                print(
                    f"Warning: failed to update epic {epic_id} priority "
                    f"to {new_priority}: {exc}",
                    file=sys.stderr,
                )


def create_epics_for_findings(
    findings: list[dict[str, Any]],
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Create or reuse a Quality Improvement epic and add child tasks.

    When creating a new epic, the epic's priority is set to the highest
    severity among its child findings (critical > high > medium > low).
    When reusing an existing epic and new child tasks are created, the
    epic's priority is escalated to match the highest severity (priority
    escalation only — never reduced).

    Args:
        findings: List of finding dicts (must have ``severity``, ``file``,
                  ``line``, ``message``, ``linter``, ``code`` keys).
        runner: Optional injectable subprocess runner.

    Returns:
        A dict with keys:
          - ``epic_id``: the epic work item ID
          - ``epic_created``: bool — True if a new epic was created
          - ``children_created``: int — number of child tasks created
          - ``epic_priority``: str — the priority assigned to the epic
    """
    if runner is None:
        runner = _default_runner

    # 0. Compute the highest priority from findings
    highest_priority = _highest_priority(findings)

    # 1. Find or create epic (pass the computed priority for new epics)
    epic_id, was_created = find_or_create_epic(runner, priority=highest_priority)

    # 2. Get existing child titles for idempotency
    existing_titles = get_existing_child_titles(epic_id, runner)

    # 3. Create child tasks
    children_created = create_child_tasks(
        epic_id, findings, runner, existing_titles=existing_titles,
    )

    # 4. If reusing an existing epic and new children were created,
    #    escalate the epic's priority if the computed priority is higher
    if not was_created and children_created > 0:
        _update_epic_priority(epic_id, highest_priority, runner)

    return {
        "epic_id": epic_id,
        "epic_created": was_created,
        "children_created": children_created,
        "epic_priority": highest_priority,
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
        computed_priority = _highest_priority(findings)
        print(f"Dry run: would process {len(findings)} findings (stage: intake_complete)")
        print(f"Computed epic priority: {computed_priority}")
        for f in findings:
            print(f"  - {_finding_title(f)}")
        print(json.dumps({
            "epic_id": "(dry-run)",
            "children_created": len(findings),
            "epic_priority": computed_priority,
            "stage": "intake_complete",
        }, indent=2))
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
