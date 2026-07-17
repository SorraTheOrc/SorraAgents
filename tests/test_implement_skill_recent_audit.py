"""Tests for recent-audit startup behavior in the implement skill docs.

These tests verify that `skill/implement/SKILL.md` instructs implement runs to
prefer a recent audit before performing a fresh audit, and that the fallback
path explicitly invokes `/skill:audit <work-item-id>` when no recent audit is
available.

Related work item: SA-0MPGU5QG40040AWS
"""

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"


def _find_step(content: str, step_heading: str) -> str | None:
    """Extract a numbered step section from the markdown document."""
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(step_heading)}.*?(?=^\s*\d+\.\s+|\Z)",
    )
    match = pattern.search(content)
    if not match:
        return None
    return match.group(0)


def _skill_content() -> str:
    assert _SKILL_MD.exists(), f"implement skill doc not found at {_SKILL_MD}"
    return _SKILL_MD.read_text(encoding="utf-8")


class TestImplementSkillRecentAuditBootstrap:
    """Ensure the implement skill starts by reusing a recent audit when possible."""

    def test_step_1_mentions_recent_audit_lookup(self) -> None:
        """Step 1 must tell the agent to look for a recent audit first."""
        content = _skill_content()
        step1 = _find_step(content, "1. Understand the work item")
        assert step1 is not None, "Could not find the 'Understand the work item' step"

        recent_audit_patterns = [
            r"recent audit",
            r"most recent action",
            r"reuse.*audit",
        ]
        assert any(re.search(pat, step1, re.IGNORECASE | re.DOTALL) for pat in recent_audit_patterns), (
            "The 'Understand the work item' step must instruct implement to look for "
            "a recent audit before doing any more work."
        )

    def test_step_1_distinguishes_reuse_from_fresh_audit(self) -> None:
        """Step 1 must distinguish between reusing an existing audit and running a fresh one."""
        content = _skill_content()
        step1 = _find_step(content, "1. Understand the work item")
        assert step1 is not None, "Could not find the 'Understand the work item' step"

        assert re.search(r"if.*recent audit.*use.*audit", step1, re.IGNORECASE | re.DOTALL), (
            "Step 1 must say that a recent audit should be reused to establish the work."
        )
        assert re.search(r"if.*no recent audit.*run.*audit", step1, re.IGNORECASE | re.DOTALL), (
            "Step 1 must say that implement should run a full audit when no recent audit exists."
        )

    def test_step_3_fallback_uses_skill_audit_command(self) -> None:
        """Step 3 must explicitly invoke `/skill:audit <id>` when no recent audit exists."""
        content = _skill_content()
        step3 = _find_step(content, "1. Implement")
        assert step3 is not None, "Could not find the 'Implement' step"

        assert "/skill:audit <id>" in step3 or "/skill:audit <work-item-id>" in step3, (
            "The implement step must explicitly instruct the agent to call `/skill:audit <id>` "
            "when no recent audit is available."
        )
        assert re.search(r"establish work needed", step3, re.IGNORECASE), (
            "The fallback audit path must say that implement runs the audit to establish what work remains."
        )

    def test_step_3_preserves_existing_workflow_after_audit_selection(self) -> None:
        """The audit bootstrap must feed into the existing implementation flow."""
        content = _skill_content()
        step3 = _find_step(content, "1. Implement")
        assert step3 is not None, "Could not find the 'Implement' step"

        assert re.search(r"audit.*[Ww]rite tests", step3, re.IGNORECASE | re.DOTALL), (
            "The implement step should preserve the existing implementation workflow after audit selection."
        )


