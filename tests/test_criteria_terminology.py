"""Terminology normalization tests for Acceptance Criteria / Success Criteria.

Related work item: make success criteria and acceptance criteria synonyms (SA-0MP3YN1HH000SCX9)
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTAKE_MD = _REPO_ROOT / "command" / "intake.md"
_PLAN_MD = _REPO_ROOT / "command" / "plan.md"
_AUDIT_SKILL = _REPO_ROOT / "skill" / "audit" / "SKILL.md"
_WORKFLOW_JSON = _REPO_ROOT / "docs" / "workflow" / "workflow.json"
_WORKFLOW_YAML = _REPO_ROOT / "docs" / "workflow" / "workflow.yaml"
_README = _REPO_ROOT / "README.md"


def test_intake_uses_acceptance_criteria_as_canonical_term() -> None:
    content = _INTAKE_MD.read_text(encoding="utf-8")

    assert "Acceptance Criteria" in content
    assert "Acceptance Criteria (synonym: Success Criteria)" in content


def test_plan_mentions_success_criteria_as_synonym() -> None:
    content = _PLAN_MD.read_text(encoding="utf-8")

    assert "Acceptance Criteria" in content
    assert "Success Criteria" in content


def test_audit_skill_recognizes_both_section_headings() -> None:
    content = _AUDIT_SKILL.read_text(encoding="utf-8")
    assert "## Acceptance Criteria" in content
    assert "## Success Criteria" in content


def test_workflow_requires_acceptance_criteria_invariant_accepts_both_terms_json() -> None:
    descriptor = json.loads(_WORKFLOW_JSON.read_text(encoding="utf-8"))
    invariant = next(
        inv
        for inv in descriptor["invariants"]
        if inv["name"] == "requires_acceptance_criteria"
    )
    logic = invariant["logic"].lower()
    assert "acceptance criteria" in logic
    assert "success criteria" in logic


def test_workflow_requires_acceptance_criteria_invariant_accepts_both_terms_yaml() -> None:
    descriptor = yaml.safe_load(_WORKFLOW_YAML.read_text(encoding="utf-8"))
    invariant = next(
        inv
        for inv in descriptor["invariants"]
        if inv["name"] == "requires_acceptance_criteria"
    )
    logic = invariant["logic"].lower()
    assert "acceptance criteria" in logic
    assert "success criteria" in logic


def test_readme_documents_terminology_policy() -> None:
    content = _README.read_text(encoding="utf-8")
    assert "Acceptance Criteria" in content
    assert "Success Criteria" in content
