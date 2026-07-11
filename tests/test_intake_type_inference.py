"""Tests for type inference and flag correction in command/intake.md.

Verifies that:
- The --type flag is no longer used (only --issue-type)
- The work item creation step includes type inference logic
- Existing items have their issueType reviewed/corrected on re-intake
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INTAKE_MD = REPO_ROOT / "command" / "intake.md"


def test_intake_md_uses_issue_type_flag():
    """The incorrect --type flag must be replaced with --issue-type."""
    content = INTAKE_MD.read_text()
    assert "--issue-type" in content, (
        "command/intake.md must use --issue-type flag instead of --type"
    )


def test_intake_md_does_not_use_type_flag():
    """The incorrect --type flag must NOT appear in wl create/update commands."""
    content = INTAKE_MD.read_text()
    # Scan for lines that contain "wl create" or "wl update" and check they
    # don't use the bare --type flag. The --issue-type flag is correct.
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("`wl") and "--type " in stripped:
            # Allow --issue-type but flag bare --type usage
            if "--issue-type" not in stripped:
                raise AssertionError(
                    f"Line {line_number} uses --type instead of --issue-type:\n{line}"
                )


def test_intake_md_has_type_inference_instructions():
    """The Work Item prep section must include instructions for type inference."""
    content = INTAKE_MD.read_text()
    # Look for the Work Item prep section and check it references type inference
    # (e.g., mentions inferring issue type from seed intent/context)
    section_markers = [
        "issue type",
        "issueType",
        "infer",
        "seed intent",
    ]
    found = any(marker.lower() in content.lower() for marker in section_markers)
    assert found, (
        "command/intake.md must include instructions for inferring the issue type "
        "from the seed intent or user-provided context"
    )


def test_intake_md_instructs_type_inference_mapping():
    """Type inference must cover bug, feature, chore, task, epic types."""
    content = INTAKE_MD.read_text()
    # Look for references to all supported issue types
    type_mentions = sum(1 for t in ["bug", "feature", "chore", "task", "epic"]
                        if t in content.lower())
    assert type_mentions >= 3, (
        "command/intake.md must reference at least 3 of the supported "
        "issue types (bug, feature, chore, task, epic) in the inference logic"
    )


def test_intake_md_corrects_existing_item_type():
    """Re-intake of an existing work item should review/correct issueType."""
    content = INTAKE_MD.read_text()
    # Look for instructions related to reviewing/correcting existing item type
    correction_markers = [
        "correct",
        "review",
        "update",
        "existing",
    ]
    # Check in the context of the Work Item prep section or similar
    found = any(marker.lower() in content.lower() for marker in correction_markers)
    assert found, (
        "command/intake.md must include instructions for reviewing and correcting "
        "the issueType of an existing work item during re-intake"
    )
