"""Doc-hygiene test: the interview skills must require an explicit preferred option.

SA-0MUBVLFP70064SPJ — Interview skills must state the agent's preferred option
explicitly.

SA-0MUBVM6FR000NDI1 relocated the shared interview-conduct rules (including the
explicit preferred-option requirement) into the canonical
``skill/interview/SKILL.md``. The requirement is still enforced — now through
the shared component — while ``intake`` and ``plan`` delegate to it. These
assertions therefore target the shared skill and verify the consuming skills
reference it (instead of restating the rule).
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_INTERVIEW_SKILL_MD = _REPO_ROOT / "skill" / "interview" / "SKILL.md"
_INTAKE_SKILL_MD = _REPO_ROOT / "skill" / "intake" / "SKILL.md"
_PLAN_SKILL_MD = _REPO_ROOT / "skill" / "plan" / "SKILL.md"
_INTERVIEW_REF_MD = _REPO_ROOT / "docs" / "dev" / "interview-skill-reference.md"
_INTAKE_REF_MD = _REPO_ROOT / "docs" / "dev" / "intake-skill-reference.md"
_PLAN_REF_MD = _REPO_ROOT / "docs" / "dev" / "plan-skill-reference.md"

_MARKER = "**Recommended:**"
_REQUIRED_PHRASES = [
    "preferred option explicitly",
    "advisory",
    "operator may choose",
]
_SHARED_REFERENCE = "interview/SKILL.md"


def _skill_content(path: Path) -> str:
    assert path.exists(), f"Skill doc not found at {path}"
    return path.read_text(encoding="utf-8")


# ── Shared interview skill carries the requirement ──────────────────────────


class TestSharedInterviewRecommendation:
    """The shared interview skill must contain the recommendation requirement."""

    def test_recommendation_marker_present(self) -> None:
        content = _skill_content(_INTERVIEW_SKILL_MD)
        assert _MARKER in content, (
            f"skill/interview/SKILL.md must contain the '{_MARKER}' marker"
        )

    def test_requires_preferred_option_explicitly(self) -> None:
        content = _skill_content(_INTERVIEW_SKILL_MD)
        assert "preferred option" in content and "explicitly" in content, (
            "shared skill must require the agent to state its preferred option "
            "explicitly"
        )

    def test_mark_advisory(self) -> None:
        content = _skill_content(_INTERVIEW_SKILL_MD)
        assert "advisory" in content, (
            "shared skill must state the recommendation is advisory"
        )

    def test_operator_choice_wins(self) -> None:
        content = _skill_content(_INTERVIEW_SKILL_MD)
        assert "operator may choose" in content, (
            "shared skill must state the operator may choose any option"
        )

    def test_applies_to_every_round_and_producer_handoff(self) -> None:
        content = _skill_content(_INTERVIEW_SKILL_MD)
        assert "every interview round" in content, (
            "shared skill must state the requirement applies to every round"
        )
        assert "producer" in content, (
            "shared skill must cover producer-review handoff questions"
        )


# ── Consuming skills delegate to the shared component ───────────────────────


class TestConsumingSkillsDelegate:
    """intake and plan must reference the shared component, not restate it."""

    def test_intake_references_shared_skill(self) -> None:
        content = _skill_content(_INTAKE_SKILL_MD)
        assert _SHARED_REFERENCE in content, (
            "skill/intake/SKILL.md must reference the shared interview skill"
        )

    def test_plan_references_shared_skill(self) -> None:
        content = _skill_content(_PLAN_SKILL_MD)
        assert _SHARED_REFERENCE in content, (
            "skill/plan/SKILL.md must reference the shared interview skill"
        )

    def test_consuming_skills_do_not_restate_the_rule(self) -> None:
        for name, path in [("intake", _INTAKE_SKILL_MD), ("plan", _PLAN_SKILL_MD)]:
            content = _skill_content(path)
            assert _MARKER not in content, (
                f"{name} skill must not duplicate the shared "
                "preferred-option rule"
            )


# ── Maintainer reference docs ───────────────────────────────────────────────


class TestReferenceDocs:
    """The maintainer reference docs must point to the interview-conduct rule."""

    def test_shared_interview_reference_documents_recommendation(self) -> None:
        content = _skill_content(_INTERVIEW_REF_MD)
        assert "Interview" in content, (
            "docs/dev/interview-skill-reference.md must document the "
            "interview-conduct rule"
        )
        assert _MARKER in content, (
            "interview reference doc must reference the `**Recommended:**` marker"
        )

    def test_intake_reference_points_to_shared_skill(self) -> None:
        content = _skill_content(_INTAKE_REF_MD)
        assert "Interview conduct" in content, (
            "docs/dev/intake-skill-reference.md must document the "
            "interview-conduct rule"
        )
        assert "interview" in content.lower(), (
            "intake reference doc must point to the shared interview skill"
        )

    def test_plan_reference_points_to_shared_skill(self) -> None:
        content = _skill_content(_PLAN_REF_MD)
        assert "Interview conduct" in content, (
            "docs/dev/plan-skill-reference.md must document the "
            "interview-conduct rule"
        )
        assert "interview" in content.lower(), (
            "plan reference doc must point to the shared interview skill"
        )
