"""Tests that implement and implement-single skills enforce TDD (tests-first).

These tests verify that:
- skill/implement/SKILL.md requires creating at least one test file before
  implementation code (Step 4).
- skill/implement/SKILL.md includes guidance on harnesses/mocks/placeholders
  when external constraints prevent writing complete tests.
- skill/implement-single/SKILL.md also includes the expanded tests-first
  details with placeholder documentation requirements.

Related work item: SA-0MQC4A11A008BSRI
"""

from pathlib import Path
import re

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_IMPLEMENT_SINGLE_MD = _REPO_ROOT / "skill" / "implement-single" / "SKILL.md"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def implement_content() -> str:
    """Load implement SKILL.md content once per module."""
    assert _IMPLEMENT_MD.exists(), f"implement SKILL.md not found at {_IMPLEMENT_MD}"
    return _IMPLEMENT_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def implement_single_content() -> str:
    """Load implement-single SKILL.md content once per module."""
    assert _IMPLEMENT_SINGLE_MD.exists(), (
        f"implement-single SKILL.md not found at {_IMPLEMENT_SINGLE_MD}"
    )
    return _IMPLEMENT_SINGLE_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_step(content: str, step_heading: str) -> str | None:
    """Extract a numbered step section from the markdown document."""
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(step_heading)}.*?(?=^\s*\d+\.\s+|\Z)",
    )
    match = pattern.search(content)
    if not match:
        return None
    return match.group(0)


