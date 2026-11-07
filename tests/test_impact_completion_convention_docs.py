"""Doc-contract tests: the ``## Impact`` work-item completion convention.

SA-0MW6EUWCF005W16Z (child of the standup epic SA-0MW6ER1NX0094QCG) asks that
newly completed work items carry an ``## Impact`` section — one sentence
describing the user-experience impact — so the standup report can quote a real
impact sentence instead of re-deriving one from the technical description.

The deliverable is documentation, so these tests assert the documented contract
in the established style of ``tests/test_agents_global_session_per_child.py``:
the convention must appear in the global guidance (``AGENTS_GLOBAL.md``, the
source of truth installed as ``~/.pi/agent/AGENTS.md``), be mirrored by the
project ``AGENTS.md``, be recommended in the intake brief guidance, and be
required by the implement skill's completion step. Existing completed items are
deliberately not retrofitted, and the wording must stay consistent — every file
uses the same canonical phrase.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_GLOBAL = _REPO_ROOT / "AGENTS_GLOBAL.md"
_PROJECT_AGENTS = _REPO_ROOT / "AGENTS.md"
_INTAKE_SKILL = _REPO_ROOT / "skill" / "intake" / "SKILL.md"
_IMPLEMENT_SKILL = _REPO_ROOT / "skill" / "implement" / "SKILL.md"

_SECTION_HEADING = "## Work-item completion conventions"

#: The single canonical phrase every touched file must share (AC5).
_CANONICAL_PHRASE = "one sentence describing the user-experience impact"


def _content(path: Path) -> str:
    assert path.exists(), f"File not found: {path}"
    return path.read_text(encoding="utf-8")


def _section(path: Path) -> str:
    """Body of the completion-conventions section, whitespace-normalised.

    The section ends at the next top-level (``## ``) heading; collapsing line
    wrapping keeps assertions independent of prose reflow.
    """
    content = _content(path)
    start = content.find(_SECTION_HEADING)
    assert start != -1, f"{path.name} must contain {_SECTION_HEADING!r}"
    body = content[start + len(_SECTION_HEADING):]
    next_heading = body.find("\n## ")
    if next_heading != -1:
        body = body[:next_heading]
    return " ".join(body.split())


class TestGlobalGuidance:
    """AC1: AGENTS_GLOBAL.md documents the ``## Impact`` completion requirement."""

    def test_section_exists(self) -> None:
        _section(_AGENTS_GLOBAL)

    def test_impact_heading_and_sentence_required(self) -> None:
        section = _section(_AGENTS_GLOBAL)
        assert "## Impact" in section
        assert _CANONICAL_PHRASE in section

    def test_completion_moment_named(self) -> None:
        """The convention must say *when* it applies: advancing to in_review."""
        section = _section(_AGENTS_GLOBAL)
        assert "in_review" in section

    def test_user_experience_not_technical_summary(self) -> None:
        section = _section(_AGENTS_GLOBAL)
        assert "user" in section.lower()
        assert "technical" in section.lower()


class TestNoRetrofit:
    """AC4: existing completed items are explicitly not retrofitted."""

    def test_global_guidance_states_no_retrofit(self) -> None:
        section = _section(_AGENTS_GLOBAL)
        assert "not retrofitted" in section.lower()

    def test_project_guidance_states_no_retrofit(self) -> None:
        section = _section(_PROJECT_AGENTS)
        assert "not retrofitted" in section.lower()


class TestProjectGuidance:
    """AC2: the project AGENTS.md mirrors the same convention."""

    def test_section_exists(self) -> None:
        _section(_PROJECT_AGENTS)

    def test_mirrors_impact_requirement(self) -> None:
        section = _section(_PROJECT_AGENTS)
        assert "## Impact" in section
        assert _CANONICAL_PHRASE in section
        assert "in_review" in section

    def test_points_at_global_guidance(self) -> None:
        section = _section(_PROJECT_AGENTS)
        assert "AGENTS_GLOBAL.md" in section


class TestIntakeGuidance:
    """AC3: the intake brief guidance recommends ``## Impact``."""

    def test_intake_brief_lists_impact(self) -> None:
        content = _content(_INTAKE_SKILL)
        assert "## Impact" in content
        assert _CANONICAL_PHRASE in content

    def test_marked_optional_but_recommended(self) -> None:
        content = _content(_INTAKE_SKILL)
        normalised = " ".join(content.split()).lower()
        assert "optional but recommended" in normalised


class TestImplementGuidance:
    """AC6 (parent): the implement skill's completion step carries the convention."""

    def test_completion_step_requires_impact_sentence(self) -> None:
        content = _content(_IMPLEMENT_SKILL)
        start = content.find("9. Commit, Push to dev and mark in_review")
        assert start != -1, "implement skill must keep its Step 9 heading"
        step = content[start:]
        assert "## Impact" in step
        assert _CANONICAL_PHRASE in step


class TestWordingConsistency:
    """AC5: the canonical phrase is identical across every touched file."""

    def test_canonical_phrase_in_all_touched_files(self) -> None:
        for path in (
            _AGENTS_GLOBAL,
            _PROJECT_AGENTS,
            _INTAKE_SKILL,
            _IMPLEMENT_SKILL,
        ):
            assert _CANONICAL_PHRASE in _content(path), (
                f"{path.name} must use the canonical phrase {_CANONICAL_PHRASE!r}"
            )
