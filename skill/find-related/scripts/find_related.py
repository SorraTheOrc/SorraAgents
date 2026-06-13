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
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Stop words
# ---------------------------------------------------------------------------

STOP_WORDS: set = {
    "a", "an", "the", "and", "or", "but", "if", "because", "as", "what",
    "which", "this", "that", "these", "those", "then", "just", "so", "than",
    "such", "both", "through", "about", "for", "is", "of", "while", "during",
    "to", "from", "in", "on", "at", "by", "with", "without", "into",
    "per", "between", "out", "against", "within", "upon", "after",
    "before", "above", "below", "across", "behind", "all", "any", "each",
    "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "too", "very", "can", "will", "just",
    "it", "its", "has", "have", "do", "does", "did", "done",
    "be", "been", "being", "am", "are", "was", "were",
}



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
# Keyword extraction
# ---------------------------------------------------------------------------


def extract_keywords(title: str, description: str) -> List[str]:
    """Derive conservative keywords from a work-item title and description.

    Returns a sorted list of unique, lowercased keywords.
    Common English stop words and very short terms are excluded.
    """
    combined = f"{title} {description}"
    # Lowercase
    combined = combined.lower()
    # Replace special characters (including hyphens) with spaces
    combined = re.sub(r"[^a-z0-9]", " ", combined)
    # Split into tokens
    tokens = combined.split()
    # Filter: remove stop words, keep only words with 3+ characters, deduplicate
    keywords = sorted(set(
        t for t in tokens
        if t not in STOP_WORDS and len(t) >= 3
    ))
    return keywords


# ---------------------------------------------------------------------------
# Worklog CLI helpers
# ---------------------------------------------------------------------------


