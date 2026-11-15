#!/usr/bin/env python3
"""One-off duplicate consolidation for refactor-filed code smells.

The refactor skill's identity-based dedupe (SA-0MWE1E4GQ004E1JG) stops NEW
duplicates, but the backlog accumulated while dedupe was a substring match
still holds clones (e.g. 22 open ``S112`` items for one file). This tool
collapses that backlog using the same identity helper.

The identity of a refactor item is ``<code>:<repository-relative path>``,
carried in ``Refactor-Finding-Key:`` markers. Items sharing an identity are
grouped; the canonical is the oldest open item; the rest are duplicates.

Usage (dry-run is the DEFAULT — an accidental run is harmless):

    python3 skill/refactor/scripts/consolidate_duplicates.py          # dry run
    python3 skill/refactor/scripts/consolidate_duplicates.py --apply  # collapse
    python3 skill/refactor/scripts/consolidate_duplicates.py --json   # machine output

On ``--apply``:

- the canonical item keeps the **union** of every group member's affected
  lines (``Refactor-Affected-Lines`` rewritten) so no diagnostic detail is
  lost;
- each duplicate gets a ``superseded by <canonical-id> — duplicate identity``
  comment and is closed with the same recorded reason;
- audit-created false-positive chores (``ruff <RULE> in <path> — candidate
  false positive``) that twin a consolidated identity are reported and
  cross-referenced (``Refactor-Twin`` comment), NEVER auto-closed — they carry
  ``needsProducerReview`` by design.

Re-running after ``--apply`` is idempotent: closed duplicates are terminal and
no longer form groups.

Related work items:
- SA-0MWE1ELZO004J1DY (this tool)
- SA-0MW7Z1AU1006UT9C (parent — dedupe substring matching files smells repeatedly)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Add ``skill/`` to sys.path so ``refactor.*`` imports resolve both when this
# module is imported as ``refactor.scripts.consolidate_duplicates`` (tests)
# and when it is run directly from the repository (CLI). Mirrors the
# bootstrap in ``refactor/scripts/refactor.py``.
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

LOG = logging.getLogger("refactor.consolidate_duplicates")

# Audit-family title marker — mirrors refactor.workitem_creation.
AUDIT_FP_TITLE_MARKER = "candidate false positive"
_REFACTOR_KEY_RE = re.compile(
    r"Refactor-Finding-Key:[ \t]*(\S+)", re.IGNORECASE
)
REFACTOR_KEY_MARKER = "Refactor-Finding-Key:"
_AFFECTED_LINES_RE = re.compile(
    r"Refactor-Affected-Lines:[ \t]*([0-9][0-9, \t]*)", re.IGNORECASE
)

# Legacy (pre-S0-0MWE1E4GQ004E1JG) structured fields still present in the
# duplicate backlog this tool consolidates. Both the refactor-skill field
# style (``**Code:** ``) and the code-quality-review style (``**Code**: ``)
# are matched.
_LEGACY_CODE_RE = re.compile(r"\*\*Code\*\*\s*[:\s]+`?([^`\n]+)`?")
_LEGACY_SMELL_RE = re.compile(r"^## Code Smell:\s*(\S+)", re.MULTILINE)
_LEGACY_FILE_RE = re.compile(r"\*\*File\*\*\s*[:\s]+`?([^`\n]+)`?")
_TITLE_IN_RE = re.compile(r"\bin\s+(.+?)\s*$", re.IGNORECASE)

# Code-quality finding titles: ``[SEVERITY] /abs/path:line — message (CODE)``.
_CODE_QUALITY_TITLE_RE = re.compile(
    r"^\[[^\]]+\]\s+(\S+?):\d+\s*—.*\(([A-Za-z0-9]+)\)\s*$",
    re.IGNORECASE,
)

_TERMINAL_STATUSES = frozenset({"completed", "deleted"})

SUPERSEDED_REASON = "superseded by {canonical_id} — duplicate identity"


# ---------------------------------------------------------------------------
# Identity helpers (pure)
# ---------------------------------------------------------------------------


def parse_finding_key(key: str) -> dict[str, str]:
    """Parse ``<code>:<path>``; mirrors workitem_creation.parse_finding_key."""
    text = str(key or "").strip()
    if ":" in text:
        code, path = text.split(":", 1)
    else:
        code, path = text, ""
    return {"code": code.strip(), "path": path.strip()}


def _normalise_key(key: str) -> str:
    """Normalise an identity key to ``<code>:<repo-relative path>``.

    Paths are normalised against the MAIN checkout root so a worktree-recorded
    path and a main-checkout-recorded path for the same file collapse to one
    identity (parent AC4). Relative paths pass through untouched — a pure
    operation for the keys consolidation deals with, so the dry-run grouping
    never touches subprocesses.
    """
    parsed = parse_finding_key(key)
    code = parsed["code"]
    path = parsed["path"]
    if path:
        norm = path.replace("\\", "/")
        if os.path.isabs(norm):
            norm = _strip_worktree_prefix(norm)
            root = _main_root()
            if root:
                try:
                    norm = os.path.relpath(norm, root).replace("\\", "/")
                except (ValueError, OSError):
                    pass
        while norm.startswith("./"):
            norm = norm[2:]
        if norm and norm != "/":
            norm = norm.rstrip("/")
        path = norm
    return f"{code}:{path}"


def _item_identity(item: dict[str, Any]) -> str | None:
    """The identity of *item*, from its marker, else its legacy fields.

    Items filed since SA-0MWE1E4GQ004E1JG carry ``Refactor-Finding-Key``;
    older items (the substring-dedupe era — the exact backlog this tool
    exists to collapse) carry the structured legacy fields
    (``**File:**`` / ``**Code:**`` and the ``Refactor: <type> in <path>``
    title). Both are reduced to the same ``<code>:<repo-relative path>``
    identity so a legacy clone and a marker item for the same finding group
    together.
    """
    haystack = f"{item.get('title', '')}\n{item.get('description', '')}"
    for found in _REFACTOR_KEY_RE.findall(haystack):
        return _normalise_key(found)

    code_match = _LEGACY_CODE_RE.search(item.get("description", "") or "")
    code = code_match.group(1).strip().strip("`") if code_match else ""
    if not code:
        smell_match = _LEGACY_SMELL_RE.search(
            item.get("description", "") or ""
        )
        if smell_match:
            code = smell_match.group(1).strip()
    file_match = _LEGACY_FILE_RE.search(item.get("description", "") or "")
    path = file_match.group(1).strip().strip("`") if file_match else ""
    if not path:
        # Code-quality finding titles carry the path and rule code directly.
        title = str(item.get("title") or "")
        cq_match = _CODE_QUALITY_TITLE_RE.match(title)
        if cq_match:
            candidate_path, title_code = cq_match.groups()
            if not code:
                code = title_code
            path = candidate_path
        else:
            in_match = _TITLE_IN_RE.search(title)
            if in_match:
                path = in_match.group(1).strip()
    if not code or not path:
        return None
    return _normalise_key(f"{code}:{path}")


# Cache of the main checkout root (git --git-common-dir always returns the
# shared main .git dir, even from inside a worktree).
_MAIN_ROOT_CACHE: dict[str, str | None] = {}


def _main_root() -> str | None:
    """The main checkout root, resolved once and cached.

    ``git rev-parse --git-common-dir`` returns the shared main ``.git``
    directory whether the process runs in the main checkout or in a linked
    worktree, so the parent of that directory is the stable base every
    absolute path must be made relative to (SA-0MW7Z11PV003V7O2).
    """
    if "root" in _MAIN_ROOT_CACHE:
        return _MAIN_ROOT_CACHE["root"]
    root: str | None = None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        common = (proc.stdout or "").strip() if proc.returncode == 0 else ""
        if common:
            if os.path.isabs(common):
                root = os.path.dirname(common)
            else:
                # Relative to the current toplevel; resolve against it.
                top = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                top_dir = (top.stdout or "").strip() if top.returncode == 0 else ""
                if top_dir:
                    root = os.path.normpath(
                        os.path.join(top_dir, common, os.pardir)
                    )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        root = None
    _MAIN_ROOT_CACHE["root"] = root
    return root


def _strip_worktree_prefix(path: str) -> str:
    """Drop a ``/.worklog/worktrees/<wt>/`` prefix from an absolute path.

    Pure string handling (no subprocess), so a worktree-recorded path and a
    main-checkout path for the same file become directly comparable before
    the repo-root relabelling (parent AC4).
    """
    marker = "/.worklog/worktrees/"
    idx = path.find(marker)
    if idx >= 0:
        rest = path[idx + len(marker):]
        slash = rest.find("/")
        if slash >= 0:
            return rest[slash + 1:]
    return path


# ---------------------------------------------------------------------------
# Grouping (pure — never touches the worklog)
# ---------------------------------------------------------------------------


def group_by_identity(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group non-terminal Refactor items by identity.

    The canonical is the oldest open item in the group; every other member is
    a duplicate. Completed/deleted items are excluded — they no longer track a
    finding, so they never form part of a live group.

    This function is deliberately PURE (no subprocess, no worklog mutation):
    it is what the dry-run renders and what ``--apply`` executes against, so
    a dry listing can never accidentally change state.

    Args:
        items: Work item dicts (e.g. from ``wl list --json``).

    Returns:
        ``[{"key", "canonical", "duplicates"}]`` in first-seen identity order;
        groups with a single member carry an empty ``duplicates`` list.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in _TERMINAL_STATUSES:
            continue
        key = _item_identity(item)
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    result: list[dict[str, Any]] = []
    for key in order:
        members = groups[key]
        canonical = min(
            members,
            key=lambda it: (
                str(it.get("createdAt") or ""),
                str(it.get("id") or ""),
            ),
        )
        duplicates = [member for member in members if member is not canonical]
        result.append({
            "key": key,
            "canonical": canonical,
            "duplicates": duplicates,
        })
    return result


# ---------------------------------------------------------------------------
# Worklog access (the CLI's only subprocess paths)
# ---------------------------------------------------------------------------


def _run_wl(cmd: Sequence[str]) -> dict[str, Any] | None:
    """Run a ``wl`` command and return parsed JSON (or ``None`` on failure)."""
    try:
        result = subprocess.run(  # noqa: PLW1510
            list(cmd), capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        LOG.warning("wl command failed (%s): %s", " ".join(cmd), exc)
        return None
    if result.returncode != 0:
        LOG.warning(
            "wl command failed (code %d): %s",
            result.returncode, result.stderr.strip(),
        )
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        LOG.warning("wl command returned unparseable output: %s", result.stdout[:200])
        return None


def load_refactor_items() -> list[dict[str, Any]]:
    """Every work item filed by the refactor skill (both eras).

    Queries the literal ``Refactor`` tag so the tool sees BOTH the
    marker-carrying items filed since the identity dedupe
    (SA-0MWE1E4GQ004E1JG) and the legacy substring-dedupe-era items that
    carry only the structured fields — exactly the duplicate backlog this
    tool exists to collapse.
    """
    data = _run_wl(["wl", "list", "--tags", "Refactor", "--json"]) or {}
    items = data.get("workItems", data.get("data", []))
    return items if isinstance(items, list) else []


def load_audit_twins() -> list[dict[str, Any]]:
    """Open audit-created false-positive chores (for twin reporting)."""
    data = _run_wl(["wl", "list", AUDIT_FP_TITLE_MARKER, "--json"]) or {}
    items = data.get("workItems", data.get("data", []))
    if not isinstance(items, list):
        return []
    return [
        item for item in items
        if isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() not in _TERMINAL_STATUSES
    ]


# ---------------------------------------------------------------------------
# Applying a consolidation
# ---------------------------------------------------------------------------


def _affected_lines(item: dict[str, Any]) -> set[int]:
    """The affected-line set recorded on *item*."""
    haystack = f"{item.get('title', '')}\n{item.get('description', '')}"
    match = _AFFECTED_LINES_RE.search(haystack)
    if not match:
        return set()
    return {int(value) for value in re.findall(r"\d+", match.group(1))}


def _unioned_description(canonical: dict[str, Any],
                         duplicates: Sequence[dict[str, Any]]) -> str:
    """The canonical description with the union of all affected lines."""
    lines: set[int] = set()
    for item in [canonical, *duplicates]:
        lines.update(_affected_lines(item))
    description = canonical.get("description") or ""
    if not lines:
        return description
    rendered = ", ".join(str(n) for n in sorted(lines))
    new_marker = f"Refactor-Affected-Lines: {rendered}"
    if _AFFECTED_LINES_RE.search(description):
        return _AFFECTED_LINES_RE.sub(new_marker, description, count=1)
    # No marker yet — place it right below the first line (the heading),
    # mirroring where the refactor skill emits identity markers.
    if "\n" in description:
        head, _, tail = description.partition("\n")
        return f"{head}\n{new_marker}{tail}"
    return f"{description}\n{new_marker}"


def _twin_matches(identity: str,
                  audit_items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live audit chores whose (rule, path) twins *identity*.

    The audit titling convention is ``<linter> <RULE> in <path>:<line> —
    candidate false positive …``; the line is ignored and the path is
    normalised to the same main-root-relative identity the grouping uses
    (SA-0MWE1EAR30045VHO AC1).
    """
    from refactor.workitem_creation import _parse_audit_fp_title

    matches: list[dict[str, Any]] = []
    for item in audit_items or []:
        parsed = _parse_audit_fp_title(item.get("title", ""))
        if not parsed:
            continue
        item_key = _normalise_key(f"{parsed['code']}:{parsed['path']}")
        if item_key == identity:
            matches.append(item)
    return matches


