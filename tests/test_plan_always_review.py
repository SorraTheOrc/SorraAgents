"""Tests that the Plan skill always runs the five automated review stages
on the auto-complete (skip) path, not just on the full planning path.

These tests verify that skill/plan/SKILL.md correctly implements the
following behaviors:
1. When plan-if-needed returns decision: "skip", the skill does NOT exit
   immediately — it runs the five automated review stages first.
2. The review stages are adapted to operate on existing work item content
   (description and any existing child items) rather than requiring a
   freshly generated feature plan.
3. The review stages may update the work item if gaps are found and fixed,
   but the improvement is conservative.
4. After the review stages complete, the skill outputs a summary to the
   console listing what each stage checked and what (if anything) was found
   or changed.
5. The work item is marked plan_complete only after all five review stages
   have run and any identified issues have been addressed.

Related work item: SA-0MQZWR37U0057KWF
"""

from pathlib import Path
import re

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD = _REPO_ROOT / "skill" / "plan" / "SKILL.md"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def skill_content() -> str:
    """Load skill/plan/SKILL.md content once per module."""
    assert _SKILL_MD.exists(), f"SKILL.md not found at {_SKILL_MD}"
    return _SKILL_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _find_section(content: str, heading: str) -> str | None:
    """Extract the text of a markdown section (including sub-sections) by heading.

    Returns None if the heading is not found.
    """
    # Escape special regex characters in heading
    escaped = re.escape(heading)
    pattern = re.compile(rf"^#+\s*{escaped}\s*$", re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return None
    start = match.start()
    level = len(match.group(0).split()[0])  # number of # characters
    rest = content[match.end():]
    # Find the next heading at the same or higher level
    next_heading = re.search(rf'^#{{1,{level}}}\s+\S', rest, re.MULTILINE)
    if next_heading:
        end = match.end() + next_heading.start()
    else:
        end = len(content)
    return content[start:end]


def _find_pre_check_section(content: str) -> str | None:
    """Extract the Pre-check section from the SKILL.md."""
    return _find_section(content, "Pre-check: Effort/Risk Threshold (must do before Process step 1)")


def _find_automated_review_section(content: str) -> str | None:
    """Extract the Automated review stages section from Process step 6."""
    # Look for "6. Automated review stages" within the Process section
    process_section = _find_section(content, "Process (must follow)")
    if not process_section:
        return None

    match = re.search(
        r"6\. Automated review stages.*?(?=\n\d+\.\s|\n## |\Z)",
        process_section,
        re.DOTALL,
    )
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestPreCheckSkipNoExit:
    """Verify that the pre-check skip path no longer exits immediately
    without running the review stages."""

    def test_pre_check_section_exists(self, skill_content: str) -> None:
        """The Pre-check section must exist in SKILL.md."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None, (
            "Could not find 'Pre-check: Effort/Risk Threshold' section in SKILL.md"
        )

    def test_skip_path_does_not_exit_immediately(self, skill_content: str) -> None:
        """The skip path must NOT exit immediately without running review stages.

        The old pattern 'Then **exit** the planning command without proceeding'
        should be replaced with instructions to run the review stages first.
        """
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        # Check that the old exit pattern is not present in the skip path
        # The "exit" should only appear as part of "without proceeding to step 1-5"
        # or similar, but NOT as an immediate exit before review
        old_exit_pattern = re.search(
            r"Then \*\*exit\*\* the planning command without proceeding",
            pre_check,
        )
        assert old_exit_pattern is None, (
            "The skip path must NOT contain 'Then **exit** the planning command "
            "without proceeding'. It should run the review stages before completing."
        )

    def test_skip_path_references_review_stages(self, skill_content: str) -> None:
        """The skip path must reference the five automated review stages."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        # Check that "skip" path mentions running review stages
        skip_ref_patterns = [
            r"review.{0,30}(stage|step|iteration)",
            r"automated review",
            r"review.{0,10}existing",
            r"run.{0,20}(five|review stage|auto.review)",
            r"(completeness|sequencing|scope sizing|acceptance|polish).{0,10}review",
        ]
        matched = any(
            re.search(pat, pre_check, re.IGNORECASE)
            for pat in skip_ref_patterns
        )
        assert matched, (
            "The skip path in the Pre-check section must reference the five "
            "automated review stages (or a review process) that runs before "
            "marking the work item as plan_complete. "
            "Expected mention of e.g., 'run the five automated review stages', "
            "'automated review on existing content', etc."
        )

    def test_skip_path_still_marks_plan_complete(self, skill_content: str) -> None:
        """After the review stages complete, the work item must still be
        marked plan_complete."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        # Check that plan_complete is still set
        assert re.search(
            r"plan_complete",
            pre_check,
        ), (
            "The skip path must still mark the work item as plan_complete "
            "after the review stages complete"
        )

    def test_skip_path_includes_comment_command(self, skill_content: str) -> None:
        """The skip path must include a wl comment add command to record
        the review summary (not just a simple skip message)."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        # Should have a wl comment add that includes review/summary content
        assert re.search(
            r"wl comment add.*(?:review|summary|completed)",
            pre_check,
            re.IGNORECASE,
        ), (
            "The skip path should include a `wl comment add` command that "
            "records a review summary (not just a simple 'skip' message)"
        )


class TestReviewStagesAdaptedForExistingContent:
    """Verify that the review stages can operate on existing work item
    content (not just freshly generated feature plans)."""

    def test_five_review_stages_are_present(self, skill_content: str) -> None:
        """The five review stages must be present somewhere in SKILL.md,
        either in the Process section step 6 or adapted for the skip path."""
        # Check all five stage names appear somewhere in the file
        stage_names = [
            r"Completeness.{0,20}review",
            r"Sequencing.{0,5}(?:&|and).{0,5}dependencies.{0,10}review",
            r"Scope.{0,10}sizing.{0,10}review",
            r"Acceptance.{0,5}(?:&|and).{0,5}testability.{0,10}review",
            r"Polish.{0,5}(?:&|and).{0,5}handoff.{0,10}review",
        ]
        for stage_pattern in stage_names:
            assert re.search(
                stage_pattern,
                skill_content,
                re.IGNORECASE,
            ), f"Expected to find stage matching '{stage_pattern}' in SKILL.md"

    def test_skip_path_mentions_review_on_existing_content(self, skill_content: str) -> None:
        """The skip path must mention that the review operates on existing
        work item content (description or child items), not on a feature plan."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        existing_content_patterns = [
            r"existing.{0,20}(content|description|child|work.?item)",
            r"(description|child).{0,20}(review|existing)",
            r"review.{0,20}whatever.{0,10}present",
            r"operate on",
        ]
        matched = any(
            re.search(pat, pre_check, re.IGNORECASE)
            for pat in existing_content_patterns
        )
        assert matched, (
            "The skip path should indicate that the review stages operate on "
            "whatever content exists (description, child items) rather than "
            "requiring a freshly generated feature plan"
        )

    def test_conservative_improvement_is_stated(self, skill_content: str) -> None:
        """The skill must state that improvements are conservative: only
        fix clearly needed and unambiguous gaps."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        assert re.search(
            r"(?:conservative|clearly.{0,10}(?:needed|wrong|missing)|unambiguous)",
            pre_check,
            re.IGNORECASE,
        ), (
            "The skip path should mention that improvements to the work item "
            "should be conservative — only fixing clearly needed and "
            "unambiguous gaps"
        )


