"""Code comment injection for the refactor step.

Injects structured REFACTOR comments into source files to track pre-existing
code smells and prevent duplicate work item creation.

Provides:
  - inject_refactor_comment(): Inject a REFACTOR comment into a source file.
  - has_existing_comment(): Check if a REFACTOR comment already exists for a
    given smell type.
  - get_comment_style(): Determine the comment style for a given file path.

Usage:

    from skill.refactor.comment_injection import (
        inject_refactor_comment,
        has_existing_comment,
        get_comment_style,
    )

    smell = {"file": "src/main.py", "smell_type": "security", ...}
    work_item_id = "SA-0MOCK9999X000COMMENT"
    success = inject_refactor_comment("src/main.py", smell, work_item_id)

    # Check for existing comment
    exists = has_existing_comment("src/main.py", "security")
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any


LOG = logging.getLogger("refactor.comment_injection")

# ---------------------------------------------------------------------------
# Comment style definitions
# ---------------------------------------------------------------------------

#: Mapping of file extensions to comment style configurations.
#: Each style defines:
#:   - ``line_prefix``: String prepended to each line of the comment block.
#:   - ``block_open``: Opening marker for block-style comments (or None).
#:   - ``block_close``: Closing marker for block-style comments (or None).
#:   - ``use_block``: If True, uses block_open/block_close instead of
#:     line_prefix for each line.
COMMENT_STYLES: dict[str, dict[str, Any]] = {
    ".py": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".js": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".jsx": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".ts": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".tsx": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".mjs": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".cjs": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".md": {
        "line_prefix": "",
        "block_open": "<!--\n",
        "block_close": "\n-->",
        "use_block": True,
    },
    ".mdx": {
        "line_prefix": "",
        "block_open": "<!--\n",
        "block_close": "\n-->",
        "use_block": True,
    },
    ".yml": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".yaml": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".html": {
        "line_prefix": "",
        "block_open": "<!--\n",
        "block_close": "\n-->",
        "use_block": True,
    },
    ".htm": {
        "line_prefix": "",
        "block_open": "<!--\n",
        "block_close": "\n-->",
        "use_block": True,
    },
    ".css": {
        "line_prefix": " * ",
        "block_open": "/*\n",
        "block_close": "\n */",
        "use_block": True,
    },
    ".scss": {
        "line_prefix": " * ",
        "block_open": "/*\n",
        "block_close": "\n */",
        "use_block": True,
    },
    ".less": {
        "line_prefix": " * ",
        "block_open": "/*\n",
        "block_close": "\n */",
        "use_block": True,
    },
    ".sql": {
        "line_prefix": "-- ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".sh": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".bash": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".zsh": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".fish": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".rb": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".go": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".rs": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".java": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".kt": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".swift": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".php": {
        "line_prefix": "// ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".tf": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".toml": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".ini": {
        "line_prefix": "; ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
    ".cfg": {
        "line_prefix": "# ",
        "block_open": None,
        "block_close": None,
        "use_block": False,
    },
}

# Default style for unknown/unrecognized file extensions.
DEFAULT_STYLE: dict[str, Any] = {
    "line_prefix": "# ",
    "block_open": None,
    "block_close": None,
    "use_block": False,
}

#: Files without an extension that are known to use # style comments.
NO_EXT_HASH_FILES: set[str] = {
    "Dockerfile",
    "Makefile",
    "Gemfile",
    "Rakefile",
    "Procfile",
}

#: Pattern to detect existing REFACTOR comments.
#: Matches the work item ID and smell type from a REFACTOR block.
REFACTOR_PATTERN = re.compile(
    r"REFACTOR-(\S+).*?smell:\s*(\S+)",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_comment_style(file_path: str) -> dict[str, Any]:
    """Determine the comment style for a given file path.

    The style is determined by the file extension. Files without a recognized
    extension use a default ``#`` prefix style. Known files without extensions
    (``Dockerfile``, ``Makefile``, etc.) also use ``#`` prefix style.

    Args:
        file_path: Path to the source file.

    Returns:
        A dict with keys ``line_prefix``, ``block_open``, ``block_close``,
        and ``use_block``.
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    if ext in COMMENT_STYLES:
        return dict(COMMENT_STYLES[ext])

    # Check for known files without extensions
    if ext == "" and path.name in NO_EXT_HASH_FILES:
        return dict(DEFAULT_STYLE)

    # Default fallback
    return dict(DEFAULT_STYLE)