def run_wl_show(work_item_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a work item via `wl show <id> --json` and return parsed JSON.

    Unwraps the nested 'workItem' object from the wl response.
    Returns None if the command fails or output is not valid JSON.
    """
    try:
        cmd = ["wl", "show", work_item_id, "--json"]
        out = subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE)
        data = json.loads(out)
        # wl show --json returns {success: true, workItem: {...}}
        if isinstance(data, dict) and "workItem" in data:
            return data["workItem"]
        return data
    except Exception:
        return None


def run_wl_search(keyword: str) -> List[Dict[str, Any]]:
    """Search Worklog for items matching a keyword.

    Returns a list of matching work items (empty list on failure).
    """
    try:
        cmd = ["wl", "search", keyword, "--json"]
        out = subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE)
        data = json.loads(out)
        # wl search may return {"items": [...]} or a bare list
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def run_wl_update(work_item_id: str, description: str) -> bool:
    """Update a work item description via `wl update <id> --description <text>`.

    Returns True on success, False on failure.
    """
    try:
        cmd = ["wl", "update", work_item_id, "--description", description, "--json"]
        subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Search and deduplication
# ---------------------------------------------------------------------------


def search_and_dedup(keywords: List[str]) -> List[Dict[str, Any]]:
    """Search Worklog for each keyword, aggregate results, and deduplicate.

    Returns a list of unique work item dicts (by id).
    """
    seen: set = set()
    results: List[Dict[str, Any]] = []

    for keyword in keywords:
        items = run_wl_search(keyword)
        for item in items:
            item_id = item.get("id")
            if item_id and item_id not in seen:
                seen.add(item_id)
                results.append(item)

    return results


# ---------------------------------------------------------------------------
# Repository file search
# ---------------------------------------------------------------------------

# Allowed file extensions for repository scanning
ALLOWED_EXTENSIONS: set = {".md", ".py", ".js", ".mjs", ".txt"}

# Directories to always exclude from repository scanning
EXCLUDED_DIRS: set = {".git", "node_modules", "__pycache__", ".pytest_cache",
                      ".venv", "venv", "env", ".idea", ".vscode",
                      "dist", "build", ".next"}


def search_repo(repo_path: str, keywords: List[str]) -> List[Dict[str, Any]]:
    """Search repository files for matching keywords.

    Scans files with allowed extensions (see ALLOWED_EXTENSIONS) while
    respecting excluded directories. Returns a list of dicts with:
      - file: relative path from repo root
      - matches: list of keywords found in the file

    Returns empty list on error or no matches.
    """
    root = Path(repo_path)
    if not root.is_dir():
        return []

    keyword_set = set(k.lower() for k in keywords)
    results: List[Dict[str, Any]] = []

    for file_path in root.rglob("*"):
        # Skip directories
        if not file_path.is_file():
            continue

        # Check extension
        if file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue

        # Check if file is inside an excluded directory
        rel = file_path.relative_to(root)
        parts = rel.parts
        if any(part in EXCLUDED_DIRS for part in parts):
            continue

        # Read and search file content
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue

        found = [kw for kw in keywords if kw.lower() in content]
        if found:
            results.append({
                "file": str(rel),
                "matches": sorted(found),
            })

    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def format_report(
    work_item_id: str,
    related_items: List[Dict[str, Any]],
    repo_matches: List[Dict[str, Any]],
) -> str:
    """Generate a Markdown report with related work items and repo matches.

    Returns a string containing the full report section including heading.
    """
    lines: List[str] = []
    lines.append(f"\n## {REPORT_HEADING}")

    if not related_items and not repo_matches:
        lines.append("\nNo related work items or documentation matches found.")
        return "\n".join(lines)

    if related_items:
        lines.append("\n### Related work items")
        for item in related_items:
            item_id = item.get("id", "?")
            title = item.get("title", item.get("description", "Unknown"))
            status = item.get("status", "")
            status_str = f" ({status})" if status else ""
            lines.append(f"- **{item_id}** – {title}{status_str}")

    if repo_matches:
        lines.append("\n### Repository file matches")
        for match in repo_matches:
            file_path = match.get("file", "?")
            matched_keywords = match.get("matches", [])
            kw_str = ", ".join(matched_keywords)
            lines.append(f"- `{file_path}` — matched: {kw_str}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Description update (idempotent)
# ---------------------------------------------------------------------------


def update_description(original_desc: str, report_section: str) -> str:
    """Append or replace the automated report section in a work-item description.

    If the description already contains a 'Related work (automated report)'
    section, it is replaced. Otherwise the report is appended.

    Returns the updated description string.
    """
    heading_pattern = f"## {REPORT_HEADING}"
    heading_idx = original_desc.find(heading_pattern)

    if heading_idx == -1:
        # No existing report section — append
        return original_desc.rstrip() + report_section

    # Find the start of the next section after the report heading
    next_section_idx = len(original_desc)
    for i in range(heading_idx + len(heading_pattern), len(original_desc)):
        if original_desc[i : i + 3] == "\n##":
            next_section_idx = i
            break

    # Replace the old report section with the new one
    before = original_desc[:heading_idx].rstrip()
    after = original_desc[next_section_idx:]
    return before + report_section + after


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()

    if args.verbose:
        print(f"[find-related] Work item: {args.work_item_id}", file=sys.stderr)
        print(f"[find-related] Repo path: {args.repo_path}", file=sys.stderr)

    # Fetch the work item
    work_item = run_wl_show(args.work_item_id)
    if work_item is None:
        msg = f"Failed to fetch work item {args.work_item_id}"
        if args.json_output:
            print(json.dumps({"error": msg}))
        else:
            print(f"Error: {msg}")
        sys.exit(1)

    title = work_item.get("title", "")
    description = work_item.get("description", "")

    if args.verbose:
        print(f"[find-related] Title: {title}", file=sys.stderr)

    # Derive keywords
    keywords = extract_keywords(title, description)

    if args.verbose:
        print(f"[find-related] Keywords: {keywords}", file=sys.stderr)

    # Search Worklog
    related_items = search_and_dedup(keywords)

    # Search repository
    repo_matches = search_repo(args.repo_path, keywords)

    # Filter out the current work item from results
    related_items = [
        item for item in related_items
        if item.get("id") != args.work_item_id
    ]

    # Generate report
    report_section = format_report(args.work_item_id, related_items, repo_matches)

    # Update description
    original_desc = work_item.get("description", "")
    updated_desc = update_description(original_desc, report_section)
    update_success = run_wl_update(args.work_item_id, updated_desc)

    if args.verbose and not update_success:
        print("[find-related] Warning: Failed to update work item description",
              file=sys.stderr)

    added_ids = [item.get("id") for item in related_items if item.get("id")]

    result: Dict[str, Any] = {
        "workItemId": args.work_item_id,
        "found": len(related_items) > 0 or len(repo_matches) > 0,
        "addedIds": added_ids,
        "reportInserted": update_success,
        "keywords": keywords,
        "relatedItemCount": len(related_items),
        "repoMatchCount": len(repo_matches),
    }

    if args.json_output:
        print(json.dumps(result))
    else:
        print(f"Work item: {args.work_item_id}")
        print(f"Related items found: {len(related_items)}")
        print(f"Repository matches: {len(repo_matches)}")
        if added_ids:
            print(f"Added IDs: {', '.join(added_ids)}")
        print(f"Report inserted: {update_success}")

    sys.exit(0)


if __name__ == "__main__":
    main()