def apply_consolidation(group: dict[str, Any]) -> dict[str, Any]:
    """Collapse one identity group: union lines, comment+close duplicates.

    Returns a report dict; every sub-command failure is recorded, never
    raised (a partial collapse must not abort the whole run).
    """
    canonical = group["canonical"]
    duplicates = group["duplicates"]
    canonical_id = str(canonical.get("id") or "")
    report: dict[str, Any] = {
        "key": group["key"],
        "canonical": canonical_id,
        "duplicates": [str(d.get("id") or "") for d in duplicates],
        "lines_unioned": None,
        "closed": [],
        "errors": [],
    }
    if not canonical_id or not duplicates:
        return report

    new_description = _unioned_description(canonical, duplicates)
    if new_description != canonical.get("description"):
        updated = _run_wl([
            "wl", "update", canonical_id, "--description", new_description, "--json",
        ])
        if updated is None or not updated.get("success", True):
            report["errors"].append(f"update {canonical_id}: failed")
        else:
            report["lines_unioned"] = new_description

    reason = SUPERSEDED_REASON.format(canonical_id=canonical_id)
    for duplicate in duplicates:
        dup_id = str(duplicate.get("id") or "")
        if not dup_id:
            continue
        commented = _run_wl([
            "wl", "comment", "add", dup_id,
            "--comment", reason, "--author", "refactor-consolidation",
            "--json",
        ])
        closed = _run_wl([
            "wl", "close", dup_id,
            "--reason", reason, "--author", "refactor-consolidation",
            "--json",
        ])
        if commented is None or closed is None:
            report["errors"].append(f"close {dup_id}: failed")
        else:
            report["closed"].append(dup_id)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments (dry-run by default)."""
    parser = argparse.ArgumentParser(
        description=(
            "Consolidate duplicate refactor work items sharing one finding "
            "identity. Dry-run by default."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Collapse duplicates: union affected lines into the canonical "
            "item, then comment and close each duplicate. Without this flag "
            "the tool only lists the groups (no worklog changes)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    items = load_refactor_items()
    audit_items = load_audit_twins()
    groups = group_by_identity(items)
    groups_with_duplicates = [g for g in groups if g["duplicates"]]

    if args.json:
        report: dict[str, Any] = {
            "dry_run": not args.apply,
            "idents_items": len(items),
            "groups": groups,
            "groups_with_duplicates": len(groups_with_duplicates),
            "apply": [],
            "audit_twins": [],
        }
        if args.apply:
            report["apply"] = [apply_consolidation(g) for g in groups_with_duplicates]
        else:
            for group in groups_with_duplicates:
                report["audit_twins"].append({
                    "key": group["key"],
                    "twins": [
                        str(t.get("id") or "") for t in _twin_matches(group["key"], audit_items)
                    ],
                })
        print(json.dumps(report, indent=2, default=str))
        return 0

    if not groups_with_duplicates:
        print("No duplicate identity groups found — the backlog is clean.")
        return 0

    print(
        f"{'Dry-run' if not args.apply else 'Apply'}: "
        f"{len(groups_with_duplicates)} identity group(s) with duplicates "
        f"across {len(items)} refactor item(s)."
    )
    for group in groups_with_duplicates:
        canonical_id = group["canonical"].get("id", "?")
        dup_ids = ", ".join(str(d.get("id", "?")) for d in group["duplicates"])
        print(f"- {group['key']}: canonical {canonical_id}; duplicates: {dup_ids}")

    if not args.apply:
        twins = [
            {"key": group["key"], "twins": _twin_matches(group["key"], audit_items)}
            for group in groups_with_duplicates
        ]
        for entry in twins:
            if entry["twins"]:
                print(
                    f"  audit-family twins (never auto-closed): "
                    f"{', '.join(str(t.get('id','?')) for t in entry['twins'])}"
                )
        print("\nRun with --apply to collapse (unions affected lines; comments and closes duplicates).")
        return 0

    results = [apply_consolidation(group) for group in groups_with_duplicates]
    closed = sum(len(r["closed"]) for r in results)
    errors = [e for r in results for e in r["errors"]]
    print(f"Closed {closed} duplicate(s); {len(errors)} error(s).")
    for error in errors:
        print(f"  ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
