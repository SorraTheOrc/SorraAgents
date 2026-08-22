#!/usr/bin/env python3
"""find_related — deterministic related-work discovery for Worklog work items.

Fetches a work item, derives keywords from its title/description, searches
Worklog and the repository for related items, generates a concise Markdown
report, and updates the work-item description.

v3 (SA-0MNCDAQ8W008KOG9) performance/value changes:
  - Keywords are extracted with the skill's own automated report section
    stripped and ordered by descending frequency (closes the feedback loop
    that grew 14 -> 82 -> 523 keywords; numeric-only tokens dropped).
  - Worklog search queries only the top MAX_SEARCH_KEYWORDS keywords (8),
    capping the v2 per-keyword subprocess fan-out (measured ~0.5s/spawn;
    96% of v2 runtime was subprocess waits). Multi-term batched queries
    were measured to collapse conjunctively (1 result), so capping beats
    batching for recall.
  - Repo scan excludes `.worklog` (sidecar full report + stale worktree
    clones) and other non-authoritative dirs.
  - `update_description` cuts sections at `\n##` NOT followed by `#`, fixing
    the boundary bug that orphaned `###` sub-blocks on re-runs.

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
import traceback
from pathlib import Path
from typing import Any

# Add repo root to sys.path for shared utility access
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

from import_guard import guard_shared_import
from scripts.failure_notice import FailureNotice

try:
    from shared.status_lifecycle import (
        StatusLifecycle,
        resolve_worklog_dir,
        resolve_worklog_flags,
    )
except ModuleNotFoundError as _missing_shared:
    guard_shared_import(_missing_shared.name)

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
    "few", "more", "most", "other", "some", "no", "nor", "not",
    "only", "own", "same", "too", "very", "can", "will", "it", "its", "has", "have", "do", "does", "did", "done",
    "be", "been", "being", "am", "are", "was", "were",
}



# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = _SKILLS_ROOT.parent
REPORT_HEADING = "Related work (automated report)"


# ---------------------------------------------------------------------------
# Configurable limits (soft caps — may be replaced by minimum-relevance
# thresholds when semantic/embedding-based scoring is available)
# ---------------------------------------------------------------------------

# Maximum number of related work items to show in the automated report.
# Ranking heuristic: items are sorted by descending `score` (BM25/hybrid)
# from `wl search --json`. Items without a score sort last.
MAX_WORK_ITEM_RESULTS: int = 3

# Maximum number of repository file matches to show in the automated report.
# Ranking heuristic: files are sorted by descending count of distinct keywords
# matched. Ties are broken alphabetically for deterministic ordering.
MAX_REPO_FILE_RESULTS: int = 3

# Maximum number of matched keywords to list per repository file in the
# automated report. Raw keyword word-lists are the dominant source of report
# bloat (measured ~58% of the related-work section on SA-0MSF4AFX9007INSP),
# inflating every prompt that carries the description. The full keyword list
# is preserved in the persisted sidecar full report (see write_full_report).
MAX_KEYWORDS_PER_FILE: int = 5


# Maximum number of search keywords fed to Worklog search. Bounds the
# subprocess fan-out (v2 spawned one `wl search` per extracted keyword —
# measured ~0.5s/spawn, 96% of runtime — and grew unbounded on polluted
# descriptions: 14 -> 82 -> 523 keywords). Only the top
# MAX_SEARCH_KEYWORDS frequency-ranked keywords (see extract_keywords) are
# queried per-keyword: this preserved recall (26 distinct items from 6
# spawns on the review item) where multi-term batched queries collapsed to
# ~1 conjunctive result, so capping beats batching as the speed lever.
MAX_SEARCH_KEYWORDS: int = 8


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _default_repo_path(work_item_id: str) -> Path:
    """Resolve the target project's repository root for *work_item_id*.

    The work item's own worklog store is resolved via the shared
    prefix-to-sibling scan (:func:`resolve_worklog_dir`); the parent of the
    ``.worklog`` directory is the target project root. Deriving the default
    from the work item (rather than this script's own location) keeps repo
    scans on the analyzed project even when the script runs from the
    framework install dir (``~/.pi/agent/skills/find-related`` is a symlink
    into SorraAgents).

    Falls back to the framework ``REPO_ROOT`` when no store resolves
    (e.g. an unknown prefix with an empty cwd chain) — the pre-fix
    behavior.
    """
    wl_dir = resolve_worklog_dir(work_item_id)
    if wl_dir is not None:
        return wl_dir.parent
    return REPO_ROOT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        default=None,
        help="Path to the repository root (default: auto-detected from the "
             "work item's worklog store; falls back to the framework repo).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------


def extract_keywords(title: str, description: str) -> list[str]:
    """Derive conservative keywords from a work-item title and description.

    Returns unique, lowercased keywords ordered by **descending frequency**
    (deterministic — ties alphabetical). Frequency ordering puts the
    descriptive core of the text first, so capped search queries (see
    ``search_and_dedup`` / ``MAX_SEARCH_KEYWORDS``) pick the most
    representative terms rather than alphabetical junk (v3). Pure numeric
    tokens (years, counts) carry no search signal and are dropped.
    Common English stop words and very short terms are excluded.

    The skill's own 'Related work (automated report)' section is stripped
    from the description before tokenizing (v3): report words (work-item
    IDs, "matched", "repository", "worktrees") must never become search
    keywords, or every run re-extracts them and the search fan-out grows
    unbounded (feedback loop).
    """
    combined = f"{title} {strip_report_sections(description)}"
    # Lowercase
    combined = combined.lower()
    # Replace special characters (including hyphens) with spaces
    combined = re.sub(r"[^a-z0-9]", " ", combined)
    # Split into tokens
    tokens = combined.split()
    # Count frequencies: exclude stop words, short terms, and numeric-only
    # tokens ("2026", "452") that carry no search signal.
    counts: dict[str, int] = {}
    for token in tokens:
        if token in STOP_WORDS or len(token) < 3 or token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
    # Deterministic ordering: descending frequency, ties alphabetical. The
    # top of the list is the descriptive core of the item.
    return sorted(counts, key=lambda word: (-counts[word], word))


# ---------------------------------------------------------------------------
# Worklog CLI helpers
# ---------------------------------------------------------------------------


def _wl_flags_for(work_item_id: str) -> list[str]:
    """Resolve ``--worklog-dir`` flags pinned from the work-item id.

    Search and semantic-probe commands carry no work-item id of their own,
    so their target store is pinned from the id of the item being analyzed
    (prefix-to-sibling scan, then cwd-chain fallback — see the shared
    :func:`resolve_worklog_flags`). Commands that DO carry the id
    (show/update) resolve the same way from the id embedded in the command.
    """
    return resolve_worklog_flags(["wl", "show", work_item_id, "--json"])


def run_wl_show(work_item_id: str, worklog_flags: list[str] | None = None) -> dict[str, Any] | None:
    """Fetch a work item via `wl show <id> --json` and return parsed JSON.

    Unwraps the nested 'workItem' object from the wl response.
    Injects the resolved ``--worklog-dir`` (prefix-to-sibling scan) into the
    subprocess call so the item is fetched from its own worklog store
    regardless of the caller's cwd.
    Returns None if the command fails or output is not valid JSON.
    """
    try:
        cmd = ["wl", "show", work_item_id, "--json"]
        if worklog_flags is None:
            worklog_flags = _wl_flags_for(work_item_id)
        cmd[1:1] = worklog_flags
        out = subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE)
        data = json.loads(out)
        # wl show --json returns {success: true, workItem: {...}}
        if isinstance(data, dict) and "workItem" in data:
            return data["workItem"]
        return data
    except Exception:  # noqa: BLE001 -- wl show failure handled gracefully
        return None


def run_wl_search(keyword: str, use_semantic: bool = False,
                  worklog_flags: list[str] | None = None) -> list[dict[str, Any]]:
    """Search Worklog for items matching a keyword.

    When use_semantic is True, includes the --semantic flag for hybrid
    lexical+semantic ranking. Falls back to keyword-only search on error.

    ``worklog_flags`` pins the target worklog store (resolved from the
    work-item id being analyzed); when None, wl resolves from cwd.

    Returns a list of matching work items (empty list on failure).
    """
    try:
        cmd = ["wl"]
        cmd.extend(worklog_flags or [])
        cmd.append("search")
        if use_semantic:
            cmd.append("--semantic")
        cmd.extend([keyword, "--json"])
        out = subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE)
        data = json.loads(out)
        # wl search returns {"success": true, "workItems": [...]}
        if isinstance(data, dict):
            items = data.get("workItems", data.get("items"))
            if items is not None:
                return items
        # Fallback: bare list
        if isinstance(data, list):
            return data
        return []
    except Exception:  # noqa: BLE001 -- search failure handled gracefully
        return []


def run_wl_update(work_item_id: str, description: str,
                  worklog_flags: list[str] | None = None) -> bool:
    """Update a work item description via `wl update <id> --description <text>`.

    Injects the resolved ``--worklog-dir`` (prefix-to-sibling scan) into the
    subprocess call so the item is updated in its own worklog store.

    Returns True on success, False on failure.
    """
    try:
        cmd = ["wl", "update", work_item_id, "--description", description, "--json"]
        if worklog_flags is None:
            worklog_flags = _wl_flags_for(work_item_id)
        cmd[1:1] = worklog_flags
        subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE)
        return True
    except Exception:  # noqa: BLE001 -- update failure handled gracefully
        return False


# ---------------------------------------------------------------------------
# Semantic search availability detection
# ---------------------------------------------------------------------------


def is_semantic_available(worklog_flags: list[str] | None = None) -> bool:
    """Probe whether `wl search --semantic` is functional.

    Runs a simple probe query. Returns True if the command succeeds
    and returns a valid response. Falls back gracefully on any error.

    ``worklog_flags`` pins the target worklog store (resolved from the
    work-item id being analyzed); when None, wl resolves from cwd.
    """
    try:
        cmd = ["wl"]
        cmd.extend(worklog_flags or [])
        cmd.extend(["search", "--semantic", "probe", "--json"])
        out = subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE)
        json.loads(out)
        # Any valid response (successful or with items) means --semantic is available
        return True
    except Exception:  # noqa: BLE001 -- semantic probe failure handled gracefully
        return False


# ---------------------------------------------------------------------------
# Search and deduplication with ranking
# ---------------------------------------------------------------------------


def _score_key(item: dict[str, Any]) -> float:
    """Return the score of a work item for ranking, with tiebreaker by title.

    Items without a score field sort last (score = float('-inf')).
    Higher (less negative) scores rank first.
    """
    score = item.get("score")
    if score is None:
        return float("-inf")
    return float(score)


def search_and_dedup(
    keywords: list[str],
    use_semantic: bool = False,
    worklog_flags: list[str] | None = None,
    max_keywords: int | None = None,
) -> list[dict[str, Any]]:
    """Search Worklog for related items, aggregate, dedup, rank, and limit.

    v3 (SA-0MNCDAQ8W008KOG9): only the top *max_keywords* frequency-ranked
    keywords are queried — one ``wl search`` spawn each (hybrid
    ``--semantic`` ranking when available). This caps the v2 per-keyword
    fan-out (523 spawns on the polluted review item -> 8) while preserving
    per-keyword recall. Multi-term batched queries were measured to be
    conjunctive/collapse (a single 25-term ``--semantic`` query returned 1
    result vs 26 distinct items from 6 per-keyword spawns), so keyword
    capping — not query batching — is the speed lever that holds recall.

    ``worklog_flags`` pins the target worklog store (resolved from the
    work-item id being analyzed) so every search targets the same store.

    Ranking heuristic: items are sorted by descending score (the `score`
    field from `wl search --json`, BM25 or hybrid BM25+semantic). Items
    without a score sort last. The final list is capped at
    MAX_WORK_ITEM_RESULTS.
    """
    if not keywords:
        return []
    if max_keywords is None:
        max_keywords = MAX_SEARCH_KEYWORDS

    seen: set = set()
    results: list[dict[str, Any]] = []

    for keyword in keywords[:max_keywords]:
        items = run_wl_search(keyword, use_semantic=use_semantic,
                              worklog_flags=worklog_flags)
        for item in items:
            item_id = item.get("id")
            if item_id and item_id not in seen:
                seen.add(item_id)
                results.append(item)

    # Rank by descending score (unscored items sort last)
    results.sort(key=_score_key, reverse=True)

    # Limit to configured maximum
    return results[:MAX_WORK_ITEM_RESULTS]


# ---------------------------------------------------------------------------
# Repository file search
# ---------------------------------------------------------------------------

# Allowed file extensions for repository scanning
ALLOWED_EXTENSIONS: set = {".md", ".py", ".js", ".mjs", ".txt"}

# Directories to always exclude from repository scanning
EXCLUDED_DIRS: set = {".git", "node_modules", "__pycache__", ".pytest_cache",
                      ".venv", "venv", "env", ".idea", ".vscode",
                      "dist", "build", ".next",
                      # v3: the worklog store and agent scaffolding are
                      # non-authoritative / self-referential — the main
                      # checkout is canonical. The skill's own sidecar full
                      # report (`.worklog/tmp/find-related-full-<id>.md`) and
                      # stale worktree clones (`.worklog/worktrees/wl-*`) were
                      # measured to be 83% of scanned files AND ranked as top
                      # repo matches (SA-0MNCDAQ8W008KOG9).
                      ".worklog", ".pi", ".ruff_cache", ".mypy_cache"}


def search_repo(repo_path: str, keywords: list[str]) -> list[dict[str, Any]]:
    """Search repository files for matching keywords.

    Scans files with allowed extensions (see ALLOWED_EXTENSIONS) while
    respecting excluded directories. Returns a list of dicts with:
      - file: relative path from repo root
      - matches: list of keywords found in the file

    Results are ranked by descending number of distinct keyword matches
    (higher = more relevant). Ties are broken alphabetically for
    deterministic ordering. The final list is capped at
    MAX_REPO_FILE_RESULTS.

    Ranking heuristic: files matching more distinct keywords rank higher.
    This provides a simple relevance signal without requiring embeddings
    or full-text indexing.

    Returns empty list on error or no matches.
    """
    root = Path(repo_path)
    if not root.is_dir():
        return []

    results: list[dict[str, Any]] = []

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
        except Exception:  # noqa: S112, BLE001 -- skip unreadable files
            continue

        found = [kw for kw in keywords if kw.lower() in content]
        if found:
            results.append({
                "file": str(rel),
                "matches": sorted(found),
                # Number of distinct keyword matches — used for ranking
                "_match_count": len(found),
            })

    # Rank by descending match count, then alphabetically for determinism
    results.sort(key=lambda m: (-m["_match_count"], m["file"]))

    # Strip internal ranking field before returning
    for m in results:
        del m["_match_count"]

    # Limit to configured maximum
    return results[:MAX_REPO_FILE_RESULTS]


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _format_keyword_list(
    keywords: list[str],
    max_keywords: int | None,
) -> str:
    """Render a compact keyword list, capping at *max_keywords* entries.

    When *max_keywords* is ``None`` the full list is rendered. When the list
    is truncated, a ``(+N more)`` marker shows how many keywords were omitted
    so readers know the full data lives in the persisted sidecar full report.
    """
    if max_keywords is None or len(keywords) <= max_keywords:
        return ", ".join(keywords)
    shown = ", ".join(keywords[:max_keywords])
    return f"{shown} (+{len(keywords) - max_keywords} more)"


def format_report(
    work_item_id: str,
    related_items: list[dict[str, Any]],
    repo_matches: list[dict[str, Any]],
    max_keywords_per_file: int | None = MAX_KEYWORDS_PER_FILE,
) -> str:
    """Generate a Markdown report with related work items and repo matches.

    Keyword word-lists per repository file match are capped at
    *max_keywords_per_file* entries (default ``MAX_KEYWORDS_PER_FILE``) with a
    ``(+N more)`` marker, keeping the section compact in descriptions and any
    prompt that carries them. Pass ``max_keywords_per_file=None`` for the full
    untruncated report (used when persisting the complete data).

    Returns a string containing the full report section including heading.
    """
    lines: list[str] = []
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
            kw_str = _format_keyword_list(
                matched_keywords, max_keywords_per_file
            )
            lines.append(f"- `{file_path}` — matched: {kw_str}")

    lines.append("")
    return "\n".join(lines)


def write_full_report(
    work_item_id: str,
    related_items: list[dict[str, Any]],
    repo_matches: list[dict[str, Any]],
    repo_root: Path = REPO_ROOT,
) -> Path | None:
    """Persist the complete (untruncated) related-work report to a sidecar file.

    The work-item description carries the compact summary (see
    ``format_report``); this writes the full keyword lists to
    ``.worklog/tmp/find-related-full-<id>.md`` so the full related-work data
    remains available without bloating descriptions or prompts.

    Returns the path written, or ``None`` if persistence failed (best-effort:
    the description update must never fail because of this).
    """
    try:
        out_dir = repo_root / ".worklog" / "tmp"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"find-related-full-{work_item_id}.md"
        full_report = format_report(
            work_item_id, related_items, repo_matches,
            max_keywords_per_file=None,
        )
        target.write_text(full_report, encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001 -- best-effort persistence
        return None


# A top-level (level-2) Markdown heading is "\n## " — but the report's own
# sub-headings ("### Related work items", "### Repository file matches")
# also start with "\n##". The v2 boundary scan matched the plain "\n##"
# prefix, so re-runs cut the report short at its first ### sub-block,
# orphaning stale content and eventually stacking duplicate sub-blocks. The
# regex requires the heading NOT to be followed by another "#" so "###"
# sub-headings stay inside the report section.
_SECTION_BOUNDARY_RE = re.compile(r"\n##(?!#)")


def _section_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of every automated-report section in *text*.

    Each span runs from its ``## Related work (automated report)`` heading to
    the next top-level heading (``\n##`` not followed by ``#``) or the end
    of the text. ``###`` sub-headings inside the report are NOT boundaries
    (v3 boundary fix).
    """
    heading = f"## {REPORT_HEADING}"
    spans: list[tuple[int, int]] = []
    search_from = 0
    while True:
        start = text.find(heading, search_from)
        if start == -1:
            break
        end = len(text)
        boundary = _SECTION_BOUNDARY_RE.search(text, start + len(heading))
        if boundary is not None:
            end = boundary.start()
        spans.append((start, end))
        search_from = end
    return spans


def strip_report_sections(text: str) -> str:
    """Return *text* with every automated related-work report section removed.

    Used by keyword extraction so the skill's own report (other work-item
    IDs, "matched", "repository", "worktrees", ...) never feeds back into
    the next run's search keywords (v3 feedback-loop fix).
    """
    spans = _section_spans(text)
    if not spans:
        return text
    return text[: spans[0][0]] + text[spans[-1][1]:]


# ---------------------------------------------------------------------------
# Description update (idempotent)
# ---------------------------------------------------------------------------


def update_description(original_desc: str, report_section: str) -> str:
    """Append or replace the automated report section in a work-item description.

    ALL existing 'Related work (automated report)' sections are removed
    before the new report is inserted — earlier runs may have appended
    duplicates (e.g. when a wrong-store run was followed by a correct
    one). The new report is inserted at the position of the first removed
    section (in-place replacement, preserving surrounding sections), or
    appended when no section existed. Manual "Related work" sections
    (without the automated marker) are preserved.

    Section boundaries are detected via :data:`_SECTION_BOUNDARY_RE` (v3):
    ``###`` sub-headings belong to the report section and are replaced with
    it, so re-runs never orphan stale sub-blocks or stack duplicates.

    Returns the updated description string.
    """
    spans = _section_spans(original_desc)
    if not spans:
        # No existing report section — append
        return original_desc.rstrip() + report_section

    # Everything before the first report section, then the last removed
    # section's tail, with the new report inserted at the first section's
    # position (in-place replacement, preserving surrounding sections).
    remaining = original_desc[: spans[0][0]] + original_desc[spans[-1][1]:]
    insertion_idx = spans[0][0]
    before = remaining[:insertion_idx].rstrip()
    after = remaining[insertion_idx:]
    return before + report_section + after


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        _main()
    except Exception as exc:  # noqa: BLE001 -- top-level error handler
        notice = FailureNotice(
            script_name="find_related.py",
            reason=f"Unhandled exception: {exc}",
            stderr_context=traceback.format_exc(),
        )
        print(notice.wrap(
            f"An unexpected error occurred: {exc}\n"
            "Related work report could not be completed."
        ))
        sys.exit(1)


def _main() -> None:
    args = parse_args()

    # Resolve the default --repo-path from the work item's own worklog
    # store (parent of .worklog) so repo scans target the analyzed project
    # even when invoked from the framework install dir. An explicit
    # --repo-path continues to override the default.
    if args.repo_path is None:
        args.repo_path = str(_default_repo_path(args.work_item_id))

    # StatusLifecycle manages work-item status transitions:
    #   - On entry: sets status to in_progress (captures original)
    #   - On exit: restores original status (restore_on_exit) — this skill
    #     is read-only and must NOT advance the item to `completed`
    #   - On exception: restores original status
    with StatusLifecycle(args.work_item_id, restore_on_exit=True):

        # Pin the target worklog store from the work-item id so every wl call
        # (show/update/search/probe) targets the same store regardless of the
        # caller's cwd (prefix-to-sibling scan, SA-0MSG57UNY009DE51).
        wl_flags = _wl_flags_for(args.work_item_id)

        if args.verbose:
            print(f"[find-related] Work item: {args.work_item_id}", file=sys.stderr)
            print(f"[find-related] Repo path: {args.repo_path}", file=sys.stderr)

        # Fetch the work item
        work_item = run_wl_show(args.work_item_id, worklog_flags=wl_flags)
        if work_item is None:
            msg = f"Failed to fetch work item {args.work_item_id}"
            notice = FailureNotice(
                script_name="find_related.py",
                reason=f"Could not fetch work item {args.work_item_id}",
                stderr_context=msg,
            )
            if args.json_output:
                payload = {"error": msg, "script_failure": {"script_name": "find_related.py", "reason": msg}}
                print(json.dumps(payload))
            else:
                print(notice.wrap(f"Error: {msg}"))
            sys.exit(1)

        title = work_item.get("title", "")
        description = work_item.get("description", "")

        if args.verbose:
            print(f"[find-related] Title: {title}", file=sys.stderr)

        # Derive keywords
        keywords = extract_keywords(title, description)

        if args.verbose:
            print(f"[find-related] Keywords: {keywords}", file=sys.stderr)

        # Probe semantic search availability
        use_semantic = is_semantic_available(worklog_flags=wl_flags)
        if args.verbose:
            print(f"[find-related] Semantic search available: {use_semantic}", file=sys.stderr)

        # Search Worklog (with semantic ranking when available)
        related_items = search_and_dedup(keywords, use_semantic=use_semantic,
                                         worklog_flags=wl_flags)

        # Search repository (ranked and limited)
        repo_matches = search_repo(args.repo_path, keywords)

        # Filter out the current work item from results
        related_items = [
            item for item in related_items
            if item.get("id") != args.work_item_id
        ]

        # Generate report
        report_section = format_report(args.work_item_id, related_items, repo_matches)

        # Persist the full (untruncated) report so no related-work data is lost
        # when the description carries only the compact summary (P11 / AC2).
        full_report_path = write_full_report(
            args.work_item_id, related_items, repo_matches,
            repo_root=Path(args.repo_path),
        )

        # Update description
        original_desc = work_item.get("description", "")
        updated_desc = update_description(original_desc, report_section)
        update_success = run_wl_update(args.work_item_id, updated_desc,
                                       worklog_flags=wl_flags)

        if args.verbose and not update_success:
            print("[find-related] Warning: Failed to update work item description",
                  file=sys.stderr)

        added_ids = [item.get("id") for item in related_items if item.get("id")]

        result: dict[str, Any] = {
            "workItemId": args.work_item_id,
            "found": len(related_items) > 0 or len(repo_matches) > 0,
            "addedIds": added_ids,
            "reportInserted": update_success,
            "keywords": keywords,
            "relatedItemCount": len(related_items),
            "repoMatchCount": len(repo_matches),
            "fullReportPath": str(full_report_path) if full_report_path else None,
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
            if full_report_path:
                print(f"Full report persisted: {full_report_path}")

        sys.exit(0)


if __name__ == "__main__":
    main()
