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


def _find_section(content: str, heading: str) -> str | None:
    """Extract a section by heading (## or ###).

    Supports both old numbered format (e.g., "1. Understand the work item")
    and new heading format (e.g., "### Step 1 — Start").
    """
    # Try exact heading match first
    section_pattern = re.compile(rf"^#+\s*{re.escape(heading)}", re.MULTILINE)
    match = section_pattern.search(content)
    if match:
        start = match.start()
        level = len(match.group(0).split()[0])
        rest = content[match.end():]
        next_heading = re.search(rf'^#{{1,{level}}}\s+\S', rest, re.MULTILINE)
        if next_heading:
            end = match.end() + next_heading.start()
        else:
            end = len(content)
        return content[start:end]

    # For new format with "### Step N — <topic>"
    # Extract topic from old-style heading like "1. Understand the work item"
    topic = re.sub(r"^[\d\.\s#-]+", "", heading).strip()
    if topic:
        # Try matching "### Step N — <topic>"
        step_pattern = re.compile(
            rf"(?ms)^\s*###\s+Step\s+\d+\s+[—\-]\s+.*?{re.escape(topic)}.*?(?=^\s*###\s+Step|\Z)",
        )
        match2 = step_pattern.search(content)
        if match2:
            return match2.group(0)

    return None


def _find_audit_section(content: str) -> str | None:
    """Find the audit-related section in the document.

    Returns the content of the "Audit self-check" subsection if found,
    or the audit-relevant portion of the Lifecycle Summary.
    """
    # Try "Audit self-check" subsection
    pattern = re.compile(
        r"(?ms)^\s*####\s+Audit self-check.*?(?=^\s*###\s+Step|\Z)",
    )
    match = pattern.search(content)
    if match:
        return match.group(0)

    # Fallback: find the lifecycle summary audit lines
    lifecycle = re.search(r"(?ms)│ 1\. Understand the work item.*?recent audit", content)
    if lifecycle:
        return lifecycle.group(0)

    return None


def _skill_content() -> str:
    assert _SKILL_MD.exists(), f"implement skill doc not found at {_SKILL_MD}"
    return _SKILL_MD.read_text(encoding="utf-8")


class TestImplementSkillRecentAuditBootstrap:
    """Ensure the implement skill starts by reusing a recent audit when possible."""

    def test_step_1_mentions_recent_audit_lookup(self) -> None:
        """The document must include guidance to look for a recent audit first."""
        content = _skill_content()
        audit_section = _find_audit_section(content)
        assert audit_section is not None, "Could not find audit guidance section"

        recent_audit_patterns = [
            r"recent audit",
            r"most recent action",
            r"reuse.*audit",
        ]
        assert any(re.search(pat, content, re.IGNORECASE | re.DOTALL) for pat in recent_audit_patterns), (
            "The document must instruct the agent to look for "
            "a recent audit before doing any more work."
        )

    def test_step_1_distinguishes_reuse_from_fresh_audit(self) -> None:
        """The document must distinguish between reusing an existing audit and running a fresh one."""
        content = _skill_content()
        audit_section = _find_audit_section(content)
        assert audit_section is not None, "Could not find audit guidance section"

        assert re.search(r"if.*recent audit.*reuse", content, re.IGNORECASE | re.DOTALL), (
            "The document must say that a recent audit should be reused to establish the work."
        )
        assert re.search(r"if.*no recent audit.*/skill:audit", content, re.IGNORECASE | re.DOTALL), (
            "The document must say that implement should run a full audit when no recent audit exists."
        )

    def test_step_3_fallback_uses_skill_audit_command(self) -> None:
        """The document must invoke `/skill:audit <id>` when no recent audit exists."""
        content = _skill_content()
        audit_section = _find_audit_section(content)
        assert audit_section is not None, "Could not find audit guidance section"

        assert "/skill:audit <id>" in audit_section or "/skill:audit <work-item-id>" in audit_section, (
            "The audit section must explicitly instruct the agent to call `/skill:audit <id>` "
            "when no recent audit is available."
        )
        assert re.search(r"establish.*work", audit_section, re.IGNORECASE), (
            "The audit fallback must say that implement runs the audit to establish what work remains."
        )

    def test_step_3_preserves_existing_workflow_after_audit_selection(self) -> None:
        """The audit bootstrap must feed into the existing implementation flow."""
        content = _skill_content()
        audit_section = _find_audit_section(content)
        assert audit_section is not None, "Could not find audit guidance section"

        # Check that the document as a whole contains both audit and implement guidance
        assert re.search(r"audit.*continue implementing", audit_section, re.IGNORECASE | re.DOTALL) or \
               re.search(r"audit.*implement", content, re.IGNORECASE | re.DOTALL), (
            "The document should preserve the existing implementation workflow after audit selection."
        )


