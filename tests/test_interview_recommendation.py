"""Doc-hygiene test: both interview skills must require an explicit preferred option.

SA-0MUBVLFP70064SPJ — Interview skills must state the agent's preferred option
explicitly.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_INTAKE_SKILL_MD = _REPO_ROOT / "skill" / "intake" / "SKILL.md"
_PLAN_SKILL_MD = _REPO_ROOT / "skill" / "plan" / "SKILL.md"

_MARKER = "**Recommended:**"
_REQUIRED_PHRASES = [
    "preferred option explicitly",
    "advisory",
    "operator's choice",
]


def _skill_content(path: Path) -> str:
    assert path.exists(), f"Skill doc not found at {path}"
    return path.read_text(encoding="utf-8")


# ── Intake skill ────────────────────────────────────────────────────────────


class TestIntakeRecommendation:
    """The intake skill must contain the recommendation requirement."""

    def test_intake_has_recommendation_marker(self) -> None:
        content = _skill_content(_INTAKE_SKILL_MD)
        assert _MARKER in content, (
            "skill/intake/SKILL.md must contain the "
            f"'{_MARKER}' recommendation marker"
        )

    def test_intake_requires_preferred_option_explicitly(self) -> None:
        content = _skill_content(_INTAKE_SKILL_MD)
        assert "preferred option explicitly" in content, (
            "skill/intake/SKILL.md must require the agent to state its "
            "preferred option explicitly"
        )

    def test_intake_mark_advisory(self) -> None:
        content = _skill_content(_INTAKE_SKILL_MD)
        assert "advisory" in content, (
            "skill/intake/SKILL.md must state the recommendation is advisory"
        )

    def test_intake_operator_choice_wins(self) -> None:
        content = _skill_content(_INTAKE_SKILL_MD)
        assert "operator's choice" in content, (
            "skill/intake/SKILL.md must state that the agent proceeds with "
            "the operator's choice"
        )


# ── Plan skill ──────────────────────────────────────────────────────────────


class TestPlanRecommendation:
    """The plan skill must contain the recommendation requirement."""

    def test_plan_has_recommendation_marker(self) -> None:
        content = _skill_content(_PLAN_SKILL_MD)
        assert _MARKER in content, (
            "skill/plan/SKILL.md must contain the "
            f"'{_MARKER}' recommendation marker"
        )

    def test_plan_requires_preferred_option_explicitly(self) -> None:
        content = _skill_content(_PLAN_SKILL_MD)
        assert "preferred option explicitly" in content, (
            "skill/plan/SKILL.md must require the agent to state its "
            "preferred option explicitly"
        )

    def test_plan_mark_advisory(self) -> None:
        content = _skill_content(_PLAN_SKILL_MD)
        assert "advisory" in content, (
            "skill/plan/SKILL.md must state the recommendation is advisory"
        )

    def test_plan_operator_choice_wins(self) -> None:
        content = _skill_content(_PLAN_SKILL_MD)
        assert "operator's choice" in content, (
            "skill/plan/SKILL.md must state that the agent proceeds with "
            "the operator's choice"
        )


# ── Both skills must be consistent ──────────────────────────────────────────


class TestBothSkillsConsistent:
    """Both interview skills must carry the same requirement language."""

    def _required_text(self, content: str) -> str:
        """Return the portion of content between the two known anchors."""
        start = content.index("preferred option explicitly")
        end = content.index("This recommendation is", start)
        return content[start:end]

    def test_both_skills_require_recommendation(self) -> None:
        intake = _skill_content(_INTAKE_SKILL_MD)
        plan = _skill_content(_PLAN_SKILL_MD)
        for name, content in [("intake", intake), ("plan", plan)]:
            assert "preferred option explicitly" in content, (
                f"{name} skill must require explicit recommendation"
            )

    def test_both_skills_mark_advisory(self) -> None:
        intake = _skill_content(_INTAKE_SKILL_MD)
        plan = _skill_content(_PLAN_SKILL_MD)
        for name, content in [("intake", intake), ("plan", plan)]:
            assert "advisory" in content, (
                f"{name} skill must mark recommendation as advisory"
            )

    def test_both_skills_allow_operator_override(self) -> None:
        intake = _skill_content(_INTAKE_SKILL_MD)
        plan = _skill_content(_PLAN_SKILL_MD)
        for name, content in [("intake", intake), ("plan", plan)]:
            assert "operator's choice" in content, (
                f"{name} skill must allow the operator's choice to prevail"
            )
