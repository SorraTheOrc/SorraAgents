"""Tests for priority classification in skill/intake/SKILL.md.

Verifies that:
- A Priority Classification section exists and is structured like the Issue Type guide
- All four priority levels (critical, high, medium, low) are covered
- The precedence rule is documented (operator-specified values take priority)
- Decision procedure is present for inferring priority
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # <repo>/skill/intake/tests/
INTAKE_MD = REPO_ROOT / "skill" / "intake" / "SKILL.md"
INTAKE_REF = REPO_ROOT / "docs" / "dev" / "intake-skill-reference.md"


def _intake_docs() -> str:
    """Return SKILL.md + reference-doc content."""
    parts = [INTAKE_MD.read_text()]
    if INTAKE_REF.exists():
        parts.append(INTAKE_REF.read_text())
    return "\n".join(parts)


def test_priority_guide_section_exists_in_skill_md():
    """SKILL.md must include a Priority Classification section."""
    content = INTAKE_MD.read_text()
    assert "**Priority classification guide**" in content, (
        "skill/intake/SKILL.md must include a 'Priority classification guide' section"
    )


def test_priority_guide_section_exists_in_reference_doc():
    """The reference doc must also include the Priority Classification section."""
    content = INTAKE_REF.read_text()
    assert "**Priority classification guide**" in content, (
        "docs/dev/intake-skill-reference.md must include a 'Priority classification guide' section"
    )


def test_priority_guide_covers_all_four_levels():
    """The priority guide must cover all four priority levels."""
    content = _intake_docs()
    priority_guide = content.split("**Priority classification guide**", 1)[1]
    for priority in ["critical", "high", "medium", "low"]:
        assert f"`{priority}`" in priority_guide, (
            f"priority guide must cover the '{priority}' priority level"
        )


def test_priority_guide_has_decision_procedure():
    """The priority guide must include a decision procedure for when priority is unspecified."""
    content = _intake_docs()
    priority_guide = content.split("**Priority classification guide**", 1)[1]
    assert "**Decision procedure**" in priority_guide, (
        "priority guide must include a 'Decision procedure' section"
    )
    # The decision procedure should ask about security/data loss for critical
    assert "security" in priority_guide.lower() or "security" in priority_guide.lower(), (
        "decision procedure should reference security for critical priority"
    )


def test_priority_guide_documentation_precedence_rule():
    """The guide must document that operator-specified values take precedence."""
    content = _intake_docs()
    assert "precedence" in content.lower(), (
        "priority guide must document that operator-specified priority takes precedence"
    )


def test_priority_guide_consulted_after_operator_values():
    """The guide must state it is consulted only when the operator has not specified priority."""
    content = _intake_docs()
    priority_section = content.split("**Priority classification guide**", 1)[1]
    assert "operator" in priority_section.lower(), (
        "priority guide must reference operator-supplied values"
    )
    assert "fallback" in priority_section.lower() or "not supplied" in priority_section.lower(), (
        "priority guide must clarify it is a fallback when no operator value is provided"
    )


def test_priority_guide_has_example_values():
    """Each priority level should have example scenarios."""
    content = _intake_docs()
    priority_guide = content.split("**Priority classification guide**", 1)[1]
    # Check for at least some example content
    example_indicators = ["patching", "shipping", "minor", "cosmetic", "bug", "feature"]
    found = sum(1 for e in example_indicators if e.lower() in priority_guide.lower())
    assert found >= 4, (
        f"priority guide examples should cover at least 4 of: {example_indicators}; "
        f"found {found}"
    )


def test_priority_guide_table_format():
    """The priority guide must use a Markdown table format consistent with the type guide."""
    content = _intake_docs()
    priority_guide = content.split("**Priority classification guide**", 1)[1]
    # A table row should have the pipe-separated format
    assert "| `critical`" in priority_guide and "| `high`" in priority_guide, (
        "priority guide must use table format with pipe-separated columns"
    )
    # Check for header row
    assert "Use when" in priority_guide, (
        "priority guide table must include a 'Use when' column header"
    )
