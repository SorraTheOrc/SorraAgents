"""Tests that plan.md implements auto-complete behavior when planning is clearly not needed.

These tests verify that the /plan command specification (command/plan.md)
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
_PLAN_MD = _REPO_ROOT / "command" / "plan.md"


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

    def test_borderline_evidence_errs_on_progress(self, plan_content: str) -> None:
        """When evidence is borderline or key uncertainties remain, the skill
        should err on the side of progress (auto-complete) rather than
        falling back to asking clarifying questions."""
        step1_content = _find_process_step(plan_content, "Evaluate whether planning is required")
        assert step1_content is not None

        # Check for "err on the side of progress" directive
        assert re.search(
            r"err on the side of progress",
            step1_content,
            re.IGNORECASE,
        ), (
            "Step 1 must include an 'err on the side of progress' directive "
            "that instructs the agent to auto-complete when in doubt"
        )

        # Check that the normal planning process is only used when
        # the heuristics genuinely cannot determine
        assert re.search(
            r"genuinely needs decomposition",
            step1_content,
            re.IGNORECASE,
        ) or re.search(
            r"cannot make a determination",
            step1_content,
            re.IGNORECASE,
        ), (
            "Step 1 should fall back to normal planning only when "
            "the heuristics genuinely cannot determine whether planning is needed"
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
        assert re.search(
            r"plan_complete.*(?:skip|already|not needed|no.?op).*comment",
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

    def test_other_stages_check_heuristics_before_asking(self, plan_content: str) -> None:
        """When stage is not intake_complete and not plan_complete or later,
        check the heuristics first before asking the operator."""
        step2_content = _find_process_step(plan_content, "Fetch & summarise")
        assert step2_content is not None

        # Check for a rule that other stages first run heuristics before asking
        assert re.search(
            r"(?:other|any other|else).*stage.*(?:heuristic|step 1|check|auto.comp)",
            step2_content,
            re.IGNORECASE | re.DOTALL,
        ), (
            "Step 2 must check heuristics first (referencing step 1) before "
            "asking the operator when the stage is not intake_complete or "
            "plan_complete or later"
        )

    def test_other_stages_ask_only_when_genuinely_unclear(self, plan_content: str) -> None:
        """When heuristics genuinely cannot determine, only then ask the
        operator how to proceed."""
        step2_content = _find_process_step(plan_content, "Fetch & summarise")
        assert step2_content is not None

        # Check that asking the user is a fallback when heuristics can't determine
        ask_context = re.search(
            r"(ask|prompt|inquire).*(?:user|operator|how to proceed)",
            step2_content,
            re.IGNORECASE,
        )
        heuristic_fallback = re.search(
            r"(genuinely|cannot|unclear|uncertain).*(?:determine|decide|resolve)",
            step2_content,
            re.IGNORECASE,
        )
        assert ask_context is not None, (
            "Step 2 should include an instruction to ask the operator when "
            "heuristics genuinely cannot determine"
        )
        assert heuristic_fallback is not None, (
            "Step 2 must include a guard condition (e.g., 'genuinely cannot "
            "determine') before asking the operator"
        )


class TestPlanNoopCommentFormat:
    """Verify that no-op/skip comments follow a consistent format."""

    def test_noop_comment_recorded_for_plan_complete(self, plan_content: str) -> None:
        """When stage is plan_complete or later, the no-op comment must be
        recorded via wl comment add."""
        step2_content = _find_process_step(plan_content, "Fetch & summarise")
        assert step2_content is not None

        assert re.search(
            r"wl comment add.*(?:plan not needed|already.*plan_complete|skip|no.?op)",
            step2_content,
            re.IGNORECASE,
        ), (
            "Step 2 must include a wl comment add command for the no-op "
            "when skipping planning due to plan_complete or later stage"
        )

    def test_auto_complete_comment_recorded(self, plan_content: str) -> None:
        """All auto-complete decisions must be recorded via wl comment add."""
        count = _count_comment_patterns(plan_content)
        assert count >= 2, (
            f"There should be at least 2 wl comment add commands for auto-complete "
            f"decisions in plan.md (one in step 1 for auto-complete, one in step 2 "
            f"for plan_complete skip). Found: {count}"
        )


class TestPlanCrossReferenceIntake:
    """Verify that the plan skill's auto-complete pattern is consistent
    with the reference implementation in intake.md."""

    def test_intake_auto_complete_pattern_exists(self) -> None:
        """Verify intake.md has the reference auto-complete pattern."""
        intake_md = _REPO_ROOT / "command" / "intake.md"
        assert intake_md.exists()
        content = intake_md.read_text(encoding="utf-8")

        assert re.search(
            r"Intake auto-complete",
            content,
        ), "intake.md should have 'Intake auto-complete' pattern as reference"

    def test_plan_uses_similar_pattern_to_intake(self, plan_content: str) -> None:
        """Plan auto-complete should use similar pattern to intake:
        update stage and add a comment."""
        assert re.search(
            r"Plan auto-complete",
            plan_content,
        ), "plan.md should have 'Plan auto-complete' comment pattern matching intake style"