def has_existing_comment(file_path: str, smell_type: str) -> bool:
    """Check if a source file already has a REFACTOR comment for a smell type.

    Prevents duplicate work items for the same code smell. Matching is
    case-insensitive.

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
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return False

    if not content.strip():
        return False

    # Search for REFACTOR comments matching the smell type
    for match in REFACTOR_PATTERN.finditer(content):
        matched_smell = match.group(2).strip().lower()
        if matched_smell == smell_type.lower():
            return True

    return False


def _build_comment_block(
    work_item_id: str,
    smell: dict[str, Any],
    style: dict[str, Any],
) -> str:
    """Build the formatted REFACTOR comment block for a given style.

    Args:
        work_item_id: The Worklog work item ID.
        smell: A smell finding dict with ``smell_type`` and ``message`` keys.
        style: A comment style dict from ``get_comment_style()``.

    Returns:
        A string containing the formatted comment block, including any
        leading newline for separation.
    """
    smell_type = smell.get("smell_type", "unknown")
    message = smell.get("message", "No description provided")
    severity = smell.get("severity", "")

    if style["use_block"] and style["block_open"] and style["block_close"]:
        # Block-style comment (e.g., Markdown, HTML)
        lines = [
            f"REFACTOR-{work_item_id}",
            f"smell: {smell_type}",
        ]
        if severity:
            lines.append(f"severity: {severity}")
        lines.append(f"description: {message}")
        inner = "\n".join(lines)
        block = style["block_open"] + inner + style["block_close"]
    else:
        # Line-prefixed comment (e.g., Python #, JS //)
        prefix = style.get("line_prefix", "# ")
        lines = [
            f"{prefix}<!-- REFACTOR-{work_item_id}",
            f"{prefix}smell: {smell_type}",
        ]
        if severity:
            lines.append(f"{prefix}severity: {severity}")
        lines.extend([
            f"{prefix}description: {message}",
            f"{prefix}-->",
        ])
        block = "\n".join(lines)

    return "\n" + block + "\n"


def inject_refactor_comment(
    file_path: str,
    smell: dict[str, Any],
    work_item_id: str,
) -> bool:
    """Inject a REFACTOR comment into a source file.

    The comment is placed at the top of the file. If a REFACTOR comment
    already exists for the same smell type, the injection is skipped and
    ``False`` is returned.

    Args:
        file_path: Path to the source file to modify.
        smell: A smell finding dict with at least ``smell_type`` and
               ``message`` keys.
        work_item_id: The Worklog work item ID to reference in the comment.

    Returns:
        ``True`` if the comment was successfully injected, ``False`` if it
        was skipped (duplicate), the file doesn't exist, or an error occurred.
    """
    # Validate inputs
    if not file_path or not work_item_id:
        LOG.warning("Invalid arguments: file_path=%r, work_item_id=%r", file_path, work_item_id)
        return False

    if not os.path.isfile(file_path):
        LOG.warning("File does not exist: %s", file_path)
        return False

    smell_type = smell.get("smell_type", "unknown")

    # Check for existing duplicate comment
    if has_existing_comment(file_path, smell_type):
        LOG.info(
            "Skipping injection for %s in %s: duplicate REFACTOR comment exists",
            smell_type,
            file_path,
        )
        return False

    # Determine comment style
    style = get_comment_style(file_path)

    # Build the comment block
    comment_block = _build_comment_block(work_item_id, smell, style)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            original_content = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        LOG.warning("Failed to read %s: %s", file_path, exc)
        return False

    # Place comment at the top of the file
    new_content = comment_block + original_content

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except (OSError, PermissionError) as exc:
        LOG.warning("Failed to write to %s: %s", file_path, exc)
        return False

    LOG.info(
        "Injected REFACTOR comment for %s into %s (work item: %s)",
        smell_type,
        file_path,
        work_item_id,
    )
    return True
