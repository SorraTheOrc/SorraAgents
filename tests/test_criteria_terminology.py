"""Terminology normalization tests for Acceptance Criteria / Success Criteria.

Related work item: make success criteria and acceptance criteria synonyms (SA-0MP3YN1HH000SCX9)
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTAKE_MD = _REPO_ROOT / "skill" / "intake" / "SKILL.md"
_PLAN_MD = _REPO_ROOT / "skill" / "plan" / "SKILL.md"
_AUDIT_SKILL = _REPO_ROOT / "skill" / "audit" / "SKILL.md"
_README = _REPO_ROOT / "README.md"


def test_intake_uses_acceptance_criteria_as_canonical_term() -> None:
    content = _INTAKE_MD.read_text(encoding="utf-8")

    assert "Acceptance Criteria" in content
    assert "Acceptance Criteria (synonym: Success Criteria)" in content


def test_skill_mentions_both_terms() -> None:
    content = _PLAN_MD.read_text(encoding="utf-8")

    assert "Acceptance Criteria" in content
    assert "Success Criteria" in content


def test_audit_skill_recognizes_both_section_headings() -> None:
    content = _AUDIT_SKILL.read_text(encoding="utf-8")
    assert "## Acceptance Criteria" in content
    assert "## Success Criteria" in content


def test_readme_documents_terminology_policy() -> None:
    content = _README.read_text(encoding="utf-8")
    assert "Acceptance Criteria" in content
    assert "Success Criteria" in content