class TestSummaryOutput:
    """Verify that after the review stages complete, a summary is output
    to the console."""

    def test_skip_path_includes_summary_output(self, skill_content: str) -> None:
        """The skip path must instruct the agent to output a summary of
        what each review stage checked and what was found or changed."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        summary_patterns = [
            r"(?:output|print|console|summar).{0,30}(?:summary|report|what.{0,5}(?:checked|found|changed))",
            r"listing what each review stage",
            r"summary of what (each|the review)",
        ]
        matched = any(
            re.search(pat, pre_check, re.IGNORECASE)
            for pat in summary_patterns
        )
        assert matched, (
            "The skip path must include an instruction to output a summary "
            "to the console listing what each review stage checked and what "
            "(if anything) was found or changed"
        )


class TestPlanCompleteAfterReview:
    """Verify that plan_complete is set only after all review stages have run."""

    def test_plan_complete_order(self, skill_content: str) -> None:
        """The wl update --stage plan_complete command must appear AFTER
        the review stages in the skip path, not before."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        # Find position of review mention and plan_complete
        review_match = re.search(r"(?:review|reviewing|completeness|sequencing|scope|acceptance|polish)", pre_check)
        complete_match = re.search(r"wl update.*--stage plan_complete", pre_check)

        if review_match and complete_match:
            review_pos = review_match.start()
            complete_pos = complete_match.start()
            assert review_pos < complete_pos, (
                "The review stage instructions must appear BEFORE the "
                "wl update --stage plan_complete command in the skip path, "
                "ensuring reviews run before marking complete"
            )

    def test_plan_complete_not_set_without_review(self, skill_content: str) -> None:
        """The plan_complete update must not appear before the review
        instructions in the skip path context."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        # If there is a 'wl update --stage plan_complete' before any
        # review mention, that's wrong (unless it's in an explanation context)
        plan_complete_cmds = [
            m.start() for m in re.finditer(r"wl update.*--stage plan_complete", pre_check)
        ]
        review_mentions = [
            m.start() for m in re.finditer(
                r"(?:review|reviewing|completeness|sequencing|scope|acceptance|polish)",
                pre_check,
            )
        ]

        # There should be at least one review mention before any plan_complete command
        review_before_any = any(
            rev < cmd
            for rev in review_mentions
            for cmd in plan_complete_cmds
        )
        assert review_before_any or not plan_complete_cmds, (
            "At least one review mention must appear before the first "
            "plan_complete command in the skip path"
        )


class TestNoSkipWithoutReview:
    """Verify that there is no path that marks plan_complete without
    running review stages when decision is 'skip'."""

    def test_no_plan_complete_before_review_in_skip(self, skill_content: str) -> None:
        """The skip path must not have a plan_complete update before the
        review instructions."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        # Find the "If decision == 'skip'" section within pre-check
        skip_section_match = re.search(
            r"If `decision == \"skip\"`.*?(?=If `decision ==|\Z)",
            pre_check,
            re.DOTALL,
        )
        assert skip_section_match is not None, (
            "Could not find the 'If decision == \"skip\"' subsection in pre-check"
        )
        skip_section = skip_section_match.group(0)

        # Verify there's a review mention before plan_complete
        review_before = re.search(
            r"(?:review|reviewing|completeness|sequencing|scope|acceptance|polish).*?"
            r"wl update.*?--stage plan_complete",
            skip_section,
            re.DOTALL | re.IGNORECASE,
        )
        assert review_before is not None, (
            "In the 'decision == skip' subsection, there must be a review "
            "instruction before the 'wl update --stage plan_complete' command"
        )

    def test_skip_path_outputs_summary_comment(self, skill_content: str) -> None:
        """The skip path must record a summary comment via wl comment add
        that includes the review results."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        skip_section_match = re.search(
            r"If `decision == \"skip\"`.*?(?=If `decision ==|\Z)",
            pre_check,
            re.DOTALL,
        )
        assert skip_section_match is not None
        skip_section = skip_section_match.group(0)

        assert re.search(
            r"wl comment add.*?(?:review|summary|completed)",
            skip_section,
            re.IGNORECASE,
        ), (
            "The skip path must include a wl comment add command that "
            "records the review summary"
        )


class TestNegativeRegression:
    """Negative tests: ensure the existing behavior can't be accidentally
    reverted without tests catching it."""

    def test_removing_review_from_skip_fails(self, skill_content: str) -> None:
        """If the review instructions are removed from the skip path,
        this test should fail."""
        pre_check = _find_pre_check_section(skill_content)
        assert pre_check is not None

        skip_section_match = re.search(
            r"If `decision == \"skip\"`.*?(?=If `decision ==|\Z)",
            pre_check,
            re.DOTALL,
        )
        assert skip_section_match is not None

        # Verify our test patterns reliably detect the review content
        skip_section = skip_section_match.group(0)
        has_review_ref = bool(re.search(
            r"(?:review|reviewing|completeness|sequencing|scope|acceptance|polish)",
            skip_section,
            re.IGNORECASE,
        ))
        assert has_review_ref, (
            "The test_review_stages_run_on_skip test depends on there being "
            "a review reference in the skip section. This assertion confirms "
            "the pattern would detect removal."
        )
