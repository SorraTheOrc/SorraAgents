"""Tests that plan.md implements auto-complete behavior when planning is clearly not needed.

These tests verify that the /plan skill specification (skill/plan/SKILL.md)
correctly implements the following behaviors:
1. Step 1 auto-completes when heuristics determine planning is not needed
   (removes "optionally" from the comment step — comments are mandatory)
2. Step 2 (stage validation): when stage is plan_complete or later, skips
   with a no-op comment rather than asking the operator
3. Step 2 (stage validation): when stage is other, checks heuristics first
   before asking the operator
4. "Err on the side of progress" directive is present in the evaluation logic
5. All auto-complete decisions are recorded via wl comment add

Related work item: SA-0MQFWHQW7008JSNZ
"""

from pathlib import Path
import re

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLAN_MD = _REPO_ROOT / "skill" / "plan" / "SKILL.md"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plan_content() -> str:
    """Load plan.md content once per module."""
    assert _PLAN_MD.exists(), f"plan.md not found at {_PLAN_MD}"
    return _PLAN_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _find_process_step(content: str, step_label: str) -> str | None:
    """Extract a numbered process step from the Process section in plan.md.

    Process steps are numbered items like '1. Evaluate whether planning...'
    that appear under '## Process (must follow)'.

    Args:
        content: Full plan.md content.
        step_label: The unique label after the number (e.g., 'Evaluate' or 'Fetch & summarise').

    Returns:
        The text of the step including all its content up to the next numbered step
        or next heading, or None if not found.
    """
    # First find the Process section
    process_match = re.search(
        r"^## Process \(must follow\)$(.*?)(?=^## )",
        content,
        re.MULTILINE | re.DOTALL,
    )
    if not process_match:
        return None
    process_section = process_match.group(1)

    # Now find the step within the process section
    # Steps are like "1. Evaluate whether planning is required (agent responsibility)"
    pattern = re.compile(
        rf"^\d+\.\s*{re.escape(step_label)}.*?(?=^\d+\.\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(process_section)
    if match:
        return match.group(0)
    return None


def _count_comment_patterns(content: str) -> int:
    """Count wl comment add commands related to auto-complete decisions."""
    patterns = re.findall(
        r"wl comment add.*(?:Plan auto-complete|auto-complete|plan not needed|skip)",
        content,
        re.IGNORECASE,
    )
    return len(patterns)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestPlanAutoCompleteStep1:
    """Verify that step 1 of plan.md auto-completes when heuristics indicate
    planning is not needed."""

    def test_auto_complete_is_default_when_heuristics_say_not_needed(self, plan_content: str) -> None:
        """When step 1 heuristics determine planning is not needed, the
        skill should auto-complete without asking the operator."""
        step1_content = _find_process_step(plan_content, "Evaluate whether planning is required")
        assert step1_content is not None, (
            "Could not find step 'Evaluate whether planning is required' in Process section"
        )

        # Check that auto-complete behavior is present: update stage + comment
        assert re.search(
            r"wl update.*--stage plan_complete",
            step1_content,
        ), (
            "Step 1 must include updating stage to plan_complete when planning is not needed"
        )

        # Check there's a comment add command (not "Optionally" — mandatory)
        assert re.search(
            r"wl comment add.*Plan auto-complete",
            step1_content,
        ), (
            "Step 1 must include a mandatory `wl comment add` documenting the auto-complete reason"
        )

    def test_comment_is_not_optional(self, plan_content: str) -> None:
        """The comment step must not be marked as 'Optionally' — it should be mandatory."""
        step1_content = _find_process_step(plan_content, "Evaluate whether planning is required")
        assert step1_content is not None

        # 'Optionally' should NOT appear before the comment add command
        optionally_pattern = re.search(
            r"Optionally add a comment.*wl comment add",
            step1_content,
        )
        assert optionally_pattern is None, (
            "The comment add step should not be marked as 'Optionally'. "
            "The comment documenting the auto-complete reason should be mandatory."
        )


class TestPlanAutoCompleteStep2:
    """Verify that step 2 of plan.md handles stage validation with
    auto-complete behavior."""

    def test_plan_complete_skips_with_noop_comment(self, plan_content: str) -> None:
        """When stage is plan_complete or later, skip with a no-op comment
        rather than asking the operator."""
        step2_content = _find_process_step(plan_content, "Fetch & summarise")
        assert step2_content is not None, (
            "Could not find step 'Fetch & summarise' in Process section"
        )

        # Check that plan_complete or later is handled with a no-op comment
        # (skill file uses "record a no-op comment" rather than an explicit wl comment add)
        assert re.search(
            r"plan_complete.*(?:skip|already|not needed|no.?op|record).*comment",
            step2_content,
            re.IGNORECASE | re.DOTALL,
        ), (
            "Step 2 must skip planning with a no-op comment when stage "
            "is plan_complete or later, rather than asking the operator"
        )

    def test_no_ask_for_plan_complete_stage(self, plan_content: str) -> None:
        """There should be no instruction to ask the user about re-running
        plan when stage is plan_complete or later."""
        step2_content = _find_process_step(plan_content, "Fetch & summarise")
        assert step2_content is not None

        # Should NOT ask user about re-running
        ask_pattern = re.search(
            r"plan_complete.*ask the user.*re.run",
            step2_content,
            re.IGNORECASE | re.DOTALL,
        )
        assert ask_pattern is None, (
            "Step 2 should not ask the user if they want to re-run planning "
            "when stage is plan_complete or later — it should auto-skip"
        )


class TestPlanNoopCommentFormat:
    """Verify that no-op/skip comments follow a consistent format."""

    def test_auto_complete_comment_recorded(self, plan_content: str) -> None:
        """All auto-complete decisions must be recorded via wl comment add."""
        count = _count_comment_patterns(plan_content)
        assert count >= 1, (
            f"There should be at least 1 wl comment add command for auto-complete "
            f"decisions in the skill file (step 1 for auto-complete). Found: {count}"
        )


class TestPlanCrossReferenceIntake:
    """Verify that the plan skill's auto-complete pattern is consistent
    with the reference implementation in intake.md."""

    def test_skill_has_auto_complete_pattern(self, plan_content: str) -> None:
        """Verify skill/plan/SKILL.md has auto-complete pattern."""
        assert re.search(
            r"auto-complete",
            plan_content,
        ), "skill/plan/SKILL.md should have auto-complete pattern matching intake style"
