"""Doc-contract tests: AGENTS_GLOBAL.md documents one session per epic child.

Verifies the session-per-child procedural guidance required by
SA-0MTLCCIPU0050Q52 (child SA-0MUCMMYTV008QWI2). The deliverable is a
documentation change — an epic (a parent work item with children) must be
implemented with a new Pi session for each child, each with a clean context
window, serial dependency ordering, per-session logging, error isolation, and
parent advancement only once all child sessions have finished.

Because there is no runtime behaviour to exercise, these tests assert the
documented contract (the guidance itself), in the established style of
``tests/test_cleanup_skill_doc.py`` and ``tests/test_skill_invocation_map.py``.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_GLOBAL = _REPO_ROOT / "AGENTS_GLOBAL.md"
_SECTION_HEADING = "### Epic/parent items — one session per child"
_EARLIER_HEADING = "## Implement the work-item"


def _content() -> str:
    assert _AGENTS_GLOBAL.exists(), f"File not found: {_AGENTS_GLOBAL}"
    return _AGENTS_GLOBAL.read_text(encoding="utf-8")


def _section() -> str:
    """Return the body of the session-per-child section, exclusive of the
    heading and bounded by the next top-level (``## ``) heading. Line wrapping
    is collapsed so assertions are not broken by prose reflow."""
    content = _content()
    start = content.find(_SECTION_HEADING)
    assert start != -1, f"{_AGENTS_GLOBAL.name} must contain {_SECTION_HEADING!r}"
    body = content[start + len(_SECTION_HEADING):]
    next_heading = body.find("\n## ")
    if next_heading != -1:
        body = body[:next_heading]
    return " ".join(body.split())


class TestSessionPerChildSection:
    """AC1: a new procedural step documents one session per child."""

    def test_section_is_under_implement_the_work_item(self) -> None:
        content = _content()
        assert content.find(_EARLIER_HEADING) < content.find(_SECTION_HEADING)

    def test_parent_invocation_starts_new_session_per_child(self) -> None:
        section = _section()
        assert "/skill:implement <parent-id>" in section
        assert "own Pi session" in section


class TestSessionIsolation:
    """AC2: each child's session starts with a clean, focused context."""

    def test_clean_context_window_documented(self) -> None:
        section = _section()
        assert "clean context window" in section
        assert "description" in section and "acceptance criteria" in section


class TestSerialDependencyOrder:
    """AC3: serial execution in dependency order is preserved."""

    def test_serial_and_blocking_first_documented(self) -> None:
        section = _section()
        assert "serially" in section
        assert "dependency order" in section
        assert "blocking items first" in section


class TestSessionLogging:
    """AC4: each new session logs its session id on the child work item."""

    def test_session_logging_convention_referenced(self) -> None:
        section = _section()
        assert "Session logging" in section
        assert "Session ID" in section


class TestParentAdvancement:
    """AC5: the parent advances only after all child sessions finish."""

    def test_parent_advances_last(self) -> None:
        section = _section()
        assert "**all** child sessions" in section
        assert "in_review" in section


class TestErrorIsolation:
    """AC6: a failure in one child's session does not affect the others."""

    def test_error_isolation_documented(self) -> None:
        section = _section()
        assert "failure" in section
        assert "does not affect" in section
        assert "succeeded and which failed" in section
