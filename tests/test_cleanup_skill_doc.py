"""Smoke tests for the cleanup skill audit gate documentation.

Verifies that skill/cleanup/SKILL.md contains the Step 0 work-item audit gate
required by SA-0MLPU8H3B1LWK3B3 (and its children SA-0MM1AW0LT1RDF49Z,
SA-0MM1AWCYD0LNMYMV, SA-0MM1AWO6D1RVZF3I): the branch-name work-item lookup,
audit invocation, decision rules, status transition on pass, and abort
behavior on unmet/partial/no-criteria verdicts.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skill" / "cleanup" / "SKILL.md"


def _skill_md_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _step0_section() -> str:
    text = _skill_md_text()
    start = text.find("### 0. Work-item audit gate")
    end = text.find("### 1. Inspect current branch")
    assert start != -1, "Step 0 heading must exist"
    assert end != -1 and end > start, "Step 1 must follow Step 0"
    return text[start:end]


class TestCleanupAuditGate:
    """Assert the Step 0 audit gate contract is documented in SKILL.md."""

    def test_step0_before_step1(self):
        """Step 0 must appear before Step 1."""
        text = _skill_md_text()
        assert text.find("### 0. Work-item audit gate") < text.find(
            "### 1. Inspect current branch"
        )

    def test_work_item_id_lookup_documented(self):
        """Step 0 must read work_item_id from inspect_current_branch.py."""
        section = _step0_section()
        assert "inspect_current_branch.py" in section
        assert "work_item_id" in section

    def test_skip_when_no_work_item(self):
        """Step 0 must be skipped when no work-item ID is present."""
        section = _step0_section()
        assert "No `work_item_id`" in section
        assert "skip this step" in section

    def test_audit_skill_invocation_documented(self):
        """Step 0 must invoke the existing audit skill, not new logic."""
        section = _step0_section()
        assert "/skill:audit" in section or "audit_runner.py issue" in section
        assert "skill/audit/SKILL.md" in section

    def test_decision_rule_met_proceeds(self):
        """All-met verdict must proceed to Step 1."""
        section = _step0_section()
        assert "Ready to close: Yes" in section
        assert "Proceed to Step 1" in section

    def test_decision_rule_unmet_partial_aborts(self):
        """Any unmet/partial criterion must abort cleanup."""
        section = _step0_section()
        assert "unmet" in section and "partial" in section
        assert "abort cleanup" in section

    def test_decision_rule_no_criteria_aborts(self):
        """No acceptance criteria defined must abort cleanup."""
        section = _step0_section()
        assert "No acceptance criteria defined" in section
        assert "abort cleanup" in section

    def test_status_transition_documented(self):
        """Pass must transition to status=completed, stage=in_review."""
        section = _step0_section()
        assert "wl update <work-item-id> --status completed --stage in_review" in section

    def test_pass_comment_documented(self):
        """Pass must add a comment with branch context."""
        section = _step0_section()
        assert "wl comment add <work-item-id>" in section
        assert "Cleanup audit passed on branch" in section
        assert "in_review" in section

    def test_abort_skips_all_branch_steps(self):
        """Abort must skip all branch operations (Steps 1-8)."""
        section = _step0_section()
        assert "skip Steps 1-8 entirely" in section

    def test_existing_steps_unchanged(self):
        """Existing steps 1-8 must still be present and in order."""
        text = _skill_md_text()
        for step in [
            "### 1. Inspect current branch",
            "### 2. Handle uncommitted/unpushed changes",
            "### 3. Switch to default and update",
            "### 4. Summarize branches",
            "### 5. Delete local merged branches",
            "### 6. Delete remote merged branches",
            "### 7. Handle remaining branches",
            "### 8. Clean up temp files and report",
        ]:
            assert step in text, f"Missing {step}"
        positions = [text.find(step) for step in [
            "### 1. Inspect current branch",
            "### 2. Handle uncommitted/unpushed changes",
            "### 3. Switch to default and update",
            "### 4. Summarize branches",
            "### 5. Delete local merged branches",
            "### 6. Delete remote merged branches",
            "### 7. Handle remaining branches",
            "### 8. Clean up temp files and report",
        ]]
        assert positions == sorted(positions)
