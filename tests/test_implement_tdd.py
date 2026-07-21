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
    """Extract a step section from the markdown document.

    Supports both old numbered format (e.g., "1. Implement") and new
    heading format (e.g., "### Step 3 — Implement").
    """
    # Try exact match first
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(step_heading)}.*?(?=^\s*\d+\.\s+|^\s*###\s+Step|\Z)",
    )
    match = pattern.search(content)
    if match:
        return match.group(0)

    # For new format, also try matching "### Step N — <heading>"
    # e.g., if step_heading is "1. Implement", try "### Step 3 — Implement"
    # Extract the topic after removing numbering
    import re as re_mod
    topic = re_mod.sub(r"^[\d\.\s#-]+|^###\s+Step\s+\d+\s+[—\-]\s+", "", step_heading).strip()
    if topic:
        pattern2 = re.compile(
            rf"(?ms)^\s*###\s+Step\s+\d+\s+[—\-]\s+{re.escape(topic)}.*?(?=^\s*###\s+Step|\Z)",
        )
        match2 = pattern2.search(content)
        if match2:
            return match2.group(0)

    return None


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


