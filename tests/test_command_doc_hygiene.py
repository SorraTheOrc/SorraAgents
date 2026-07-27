"""Doc hygiene tests for command markdown files.

Verifies that command .md files contain required sections, such as
status management instructions, and follow project conventions.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_MD = REPO_ROOT / "command" / "audit.md"


def test_audit_md_exists():
    """command/audit.md must exist as a file."""
    assert AUDIT_MD.is_file(), "command/audit.md must exist"


def test_audit_md_has_description():
    """command/audit.md must have a YAML frontmatter description."""
    content = AUDIT_MD.read_text()
    assert content.startswith("---"), "command/audit.md must have YAML frontmatter"
    assert "description:" in content, "command/audit.md must have a description field"


def test_audit_md_references_runner():
    """command/audit.md must reference the audit runner script."""
    content = AUDIT_MD.read_text()
    assert "audit_runner.py" in content, "audit.md must reference audit_runner.py"


def test_audit_md_has_immediate_execution_instruction():
    """command/audit.md must instruct immediate execution without asking."""
    content = AUDIT_MD.read_text()
    assert "immediately" in content.lower() or "do NOT ask" in content, (
        "audit.md must instruct immediate execution without asking permission"
    )


def test_audit_md_accepts_work_item_argument():
    """command/audit.md must accept a work item ID argument."""
    content = AUDIT_MD.read_text()
    assert "$1" in content or "$ARGUMENTS" in content or "<work-item" in content, (
        "audit.md must reference a work item argument"
    )
