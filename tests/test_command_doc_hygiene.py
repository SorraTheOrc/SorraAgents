"""Doc hygiene tests for command markdown files.

Verifies that command .md files contain required sections, such as
status management instructions, and follow project conventions.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_MD = REPO_ROOT / "command" / "review.md"


def test_review_md_has_status_management_instructions():
    """command/review.md must include instructions for setting in_progress and open status."""
    content = REVIEW_MD.read_text()

    assert "in_progress" in content, "review.md must reference setting status to in_progress"
    assert "open --json" in content, "review.md must reference setting status to open at end"
