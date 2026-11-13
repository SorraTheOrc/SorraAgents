"""Work item creation from detected code smells.

Creates Worklog work items for pre-existing code smells so that technical
debt is tracked and can be addressed systematically.

Provides:
  - create_smell_work_item(): Create a single work item for one smell.
  - create_smell_work_items(): Batch-create work items for multiple smells,
    grouping same-identity findings into one item.
  - create_smell_work_items_mapped(): Batch-create and map each identity to
    its work item ID.
  - group_smells_by_identity(): Group smells by finding identity.
  - build_finding_key() / parse_finding_key(): Build and parse the exact
    ``<code>:<repo-relative-path>`` identity carried in each item.
  - compute_content_fingerprint(): sha256 of the target file content + rule.
  - severity_to_priority(): Map smell severity to work item priority.
  - build_smell_title(): Generate a work item title from a smell.
  - build_smell_description(): Generate a work item description from a smell.
  - has_existing_smell_comment(): Check for existing REFACTOR comments.

Usage:

    from refactor.workitem_creation import (
        create_smell_work_item,
        create_smell_work_items,
        has_existing_smell_comment,
    )

    smell = {"file": "src/main.py", "line": 42, "severity": "critical", ...}
    work_item_id = create_smell_work_item(smell)

    # Batch creation with duplicate prevention
    results = create_smell_work_items([smell1, smell2, smell3])
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any

LOG = logging.getLogger("refactor.workitem_creation")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Severity-to-priority mapping.
SEVERITY_PRIORITY_MAP: dict[str, str] = {
    "critical": "high",
    "high": "high",
    "medium": "medium",
    "low": "low",
}

# Default priority for unknown or missing severity values.
DEFAULT_PRIORITY = "medium"

# The tag applied to all smell-based work items.
REFACTOR_TAG = "Refactor"

# Prefix for work item titles.
TITLE_PREFIX = "Refactor:"

# Pattern to detect existing REFACTOR comments in source files.
REFACTOR_COMMENT_PATTERN = re.compile(
    r"REFACTOR-(\S+)\s*\n\s*(?:#|//|--|<!--)\s*smell:\s*(\S+)",
    re.MULTILINE,
)

# Machine-readable identity markers embedded in a work item's description.
#
# ``Refactor-Finding-Key: <code>:<repo-relative-path>`` is the exact identity
# a re-found smell is deduplicated on (SA-0MWE1E4GQ004E1JG). The line number
# is deliberately excluded so line drift cannot break the lookup, and the
# path is repository-relative so worktree-prefix drift cannot either.
FINDING_KEY_MARKER = "Refactor-Finding-Key:"
FINGERPRINT_MARKER = "Refactor-Fingerprint:"
AFFECTED_LINES_MARKER = "Refactor-Affected-Lines:"

# Local parser for the identity marker, mirroring the audit-side
# ``CFP-Chore-Key`` precedent: one ``wl list`` call, then a regex parse here.
_FINDING_KEY_RE = re.compile(
    r"Refactor-Finding-Key:[ \t]*(\S+)", re.IGNORECASE
)

# Statuses that no longer track a finding. A completed/deleted item must not
# suppress a fresh filing (SA-0MWE1E4GQ004E1JG AC3).
FINDING_KEY_TERMINAL_STATUSES = frozenset({"completed", "deleted"})

# Marker carried by every audit-created false-positive chore title. The audit
# producer titles its chores ``ruff <RULE> in <path>:<line> — candidate false
# positive — producer decision required``, so the refactor guard queries by
# this marker and ignores the line when matching (SA-0MWE1EAR30045VHO AC1).
AUDIT_FP_TITLE_MARKER = "candidate false positive"

# Local parser for audit-family titles. Captures the linter, rule code and
# ``<path>[:<line>]``; the trailing line is stripped separately so line drift
# cannot break the cross-family identity.
_AUDIT_FP_TITLE_RE = re.compile(
    r"^(\S+)\s+(\S+)\s+in\s+(.+?)\s*—\s*candidate false positive",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def severity_to_priority(severity: Any) -> str:
    """Map a smell severity level to a Worklog priority.

    Args:
        severity: The severity value (``"critical"``, ``"high"``,
                  ``"medium"``, ``"low"``, or any other value).

    Returns:
        A Worklog priority string (``"high"``, ``"medium"``, ``"low"``).
    """
    if not isinstance(severity, str):
        return DEFAULT_PRIORITY
    return SEVERITY_PRIORITY_MAP.get(severity.lower(), DEFAULT_PRIORITY)


def _finding_code(smell: dict[str, Any]) -> str:
    """Return the rule/smell code that forms half of a finding's identity.

    Linter findings carry an explicit ``code``; LLM-derived smells may only
    carry a ``smell_type``. Falling back to the smell type keeps the identity
    meaningful instead of producing a bare ``:path`` key.
    """
    code = str(smell.get("code") or "").strip()
    if code:
        return code
    return str(smell.get("smell_type") or "unknown").strip()


def build_finding_key(
    smell: dict[str, Any], repo_root: str | None = None
) -> str:
    """Build the exact identity for a smell finding.

    The identity is ``<code>:<repository-relative path>``. Neither the line
    number nor an absolute/worktree path is part of it, so the same smell is
    recognised across line drift and across implement worktrees
    (SA-0MWE1E4GQ004E1JG AC1).

    Args:
        smell: A smell finding dict with ``file`` and ``code`` keys.
        repo_root: Optional repository root used to normalise an absolute
            ``file`` path (reuses the shared :func:`_repo_relative_path`).

    Returns:
        The identity string, e.g. ``"S112:skill/x.py"``.
    """
    code = _finding_code(smell)
    path = _repo_relative_path(smell.get("file", ""), repo_root)
    return f"{code}:{path}"


def parse_finding_key(key: str) -> dict[str, str]:
    """Parse a ``<code>:<repository-relative path>`` finding key.

    The key is split on the first colon only. File paths containing a colon
    are out of scope (matching the audit-side ``CFP-Chore-Key`` precedent,
    documented in ``skill/refactor/SKILL.md``).

    Args:
        key: An identity string. A value without a colon is treated as a code
            with an empty path.

    Returns:
        ``{"code": <code>, "path": <path>}``.
    """
    text = str(key or "").strip()
    if ":" in text:
        code, path = text.split(":", 1)
    else:
        code, path = text, ""
    return {"code": code.strip(), "path": path.strip()}


def _normalise_finding_key(key: str, repo_root: str | None = None) -> str:
    """Normalise a finding key so equivalent identities compare equal."""
    parsed = parse_finding_key(key)
    code = parsed["code"]
    path = parsed["path"]
    normalised_path = _repo_relative_path(path, repo_root) if path else ""
    return f"{code}:{normalised_path}"


def compute_content_fingerprint(
    smell: dict[str, Any], repo_root: str | None = None
) -> str:
    """Compute a content fingerprint for a smell's target file.

    The fingerprint is the sha256 hex digest of the target file's bytes
    followed by a ``NUL`` separator and the rule code, so it changes when the
    file content or the rule changes and stays stable otherwise
    (SA-0MWE1E4GQ004E1JG AC2, aligning with re-verification item
    SA-0MW7Z1JCE009G6J3 AC1: file hash + rule set).

    The path is **not** hashed, so the same content scanned from a worktree
    and from the main checkout yields the same fingerprint.

    Returns:
        A 64-character lowercase sha256 hex digest, or an empty string when
        the target file cannot be read (e.g. a relative path that does not
        resolve from the current directory).
    """
    raw_path = str(smell.get("file", "") or "")
    if not raw_path:
        return ""

    candidate = raw_path
    if not os.path.isabs(candidate) and repo_root:
        rooted = os.path.join(str(repo_root), candidate)
        if os.path.isfile(rooted):
            candidate = rooted

    try:
        with open(candidate, "rb") as handle:
            content = handle.read()
    except (OSError, ValueError):
        return ""

    digest = hashlib.sha256()
    digest.update(content)
    digest.update(b"\x00")
    digest.update(_finding_code(smell).encode("utf-8"))
    return digest.hexdigest()


def _parse_audit_fp_title(title: str) -> dict[str, str] | None:
    """Parse an audit false-positive chore title into ``{code, path}``.

    Accepts titles of the form ``<linter> <RULE> in <path>:<line> — candidate
    false positive …``. The line (when present) is stripped from the path
    because the finding identity never includes it; only the rule code and the
    repository-relative path participate in the cross-family match
    (SA-0MWE1EAR30045VHO AC1). Returns ``None`` when the title does not look
    like an audit-family item.
    """
    match = _AUDIT_FP_TITLE_RE.match(str(title or "").strip())
    if not match:
        return None
    _linter, code, path_and_line = match.groups()
    path = path_and_line
    if ":" in path:
        head, _separator, tail = path.rpartition(":")
        if tail.isdigit():
            path = head
    if not path:
        return None
    return {"code": code, "path": path}


def _affected_lines(smell: dict[str, Any]) -> list[int]:
    """Return the deduplicated, sorted affected-line list for a smell.

    ``lines`` (when present) is the full observed set; the primary ``line`` is
    always included so a caller that supplies both loses no detail. The list
    is what makes consolidation safe: grouping same-identity findings keeps
    every reported line (SA-0MWE1E4GQ004E1JG AC2/AC4).
    """
    raw_lines = smell.get("lines")
    if raw_lines is None:
        raw_lines = []
    elif isinstance(raw_lines, (int, str)):
        raw_lines = [raw_lines]

    observed: set[int] = set()
    for value in [*raw_lines, smell.get("line")]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            observed.add(number)
    return sorted(observed)


def build_smell_title(smell: dict[str, Any], repo_root: str | None = None) -> str:
    """Build a descriptive work item title for a code smell.

    The title follows the format::

        Refactor: <Smell Type> in <file>

    The recorded file is always **repository-relative** (never an absolute
    path, never a ``.worklog/worktrees/**`` prefix) so that the same source
    file scanned from different implement worktrees yields an identical
    identity (SA-0MW7Z11PV003V7O2).

    Args:
        smell: A smell finding dict with at least ``"file"``,
               ``"smell_type"``, and ``"message"`` keys.
        repo_root: Optional repository root used to normalise an absolute
            ``file`` path. Defaults to resolving ``git rev-parse
            --show-toplevel`` from the current checkout.

    Returns:
        A title string suitable for a work item.
    """
    smell_type = smell.get("smell_type", "unknown").replace("_", " ").title()
    file_path = _repo_relative_path(smell.get("file", "unknown file"), repo_root)
    return f"{TITLE_PREFIX} {smell_type} in {file_path}"


def build_smell_description(
    smell: dict[str, Any], repo_root: str | None = None
) -> str:
    """Build a detailed work item description for a code smell.

    The description includes the file path, line number, smell type,
    severity, source, and original message in a structured markdown format.

    As with :func:`build_smell_title`, the recorded file is repository-relative
    so an ephemeral worktree path never leaks into the work item
    (SA-0MW7Z11PV003V7O2).

    Args:
        smell: A smell finding dict.
        repo_root: Optional repository root used to normalise an absolute
            ``file`` path. Defaults to resolving ``git rev-parse
            --show-toplevel`` from the current checkout.

    Returns:
        A markdown-formatted description string.
    """
    file_path = _repo_relative_path(smell.get("file", "unknown"), repo_root)
    line = smell.get("line", 0)
    severity = smell.get("severity", "medium")
    smell_type = smell.get("smell_type", "unknown")
    source = smell.get("source", "unknown")
    code = smell.get("code", "")
    message = smell.get("message", "No description provided")

    finding_key = build_finding_key(smell, repo_root)
    fingerprint = compute_content_fingerprint(smell, repo_root)
    affected_lines = _affected_lines(smell)

    # Identity markers sit immediately below the heading so that title
    # truncation cannot hide them (SA-0MWE1E4GQ004E1JG AC2).
    parts = [
        f"## Code Smell: {smell_type}",
        "",
        f"{FINDING_KEY_MARKER} {finding_key}",
    ]
    if fingerprint:
        parts.append(f"{FINGERPRINT_MARKER} {fingerprint}")
    if affected_lines:
        rendered_lines = ", ".join(str(n) for n in affected_lines)
        parts.append(f"{AFFECTED_LINES_MARKER} {rendered_lines}")
    parts += [
        "",
        f"- **File:** `{file_path}`",
        f"- **Line:** {line}",
        f"- **Severity:** {severity}",
        f"- **Source:** {source}",
        f"- **Code:** `{code}`" if code else "",
        "",
        "### Description",
        "",
        message,
        "",
        "---",
        "",
        "This work item was automatically created by the refactor skill.",
    ]
    return "\n".join(p for p in parts if p)


def has_existing_smell_comment(file_path: str, smell_type: str) -> bool:
    """Check if a source file already has a REFACTOR comment for a smell type.

    Prevents duplicate work items for the same code smell.

    Args:
        file_path: Path to the source file to check.
        smell_type: The smell type to look for (e.g. ``"security"``).

    Returns:
        ``True`` if a REFACTOR comment with the given smell type exists,
        ``False`` otherwise.
    """
    if not os.path.isfile(file_path):
        return False

    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return False

    if not content.strip():
        return False

    # Check for REFACTOR comments matching the smell type
    for match in REFACTOR_COMMENT_PATTERN.finditer(content):
        matched_smell_type = match.group(2).strip().lower()
        if matched_smell_type == smell_type.lower():
            return True

    return False


# Prefix (relative to the repository root) denoting ephemeral implement
# worktree roots that must never become a work item's identity.
WORKTREES_PREFIX = ".worklog/worktrees"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_path(path: str) -> str:
    """Normalise a path to POSIX separators with no leading ``./``/trailing ``/``."""
    value = str(path).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if value not in ("/", ""):
        value = value.rstrip("/")
    return value


def _git_toplevel() -> str | None:
    """Return the current checkout's top-level directory.

    Run from inside a git worktree this returns that worktree's root, which is
    exactly the base an absolute scan path must be made relative to. When git
    is unavailable or fails, the process working directory is used as a
    best-effort scan root (the refactor skill runs from the repository or
    worktree root), so a path is still normalised rather than left absolute.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        proc = None

    if proc is not None and proc.returncode == 0:
        root = (proc.stdout or "").strip()
        # Reject non-path output (e.g. a misconfigured/mocked command) so a
        # bogus root cannot silently produce a wrong relative path.
        if root and os.path.isabs(root):
            return root

    fallback = os.getcwd()
    return fallback if fallback and os.path.isabs(fallback) else None


def _repo_relative_path(path: str, repo_root: str | None = None) -> str:
    """Return *path* relative to the repository (or worktree) root.

    - Paths that are already relative pass through (POSIX-normalised).
    - Absolute paths are made relative to *repo_root* when supplied, else to
      ``git rev-parse --show-toplevel``; running inside an implement worktree
      that resolves to the worktree root, so
      ``.worklog/worktrees/<wt>/skill/...`` becomes ``skill/...``.
    - If the root cannot be resolved the path is returned POSIX-normalised.

    This guarantees a work item's identity is stable regardless of which
    worktree the scan ran in (SA-0MW7Z11PV003V7O2, AC1).
    """
    if not path:
        return ""

    normalised = str(path).replace("\\", "/")
    if not os.path.isabs(normalised):
        return _normalise_path(normalised)

    root = str(repo_root) if repo_root else _git_toplevel()
    if not root:
        return _normalise_path(normalised)

    try:
        relative = os.path.relpath(normalised, root)
    except (ValueError, OSError):
        return _normalise_path(normalised)
    return _normalise_path(relative)


def _is_under_worklog_worktrees(relative_path: str) -> bool:
    """Return ``True`` when *relative_path* lies under ``.worklog/worktrees``."""
    rel = _normalise_path(relative_path)
    return rel == WORKTREES_PREFIX or rel.startswith(WORKTREES_PREFIX + "/")


def _find_existing_audit_twin(
    smell: dict[str, Any], repo_root: str | None = None
) -> dict[str, Any] | None:
    """Find a live audit-created false-positive chore tracking the identity.

    Closes the cross-family blindness (parent AC2): the refactor producer must
    also see the audit ``ruff <RULE> in <path> — candidate false positive``
    family (73 open items at filing) so the two producers cannot mint blind
    twins for the same diagnostic. One ``wl list "candidate false positive"
    --json`` call returns every audit chore; each title is parsed locally and
    matched on (rule code, repository-relative path) after path
    normalisation — never a substring, never a line number
    (SA-0MWE1EAR30045VHO AC1). Only non-terminal chores count: a completed
    chore no longer tracks the finding, so a fresh filing is allowed.

    Args:
        smell: A smell finding dict.
        repo_root: Optional repository root used to normalise absolute paths.

    Returns:
        The matching audit item dict, or ``None`` when no live audit twin
        exists (including when the lookup fails — fail-open, so a transient
        ``wl`` problem never silently drops a finding).
    """
    if not shutil.which("wl"):
        return None

    target_code = _finding_code(smell)
    target_path = _repo_relative_path(smell.get("file", ""), repo_root)
    if not target_path:
        return None

    try:
        result = subprocess.run(  # noqa: PLW1510
            ["wl", "list", AUDIT_FP_TITLE_MARKER, "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    work_items = data.get("workItems", data.get("data", []))
    if not isinstance(work_items, list):
        return None

    for item in work_items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "") or "").strip().lower()
        if status in FINDING_KEY_TERMINAL_STATUSES:
            continue
        parsed = _parse_audit_fp_title(item.get("title", ""))
        if not parsed:
            continue
        normalised_path = _repo_relative_path(parsed["path"], repo_root)
        if parsed["code"] == target_code and normalised_path == target_path:
            return item

    return None


def _find_existing_worklog_item(
    smell: dict[str, Any], repo_root: str | None = None
) -> dict[str, Any] | None:
    """Find the non-terminal work item tracking this exact finding identity.

    Uses the audit-side ``CFP-Chore-Key`` precedent: one ``wl list
    "Refactor-Finding-Key:" --json`` call returns every item carrying a
    refactor identity marker, each marker is parsed locally, and the match is
    an **exact** key comparison after path normalisation. There is no
    substring matching anywhere, so line numbers, title truncation and
    worktree prefixes cannot create false positives or false negatives
    (SA-0MWE1E4GQ004E1JG AC3).

    Args:
        smell: A smell finding dict.
        repo_root: Optional repository root used to normalise absolute paths.

    Returns:
        The matching work item dict, or ``None`` when no live item tracks the
        identity (including when the lookup itself fails — fail-open, so a
        transient ``wl`` problem never silently drops a finding).
    """
    if not shutil.which("wl"):
        return None

    target_key = build_finding_key(smell, repo_root)
    if not parse_finding_key(target_key)["path"]:
        return None

    try:
        result = subprocess.run(  # noqa: PLW1510
            ["wl", "list", FINDING_KEY_MARKER, "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    work_items = data.get("workItems", data.get("data", []))
    if not isinstance(work_items, list):
        return None

    for item in work_items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "") or "").strip().lower()
        if status in FINDING_KEY_TERMINAL_STATUSES:
            continue
        haystack = f"{item.get('title', '')}\n{item.get('description', '')}"
        for found in _FINDING_KEY_RE.findall(haystack):
            if _normalise_finding_key(found, repo_root) == target_key:
                return item

    return None


def create_smell_work_item(
    smell: dict[str, Any], repo_root: str | None = None
) -> str | None:
    """Create a single Worklog work item for a code smell.

    Enforces the following guards before creating a work item:

    0. **Ephemeral worktree target**: If the smell's repository-relative file
       path lies under ``.worklog/worktrees/**`` the finding is skipped — such
       a worktree is ephemeral and the resulting work item would be
       unactionable (SA-0MW7Z11PV003V7O2, AC2).
    1. **File existence**: If the referenced source file does not exist on
       disk, a warning is logged and creation is skipped.\
       (AC: SA-0MQJLXMV7002X1VY)
    2. **REFACTOR comment duplicate**: If the source file already contains a
       REFACTOR comment for the same smell type, creation is skipped.
    3. **Worklog duplicate**: The worklog database is queried for existing
       items with the same (file, line, code) combination as a secondary
       safety net.

    Args:
        smell: A smell finding dict.
        repo_root: Optional repository root used to normalise an absolute
            ``file`` path to a repository-relative identity.

    Returns:
        The work item ID if creation succeeded, or ``None`` if it was
        skipped (duplicate) or failed.
    """
    file_path = smell.get("file", "")
    smell_type = smell.get("smell_type", "unknown")
    relative_path = _repo_relative_path(file_path, repo_root) if file_path else ""

    # Guard 0: never file an item against an ephemeral implement worktree (AC2).
    if _is_under_worklog_worktrees(relative_path):
        LOG.warning(
            "Skipping work item creation for %s in %s: target lies under "
            ".worklog/worktrees and is not actionable",
            smell_type,
            relative_path,
        )
        return None

    # Guard 1: File existence check (AC1)
    if file_path and not os.path.isfile(file_path):
        LOG.warning(
            "Skipping work item creation for %s in %s: "
            "source file does not exist on disk",
            smell_type,
            file_path,
        )
        return None

    # Guard 2: Check for duplicate REFACTOR comment in source file
    if file_path and has_existing_smell_comment(file_path, smell_type):
        LOG.info(
            "Skipping creation for %s in %s: duplicate REFACTOR comment exists",
            smell_type,
            file_path,
        )
        return None

    # Guard 3: Check worklog database for the exact finding identity (AC3)
    existing_item = _find_existing_worklog_item(smell, repo_root)
    if existing_item is not None:
        LOG.info(
            "Skipping creation for %s in %s: existing worklog item %s found",
            smell_type,
            file_path,
            existing_item.get("id", "<unknown>"),
        )
        return None

    # Guard 3.5: cross-family twin suppression (parent AC2). An open
    # audit-created ``ruff <RULE> in <path> — candidate false positive``
    # chore already tracks this identity, so minting a refactor item would
    # create a blind twin. The audit chore carries ``needsProducerReview`` by
    # design and is never closed or mutated here — only the creation is
    # suppressed (SA-0MWE1EAR30045VHO AC1).
    audit_twin = _find_existing_audit_twin(smell, repo_root)
    if audit_twin is not None:
        LOG.info(
            "Skipping creation for %s in %s: audit-family item %s already "
            "tracks this identity",
            smell_type,
            file_path,
            audit_twin.get("id", "<unknown>"),
        )
        return None

    title = build_smell_title(smell, repo_root)
    description = build_smell_description(smell, repo_root)
    priority = severity_to_priority(smell.get("severity", "medium"))

    # Build the wl create command
    cmd = [
        "wl",
        "create",
        "--title",
        title,
        "--description",
        description,
        "--priority",
        priority,
        "--tags",
        REFACTOR_TAG,
        "--json",
    ]

    try:
        result = subprocess.run(  # noqa: PLW1510
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        LOG.warning("wl command not found; cannot create work item")
        return None
    except subprocess.TimeoutExpired:
        LOG.warning("wl create timed out for smell: %s", smell_type)
        return None
    except OSError as exc:
        LOG.warning("Failed to run wl create: %s", exc)
        return None

    if result.returncode != 0:
        LOG.warning(
            "wl create failed (code %d): %s", result.returncode, result.stderr
        )
        return None

    # Parse the JSON output
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        LOG.warning("Failed to parse wl create output: %s", result.stdout)
        return None

    work_item_id = data.get("workItem", {}).get("id")
    if work_item_id:
        LOG.info("Created work item %s for %s", work_item_id, title)
        return work_item_id

    LOG.warning("wl create output missing workItem.id: %s", result.stdout)
    return None


def group_smells_by_identity(
    smells: list[dict[str, Any]],
    repo_root: str | None = None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group smells by finding identity, preserving first-seen order.

    A batch run that detects the same smell twice (at two lines, or from two
    scan passes) collapses to one group. The returned pairs let callers map
    every original smell back to the one item filed for its identity
    (SA-0MWE1E4GQ004E1JG AC4).
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for smell in smells:
        key = build_finding_key(smell, repo_root)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(smell)
    return [(key, groups[key]) for key in order]


def _merge_smells(smells: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge same-identity smells into one finding with a union of lines."""
    merged = dict(smells[0])
    lines: set[int] = set()
    for smell in smells:
        lines.update(_affected_lines(smell))
    if lines:
        merged["lines"] = sorted(lines)
    return merged


def create_smell_work_items(
    smells: list[dict[str, Any]],
    repo_root: str | None = None,
) -> list[str]:
    """Batch-create Worklog work items for a list of code smells.

    Smells sharing a finding identity are grouped into a single work item
    whose affected-line list is the union of every occurrence, so a batch run
    never files the same smell twice (SA-0MWE1E4GQ004E1JG AC4). Each group is
    still checked for duplicate REFACTOR comments and an existing live item
    before creation.

    Args:
        smells: A list of smell finding dicts.
        repo_root: Optional repository root for path normalisation.

    Returns:
        A list of work item IDs that were successfully created (duplicates
        and failures are excluded), one per identity group.
    """
    results: list[str] = []
    for _key, group in group_smells_by_identity(smells, repo_root):
        work_item_id = create_smell_work_item(_merge_smells(group), repo_root)
        if work_item_id is not None:
            results.append(work_item_id)
    return results


def create_smell_work_items_mapped(
    smells: list[dict[str, Any]],
    repo_root: str | None = None,
) -> dict[str, str | None]:
    """Create one work item per identity and map each key to its item ID.

    Returns:
        ``{finding_key: work_item_id_or_None}`` for every identity group. The
        mapping lets a caller (e.g. REFACTOR comment injection) resolve every
        original smell to the single item filed for its identity.
    """
    mapping: dict[str, str | None] = {}
    for key, group in group_smells_by_identity(smells, repo_root):
        mapping[key] = create_smell_work_item(_merge_smells(group), repo_root)
    return mapping
