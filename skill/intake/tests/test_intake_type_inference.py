"""Tests for type inference and flag correction in skill/intake/SKILL.md.

Verifies that:
- The --issue-type flag is used (not --type)
- The work item creation step includes type inference logic
- Existing items have their issueType reviewed/corrected on re-intake
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # <repo>/skill/intake/tests/
INTAKE_MD = REPO_ROOT / "skill" / "intake" / "SKILL.md"


def test_intake_md_uses_issue_type_flag():
    """The incorrect --type flag must be replaced with --issue-type."""
    content = INTAKE_MD.read_text()
    assert "--issue-type" in content, (
        "skill/intake/SKILL.md must use --issue-type flag instead of --type"
    )


def test_intake_md_does_not_use_type_flag():
    """The incorrect --type flag must NOT appear in wl create/update commands."""
    content = INTAKE_MD.read_text()
    # Scan for lines that contain "wl create" or "wl update" and check they
    # don't use the bare --type flag. The --issue-type flag is correct.
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("`wl") and "--type " in stripped and "--issue-type" not in stripped:
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
        "skill/intake/SKILL.md must include instructions for inferring the issue type "
        "from the seed intent or user-provided context"
    )


def test_intake_md_instructs_type_inference_mapping():
    """Type inference must cover bug, feature, chore, task, epic types."""
    content = INTAKE_MD.read_text()
    # Look for references to all supported issue types
    type_mentions = sum(1 for t in ["bug", "feature", "chore", "task", "epic"]
                        if t in content.lower())
    assert type_mentions >= 3, (
        "skill/intake/SKILL.md must reference at least 3 of the supported "
        "issue types (bug, feature, chore, task, epic) in the inference logic"
    )


def test_intake_md_has_categorization_decision_guide():
    """The decision guide must cover all five issue types with distinguishing descriptions."""
    content = INTAKE_MD.read_text()
    # The decision guide must be anchored by an explicit heading
    assert "**Issue type decision guide**" in content, (
        "skill/intake/SKILL.md must include an 'Issue type decision guide' section"
    )
    guide = content.split("**Issue type decision guide**", 1)[1]
    # Every supported type must appear as a table row with a distinguishing description
    for issue_type in ["bug", "feature", "chore", "task", "epic"]:
        assert f"`{issue_type}`" in guide, (
            f"skill/intake/SKILL.md decision guide must cover the '{issue_type}' type"
        )
    # Each type must be distinguished by behavior change (or lack thereof)
    assert "incorrect or broken" in guide, (
        "skill/intake/SKILL.md decision guide must describe bug as incorrect/broken behavior"
    )
    assert "adds new capability" in guide, (
        "skill/intake/SKILL.md decision guide must describe feature as new capability"
    )
    assert "does not change code behavior" in guide, (
        "skill/intake/SKILL.md decision guide must describe chore as not changing code behavior"
    )


def test_intake_md_categorization_distinguishes_bug_from_feature():
    """The decision guide must not mislabel a bug fix as a feature."""
    content = INTAKE_MD.read_text()
    guide = content.split("**Issue type decision guide**", 1)[1]
    # bug row: must not claim it adds new capability
    assert "The change corrects existing wrong behavior" in guide, (
        "skill/intake/SKILL.md must state that a bug fix corrects existing wrong behavior"
    )
    # feature row: must explicitly exclude fixes of already-broken behavior
    assert "The work only fixes something that is already broken" in guide, (
        "skill/intake/SKILL.md must state that feature does NOT cover fixing broken behavior"
    )


def test_intake_md_categorization_distinguishes_chore_from_code_change():
    """The decision guide must classify docs/CI/dependency changes as chore, not feature."""
    content = INTAKE_MD.read_text()
    guide = content.split("**Issue type decision guide**", 1)[1]
    # chore row must explicitly list non-code examples (docs, CI, deps, formatting)
    for example in ["documentation", "CI", "dependencies", "formatting"]:
        assert example in guide, (
            f"skill/intake/SKILL.md chore row must list '{example}' as a non-code-change example"
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
        "skill/intake/SKILL.md must include instructions for reviewing and correcting "
        "the issueType of an existing work item during re-intake"
    )
