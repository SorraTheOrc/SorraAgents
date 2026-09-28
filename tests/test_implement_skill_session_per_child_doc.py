"""Doc-contract tests: skill/implement/SKILL.md Step 6.1 documents session-per-child.

Verifies SA-0MTLCCIPU0050Q52 child SA-0MUCMO5YV008RD83: the parent-recursion
step must state that each epic child is implemented in its own Pi session
with a clean context window, while preserving worktree isolation, dependency
ordering, per-session logging, error isolation, and parent-last advancement —
aligned with the "Epic/parent items — one session per child" guidance in
AGENTS_GLOBAL.md.

The deliverable is documentation, so these tests assert the documented
contract (the guidance itself), in the established style of
``tests/test_cleanup_skill_doc.py`` and
``tests/test_agents_global_session_per_child.py``.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_STEP_HEADING = "6.1. Parent recursion"
_NEXT_LINE = "- Write tests and code to meet ACs:"


def _content() -> str:
    assert _SKILL_MD.exists(), f"File not found: {_SKILL_MD}"
    return _SKILL_MD.read_text(encoding="utf-8")


def _step() -> str:
    """Return the Step 6.1 body, whitespace-normalised so prose reflow does
    not break assertions. Bounded by the Step 6 test bullet."""
    content = _content()
    start = content.find(_STEP_HEADING)
    assert start != -1, f"{_SKILL_MD.name} must contain {_STEP_HEADING!r}"
    end = content.find(_NEXT_LINE, start)
    assert end != -1 and end > start, "Step 6.1 must precede the Step 6 test bullet"
    return " ".join(content[start:end].split())


class TestStep61SessionPerChild:
    """AC1: Step 6.1 states one new session per child, with a clean context."""

    def test_new_session_per_child_documented(self) -> None:
        step = _step()
        assert "/skill:implement" in step
        assert "new Pi session for each child" in step

    def test_clean_context_window_documented(self) -> None:
        step = _step()
        assert "clean context window" in step
        assert "description" in step and "acceptance criteria" in step


class TestStep61PreservedGuarantees:
    """AC2–AC3: worktree isolation and dependency order are preserved."""

    def test_worktree_isolation_preserved(self) -> None:
        step = _step()
        assert "Worktree isolation preserved" in step

    def test_dependency_order_confirmed(self) -> None:
        step = _step()
        assert "blocking items first" in step
        assert "dependency" in step


class TestStep61OperationalRules:
    """AC4–AC5: session logging and error isolation are documented."""

    def test_session_logging_documented(self) -> None:
        step = _step()
        assert "Session ID" in step

    def test_error_isolation_documented(self) -> None:
        step = _step()
        assert "does not affect" in step
        assert "succeeded and which failed" in step

    def test_parent_advanced_last(self) -> None:
        step = _step()
        assert "child sessions have reached a terminal stage" in step
        assert "in_review" in step


class TestStep61Alignment:
    """AC6: aligned with the AGENTS_GLOBAL.md session-per-child guidance."""

    def test_references_agents_global(self) -> None:
        step = _step()
        assert "AGENTS_GLOBAL.md" in step
        assert "one session per child" in step
