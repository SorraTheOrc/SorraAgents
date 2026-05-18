"""Tests that plan.md enforces test-first ordering for child work items.

These tests verify that the /plan command specification (command/plan.md)
includes mandatory rules ensuring test-related work items are always created
before implementation work items.

Related work item: SA-0MPAW77DY002QSP4
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


def _find_section(content: str, heading: str) -> str | None:
    """Extract the text of a markdown section (including sub-sections) by heading.

    Returns None if the heading is not found.
    """
    pattern = re.compile(rf"^#+\s*{re.escape(heading)}", re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return None
    start = match.start()
    # Find the next heading at the same or higher level
    level = len(match.group(0).split()[0])  # number of #
    # After the matched heading, find the next heading of same or higher level
    rest = content[match.end():]
    next_heading = re.search(rf'^#{{1,{level}}}\s+\S', rest, re.MULTILINE)
    if next_heading:
        end = match.end() + next_heading.start()
    else:
        end = len(content)
    return content[start:end]


def _count_heading_occurrences(content: str, heading: str) -> int:
    """Count how many times a heading appears in the content."""
    pattern = re.compile(rf"^#+\s*{re.escape(heading)}", re.MULTILINE)
    return len(pattern.findall(content))


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestPlanHardRequirementsForTestFirst:
    """Verify that the Hard requirements section of plan.md includes
    a rule mandating that test tasks must be created before implementation
    tasks."""

    def test_hard_requirements_section_exists(self, plan_content: str) -> None:
        """The 'Hard requirements' section must exist in plan.md."""
        assert re.search(r"^##\s*Hard requirements", plan_content, re.MULTILINE), (
            "plan.md must contain a '## Hard requirements' section"
        )

    def test_test_first_rule_in_hard_requirements(self, plan_content: str) -> None:
        """The Hard requirements section must contain a rule stating that
        test/verification work items must be created before implementation
        work items."""
        hard_req_section = _find_section(plan_content, "Hard requirements")
        assert hard_req_section is not None, (
            "Could not find 'Hard requirements' section in plan.md"
        )
        # Check for language about tests being first/created before implementation
        test_first_patterns = [
            r"test.{0,20}(first|before|ahead of|prior to).{0,30}implement",
            r"test.{0,20}work.{0,5}item.{0,20}(first|before|prior)",
            r"verification.{0,20}(first|before|prior).{0,30}implement",
            r"(first|before|prior).{0,20}test.{0,20}(implement|feature|task)",
        ]
        matched = any(
            re.search(pat, hard_req_section, re.IGNORECASE)
            for pat in test_first_patterns
        )
        assert matched, (
            "The 'Hard requirements' section must include a rule that "
            "test/verification work items are created before implementation "
            "work items. Expected language like 'tests must be created first' "
            "or 'test work items before implementation'."
        )


class TestPlanUpdateWorkItemsStepForTestFirst:
    """Verify that step 5 (Update work items) in the plan process explicitly
    instructs the agent to create test work items before implementation work
    items."""

    def test_update_work_items_section_exists(self, plan_content: str) -> None:
        """The 'Update work items' step must exist in plan.md."""
        # The section heading might be numbered (e.g., "5. Update work items")
        assert re.search(r"(?:\d+\.\s*)?Update work items", plan_content, re.IGNORECASE), (
            "plan.md must contain an 'Update work items' step"
        )

    def test_test_first_instruction_in_update_step(self, plan_content: str) -> None:
        """The 'Update work items' step must contain explicit instructions
        to create test/verification work items before implementation items."""
        # Find the section that contains "Update work items"
        # Look for content around "wl create" instructions in the update step
        update_section_match = re.search(
            r"Update work items.*?(?=\n#{1,3}\s|\Z)",
            plan_content,
            re.DOTALL | re.IGNORECASE,
        )
        assert update_section_match is not None, (
            "Could not find 'Update work items' section in plan.md"
        )
        update_section = update_section_match.group(0)

        test_first_patterns = [
            r"test.{0,30}(first|before|prior).{0,30}implement",
            r"creat.{0,20}test.{0,20}(first|before|prior)",
            r"verification.{0,20}(first|before|prior)",
        ]
        matched = any(
            re.search(pat, update_section, re.IGNORECASE)
            for pat in test_first_patterns
        )
        assert matched, (
            "The 'Update work items' step must include explicit instructions "
            "to create test/verification work items before implementation "
            "work items. For example: 'Create test work items before "
            "implementation work items' or 'Test tasks must be created first'."
        )


class TestPlanSequencingReviewForTestFirst:
    """Verify that the automated review stage for sequencing and dependencies
    includes a check that test tasks appear before implementation tasks."""

    def test_sequencing_review_exists(self, plan_content: str) -> None:
        """There must be a 'Sequencing & dependencies review' stage in plan.md."""
        assert re.search(
            r"Sequencing.{0,5}(?:&|and).{0,5}dependencies.{0,10}review",
            plan_content,
            re.IGNORECASE,
        ), (
            "plan.md must contain a 'Sequencing & dependencies review' stage"
        )

    def test_sequencing_review_checks_test_first(self, plan_content: str) -> None:
        """The sequencing review must include an action to ensure test tasks
        come before implementation tasks."""
        # Find the sequencing review section
        seq_match = re.search(
            r"Sequencing.{0,5}(?:&|and).{0,5}dependencies.{0,10}review.*?(?=\n\d+\.|\n#{2,3}\s|\Z)",
            plan_content,
            re.DOTALL | re.IGNORECASE,
        )
        assert seq_match is not None, (
            "Could not find 'Sequencing & dependencies review' section in plan.md"
        )
        seq_section = seq_match.group(0)

        test_first_patterns = [
            r"test.{0,20}(first|before|prior|preced)",
            r"verif.{0,10}(first|before|prior|preced)",
        ]
        matched = any(
            re.search(pat, seq_section, re.IGNORECASE)
            for pat in test_first_patterns
        )
        assert matched, (
            "The 'Sequencing & dependencies review' stage must include an action "
            "that verifies test tasks come before implementation tasks. "
            "For example: 'Ensure test tasks appear before implementation tasks'."
        )


class TestPlanProposeFeaturePlanForTestFirst:
    """Verify that the 'Propose feature plan' step requires test features to
    be listed first in the plan."""

    def test_propose_feature_plan_section_exists(self, plan_content: str) -> None:
        """The 'Propose feature plan' step must exist in plan.md."""
        assert re.search(
            r"Propose feature plan|feature plan.*propose",
            plan_content,
            re.IGNORECASE,
        ), (
            "plan.md must contain a 'Propose feature plan' step"
        )

    def test_propose_step_requires_test_first(self, plan_content: str) -> None:
        """The 'Propose feature plan' step must include a requirement that
        test features be listed before implementation features in the plan."""
        # Find the propose feature plan section
        propose_match = re.search(
            r"Propose feature plan.*?(?=\n#{2,3}\s|\n\d+\.\s+Aut|\n\d+\.\s+Update|\Z)",
            plan_content,
            re.DOTALL | re.IGNORECASE,
        )
        assert propose_match is not None, (
            "Could not find 'Propose feature plan' section in plan.md"
        )
        propose_section = propose_match.group(0)

        test_first_patterns = [
            r"test.{0,20}(first|before|prior|preced|order)",
            r"verif.{0,10}(first|before|prior|preced|order)",
            r"(first|before|prior|preced).{0,20}test",
        ]
        matched = any(
            re.search(pat, propose_section, re.IGNORECASE)
            for pat in test_first_patterns
        )
        assert matched, (
            "The 'Propose feature plan' step must include a requirement that "
            "test/verification features are listed before implementation features. "
            "For example: 'Test features must appear before implementation features'."
        )


class TestPlanNegativeRegression:
    """Negative tests: ensure the plan.md content cannot be incorrectly
    modified to remove test-first ordering without tests catching it."""

    def test_removal_of_test_first_in_hard_req_fails(self, plan_content: str) -> None:
        """If the test-first rule in Hard requirements is removed, the
        test_first_rule_in_hard_requirements test should fail."""
        # This test validates the detection pattern by confirming
        # that removing 'test' references from Hard requirements would
        # cause a detection failure. We verify that the pattern exists.
        hard_req_section = _find_section(plan_content, "Hard requirements")
        assert hard_req_section is not None
        # Verify our test pattern reliably detects the rule
        test_first_patterns = [
            r"test.{0,20}(first|before|ahead of|prior to).{0,30}implement",
            r"test.{0,20}work.{0,5}item.{0,20}(first|before|prior)",
        ]
        assert any(
            re.search(pat, hard_req_section, re.IGNORECASE)
            for pat in test_first_patterns
        ), (
            "Test-first ordering rule not found in Hard requirements section. "
            "The test_first_rule_in_hard_requirements test depends on this content."
        )

    def test_removal_of_test_first_in_update_step_fails(self, plan_content: str) -> None:
        """If the test-first instruction in Update work items is removed, the
        test_first_instruction_in_update_step test should fail."""
        update_match = re.search(
            r"Update work items.*?(?=\n#{1,3}\s|\Z)",
            plan_content,
            re.DOTALL | re.IGNORECASE,
        )
        assert update_match is not None
        update_section = update_match.group(0)

        test_first_patterns = [
            r"test.{0,30}(first|before|prior).{0,30}implement",
            r"creat.{0,20}test.{0,20}(first|before|prior)",
        ]
        assert any(
            re.search(pat, update_section, re.IGNORECASE)
            for pat in test_first_patterns
        ), (
            "Test-first instruction not found in 'Update work items' section. "
            "The test_first_instruction_in_update_step test depends on this content."
        )