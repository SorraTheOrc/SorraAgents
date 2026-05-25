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

    def test_persist_flag_documented(self):
        """SKILL.md must document the --persist flag."""
        text = _skill_md_text()
        assert "--persist" in text

    def test_pi_bin_flag_documented(self):
        """SKILL.md must document the --pi-bin flag."""
        text = _skill_md_text()
        assert "--pi-bin" in text

    def test_model_flag_documented(self):
        """SKILL.md must document the --model flag."""
        text = _skill_md_text()
        assert "--model" in text

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