def _find_section(content: str, heading: str) -> str | None:
    """Extract a section by heading (## or ###)."""
    pattern = re.compile(rf"^#+\s*{re.escape(heading)}", re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return None
    start = match.start()
    level = len(match.group(0).split()[0])
    rest = content[match.end():]
    next_heading = re.search(rf'^#{{1,{level}}}\s+\S', rest, re.MULTILINE)
    if next_heading:
        end = match.end() + next_heading.start()
    else:
        end = len(content)
    return content[start:end]


# ===================================================================
# Tests for skill/implement/SKILL.md
# ===================================================================


class TestImplementSkillTDD:
    """Verify implement SKILL.md enforces tests-first in Step 4."""

    def test_implement_step_4_requires_tests_first(self, implement_content: str) -> None:
        """Step 4 must include explicit instruction to write tests first
        before implementation code."""
        step4 = _find_step(implement_content, "1. Implement")
        assert step4 is not None, "Could not find 'Implement' step"

        # Must contain "write tests first" or equivalent
        tests_first_patterns = [
            r"write test[s]?\s+first",
            r"creat.{1,10}test.{1,30}(before|prior to|first)",
            r"test.{1,5}driven development",
            r"tests.{1,15}first",
        ]
        assert any(
            re.search(pat, step4, re.IGNORECASE)
            for pat in tests_first_patterns
        ), (
            "Step 4 must include explicit instruction to write tests before "
            "implementation code. Expected language like 'Write tests first' "
            "or 'Create at least one test file before adding or editing "
            "implementation code'."
        )

    def test_implement_step_4_allows_initial_test_failure(self, implement_content: str) -> None:
        """Step 4 must allow tests to fail on first run, then implement
        code to make them pass."""
        step4 = _find_step(implement_content, "1. Implement")
        assert step4 is not None, "Could not find 'Implement' step"

        must_have = [
            r"(fail|failures?)\s+on\s+(first|initial)\s+run",
            r"tests?\s+(may\s+)?(fail|be\s+failing).*first",
            r"(make|get).*pass.*(before\s+)?commit",
        ]
        assert any(
            re.search(pat, step4, re.IGNORECASE)
            for pat in must_have
        ), (
            "Step 4 must state that tests created first are allowed to fail on "
            "first run, and that the agent must then implement code to make them "
            "pass before committing."
        )

    def test_implement_step_4_harness_mock_guidance(self, implement_content: str) -> None:
        """Step 4 must include guidance for creating harnesses or mocks
        when external constraints prevent writing complete tests."""
        step4 = _find_step(implement_content, "1. Implement")
        assert step4 is not None, "Could not find 'Implement' step"

        harness_patterns = [
            r"harness(es)?",
            r"mock(s)?",
            r"placeholder",
            r"external\s+(constraint|service|infra|dependenc)",
            r"stub(s)?",
        ]
        assert any(
            re.search(pat, step4, re.IGNORECASE)
            for pat in harness_patterns
        ), (
            "Step 4 must include guidance for using harnesses, mocks, or "
            "placeholders when external constraints prevent writing complete tests."
        )

    def test_implement_step_4_placeholder_documentation(self, implement_content: str) -> None:
        """Step 4 must require explicit documentation when a harness/mock
        or placeholder is used, including a note in the work item comment
        and in the test file header."""
        step4 = _find_step(implement_content, "1. Implement")
        assert step4 is not None, "Could not find 'Implement' step"

        doc_patterns = [
            r"note\s+in\s+(the\s+)?work\s+item\s+comment",
            r"test\s+file\s+header",  # in the test file header
            r"temporary\s+(placeholder|workaround)",
            r"state\s+(the|a)\s+reason",
        ]
        assert any(
            re.search(pat, step4, re.IGNORECASE)
            for pat in doc_patterns
        ), (
            "Step 4 must require documenting the reason in the work item "
            "comment and test file header when a placeholder is used."
        )

    def test_implement_best_practices_tests_first(self, implement_content: str) -> None:
        """Best Practices section must include a tests-first guideline."""
        best_practices = _find_section(implement_content, "Best Practices")
        assert best_practices is not None, "Could not find 'Best Practices' section"

        tests_first_patterns = [
            r"write tests?\s+before",
            r"test.{1,10}driven development",
            r"tests?\s+first",
            r"at least one test",
        ]
        assert any(
            re.search(pat, best_practices, re.IGNORECASE)
            for pat in tests_first_patterns
        ), (
            "Best Practices section must include a guideline about writing "
            "tests first or test-driven development."
        )

    def test_implement_step_4_tests_recorded_in_artifacts(self, implement_content: str) -> None:
        """Step 4 must mention that tests must be recorded in run artifacts
        and visible in commit history."""
        step4 = _find_step(implement_content, "1. Implement")
        assert step4 is not None, "Could not find 'Implement' step"

        record_patterns = [
            r"record.{1,15}(run\s+)?(artifact|commit)",
            r"visible\s+in\s+(the\s+)?commit\s+history",
            r"commit\s+histor",
        ]
        assert any(
            re.search(pat, step4, re.IGNORECASE)
            for pat in record_patterns
        ), (
            "Step 4 must mention that tests must be recorded in run artifacts "
            "and visible in commit history."
        )


# ===================================================================
# Tests for skill/implement-single/SKILL.md
# ===================================================================


class TestImplementSingleTDD:
    """Verify implement-single SKILL.md has expanded tests-first details."""

    def test_implement_single_step_3_tests_first(self, implement_single_content: str) -> None:
        """Step 3 must have explicit 'write tests first' instruction."""
        step3 = _find_step(implement_single_content, "### Step 3")
        assert step3 is not None, "Could not find 'Step 3' in implement-single"

        tests_first_patterns = [
            r"write tests?\s+first",
            r"test.{1,5}driven development",
            r"tests?\s+first",
        ]
        assert any(
            re.search(pat, step3, re.IGNORECASE)
            for pat in tests_first_patterns
        ), (
            "Step 3 must include explicit 'write tests first' instruction."
        )

    def test_implement_single_step_3_harness_guidance(self, implement_single_content: str) -> None:
        """Step 3 must include guidance for using harnesses or mocks when
        external infra blocks writing real tests."""
        step3 = _find_step(implement_single_content, "### Step 3")
        assert step3 is not None, "Could not find 'Step 3' in implement-single"

        harness_patterns = [
            r"harness(es)?",
            r"mock(s)?",
            r"placeholder",
            r"external\s+(constraint|service|infra|dependenc)",
            r"stub(s)?",
            r"fixture(s)?",
        ]
        assert any(
            re.search(pat, step3, re.IGNORECASE)
            for pat in harness_patterns
        ), (
            "Step 3 must include guidance for using harnesses, mocks, or "
            "placeholders when external constraints prevent writing complete tests."
        )

    def test_implement_single_step_3_placeholder_documentation(self, implement_single_content: str) -> None:
        """Step 3 must require explicit note when harness/placeholder is used."""
        step3 = _find_step(implement_single_content, "### Step 3")
        assert step3 is not None, "Could not find 'Step 3' in implement-single"

        doc_patterns = [
            r"note\s+in\s+(the\s+)?work\s+item\s+comment",
            r"test\s+file\s+header",
            r"temporary\s+(placeholder|workaround)",
            r"state\s+(the\s+)?reason",
        ]
        assert any(
            re.search(pat, step3, re.IGNORECASE)
            for pat in doc_patterns
        ), (
            "Step 3 must require documenting the reason in the work item "
            "comment and test file header when a placeholder is used."
        )
