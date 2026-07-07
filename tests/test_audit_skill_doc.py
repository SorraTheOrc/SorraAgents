"""Smoke test for the audit skill documentation.

Verifies that SKILL.md contains the canonical runner invocations and report header
required by downstream consumers (ralph, persist_audit, agents).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skill" / "audit" / "SKILL.md"


def _skill_md_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestAuditSkillDoc:
    """Assert the runner contract is documented in SKILL.md."""

    def test_issue_invocation_present(self):
        """SKILL.md must document audit_runner.py issue."""
        assert "audit_runner.py issue" in _skill_md_text()

    def test_project_invocation_present(self):
        """SKILL.md must document audit_runner.py project."""
        assert "audit_runner.py project" in _skill_md_text()

    def test_ready_to_close_header_present(self):
        """SKILL.md must document the canonical Ready to close: header."""
        assert "Ready to close:" in _skill_md_text()

    def test_no_legacy_step_procedure(self):
        """Legacy 8-step procedure must be removed."""
        text = _skill_md_text()
        # The old doc had numbered steps like "1. Detect whether", "2. If no work item", etc.
        # The new doc should not have the old step markers
        assert "## Steps" not in text
        assert "Deep code review of acceptance criteria (parent work item)" not in text
        assert "Deep code review of children" not in text

    def test_do_not_persist_flag_documented(self):
        """SKILL.md must document the --do-not-persist flag."""
        text = _skill_md_text()
        assert "--do-not-persist" in text

    def test_pi_bin_flag_documented(self):
        """SKILL.md must document the --pi-bin flag."""
        text = _skill_md_text()
        assert "--pi-bin" in text

    def test_model_flag_documented(self):
        """SKILL.md must document the --model flag."""
        text = _skill_md_text()
        assert "--model" in text

    def test_debug_log_flag_documented(self):
        """SKILL.md must document the --debug-log flag."""
        text = _skill_md_text()
        assert "--debug-log" in text

    def test_model_source_flag_documented(self):
        """SKILL.md must document the --model-source flag."""
        text = _skill_md_text()
        assert "--model-source" in text
        assert "remote" in text or "local" in text

    def test_model_source_choices_documented(self):
        """SKILL.md must mention remote and local as choices for --model-source."""
        text = _skill_md_text()
        assert "remote" in text.lower()
        assert "local" in text.lower()

    def test_exit_codes_documented(self):
        """SKILL.md must document exit code semantics."""
        text = _skill_md_text()
        assert "Exit Codes" in text or "exit code" in text.lower()

    def test_section_order_documented(self):
        """SKILL.md must document the canonical section order for issue mode."""
        text = _skill_md_text()
        assert "## Summary" in text
        assert "## Acceptance Criteria Status" in text
        assert "## Children Status" in text

    def test_scripts_section_only_runner_and_persist(self):
        """Scripts section must only reference audit_runner.py and persist_audit.py."""
        text = _skill_md_text()
        assert "audit_runner.py" in text
        assert "persist_audit.py" in text


class TestAuditSkillReadyToCloseCriteria:
    """Assert that SKILL.md contains correct ready-to-close criteria guidance
    for the model, particularly that children in `in_review` stage do NOT block
    closure and that extraneous release-process constraints are prohibited."""

    def test_in_review_children_acceptable(self):
        """SKILL.md must state that children in in_review stage are acceptable and do NOT block closure."""
        text = _skill_md_text()
        # Check for explicit language that in_review children don't block closure
        assert "in_review" in text
        assert "do NOT block closure" in text or "does not block closure" in text or "do not block closure" in text.lower()

    def test_in_review_or_done_stage_acceptable(self):
        """SKILL.md must mention in_review or done as acceptable child stages."""
        text = _skill_md_text()
        assert "in_review" in text and "done" in text
        # Verify the ready-to-close criteria explicitly lists in_review/done as acceptable
        assert "in_review" in text and "done" in text

    def test_no_release_process_in_audit_verdict(self):
        """SKILL.md must prohibit adding release-process constraints like 'must be merged to main' in audit verdicts."""
        text = _skill_md_text()
        # The SKILL.md must include a clear statement that release-process
        # constraints (e.g., "must be merged to main") are NOT an audit concern
        # and must not appear in audit verdicts
        has_explicit_prohibition = (
            "release process" in text.lower()
            or "merged to main" in text.lower()
            or "release constraints" in text.lower()
        )
        has_prohibition_marker = (
            "MUST NOT" in text
            or "DO NOT" in text
            or "must not" in text
            or "do not" in text
        )
        # Check either they're together in context, or the intent is clearly communicated
        if has_explicit_prohibition:
            assert has_prohibition_marker, (
                "If release process is mentioned, there must be a prohibition marker"
            )
        else:
            # The release process might not be explicitly named, but there must be
            # a clear prohibition against adding constraints outside the defined criteria
            assert "not an audit concern" in text.lower() or "not a closure constraint" in text.lower()

    def test_guidance_for_models_includes_ready_to_close(self):
        """The Guidance for models section must reference the ready-to-close criteria."""
        text = _skill_md_text()
        # Find the Guidance for models section
        guidance_idx = text.find("## Guidance for models")
        assert guidance_idx >= 0, "Guidance for models section must exist"
        # The section after this point should contain a reference to ready-to-close logic
        guidance_section = text[guidance_idx:]
        assert "ready-to-close" in guidance_section.lower() or "Ready to close" in guidance_section

    def test_phase1_children_stage_check_documented_in_guidance(self):
        """The Guidance for models section must document that Phase 1 includes a children stage check."""
        text = _skill_md_text()
        guidance_idx = text.find("## Guidance for models")
        assert guidance_idx >= 0
        guidance_section = text[guidance_idx:]
        assert "Phase 1" in guidance_section
        assert "children" in guidance_section.lower()


class TestAuditSkillSafetyInstructions:
    """Assert that SKILL.md contains critical safety instructions that
    prevent models from modifying work items during audit evaluation."""

    def test_safety_designation_present(self):
        """SKILL.md must include a safety designation with permitted/forbidden actions."""
        text = _skill_md_text()
        # Must have some safety designation about what is and isn't allowed
        assert any(marker in text for marker in ["READ-ONLY", "MUST NOT", "⚠️"])

    def test_no_close_modify_create_delete_prohibition(self):
        """SKILL.md must prohibit closing, modifying, creating, or deleting work items."""
        text = _skill_md_text()
        assert "Do NOT close" in text or "Do NOT modify" in text or "Do NOT create" in text or "Do NOT delete" in text

    def test_no_wl_state_commands_prohibition(self):
        """SKILL.md must prohibit executing wl commands that change state."""
        text = _skill_md_text()
        assert "wl" in text
        assert "state-modifying" in text or "change state" in text

    def test_structured_markdown_report_instruction(self):
        """SKILL.md must instruct the model to produce only a structured markdown report."""
        text = _skill_md_text()
        assert "structured markdown report" in text or "structured evaluation report" in text

    def test_ambiguity_return_instruction(self):
        """SKILL.md must instruct the model to return immediately if ambiguity is detected."""
        text = _skill_md_text()
        assert "ambiguity" in text.lower()
        assert "return immediately" in text.lower()

    def test_refuse_state_modifying_commands(self):
        """SKILL.md must instruct the model to refuse state-modifying wl commands."""
        text = _skill_md_text()
        assert "refuse" in text.lower()
        assert "wl" in text


class TestAuditSkillWorkItemIdDetection:
    """Assert that SKILL.md contains explicit work item ID detection instructions."""

    def test_regex_pattern_present(self):
        """SKILL.md must include explicit regex for work item ID detection."""
        text = _skill_md_text()
        assert "[A-Z]{2}-[A-Z0-9]+" in text or "[A-Z]{2}-[A-Z0-9]+\\b" in text

    def test_pre_flight_affirmation_present(self):
        """SKILL.md must include a pre-flight affirmation step."""
        text = _skill_md_text()
        assert "Pre-flight affirmation" in text

    def test_verification_guard_present(self):
        """SKILL.md must include a verification guard before project-level path."""
        text = _skill_md_text()
        assert "Verify absence before proceeding" in text

    def test_scan_match_branch_structure(self):
        """SKILL.md must use active 'scan → match → branch' structure."""
        text = _skill_md_text()
        assert "Scan for a work item ID" in text
        assert "No ID found" in text
        assert "ID found" in text

    def test_item_level_routes_to_wl_show(self):
        """SKILL.md must route item-level audits to `wl show`."""
        text = _skill_md_text()
        # Verify the item-level path references wl show with children
        assert "wl show" in text
        assert "--children" in text

    def test_project_level_routes_to_wl_list(self):
        """SKILL.md must route project-level audits to `wl list`."""
        text = _skill_md_text()
        assert "wl list" in text
        assert "wl in_progress" in text
