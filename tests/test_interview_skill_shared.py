"""Doc-hygiene tests for the shared interview conduct skill.

Verifies SA-0MUBVM6FR000NDI1 acceptance criteria:

- AC1: a canonical shared interview component exists
  (``skill/interview/SKILL.md``) and is hidden from model invocation.
- AC2: ``skill/intake/SKILL.md`` and ``skill/plan/SKILL.md`` reference the
  shared component instead of restating the interview rules.
- AC3/AC4: the shared component defines each required interview rule:
  ≤3 questions per round, multiple-choice preferred/freeform allowed, the
  explicit ``**Recommended:**`` preferred-option rule, the producer-review
  handoff via ``wl reviewed``, and idempotent Appendix recording.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skill"
INTERVIEW_SKILL = SKILL_DIR / "interview" / "SKILL.md"

# Consuming skills that must delegate interview conduct to the shared skill.
CONSUMING_SKILLS = ["intake", "plan"]

# How a consuming skill points at the shared contract (skill-relative path).
SHARED_REFERENCE = "interview/SKILL.md"


def _read(path: Path) -> str:
    assert path.exists(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


class TestSharedInterviewSkillExists:
    """AC1: the canonical interview component exists and is a hidden helper."""

    def test_shared_interview_skill_exists(self) -> None:
        assert INTERVIEW_SKILL.is_file(), (
            f"canonical interview skill missing at {INTERVIEW_SKILL}"
        )

    def test_shared_interview_skill_is_hidden_from_model_invocation(self) -> None:
        text = _read(INTERVIEW_SKILL)
        assert "disable-model-invocation: true" in text, (
            "shared interview skill must set disable-model-invocation: true"
        )


class TestSharedInterviewSkillRules:
    """AC3/AC4: every required interview rule lives in the shared component."""

    def test_question_volume_rule_present(self) -> None:
        assert "≤ 3 high-signal questions per round" in _read(INTERVIEW_SKILL)

    def test_multiple_choice_preference_rule_present(self) -> None:
        assert "Multiple-choice preferred; freeform allowed" in _read(INTERVIEW_SKILL)

    def test_explicit_recommended_option_rule_present(self) -> None:
        text = _read(INTERVIEW_SKILL)
        assert "**Recommended:** <option>" in text, (
            "shared skill must define the explicit preferred-option marker"
        )
        assert "advisory" in text.lower(), (
            "shared skill must state the recommendation is advisory"
        )

    def test_producer_review_handoff_rule_present(self) -> None:
        assert "wl reviewed <work-item-id> true" in _read(INTERVIEW_SKILL)

    def test_appendix_recording_rule_present(self) -> None:
        text = _read(INTERVIEW_SKILL)
        assert "Appendix" in text, "shared skill must require Appendix recording"
        assert "never duplicate" in text, (
            "shared skill must require idempotent (non-duplicating) recording"
        )


class TestConsumingSkillsReferenceSharedComponent:
    """AC2: intake and plan delegate to the shared component."""

    def test_both_consuming_skills_reference_shared_skill(self) -> None:
        for name in CONSUMING_SKILLS:
            text = _read(SKILL_DIR / name / "SKILL.md")
            assert SHARED_REFERENCE in text, (
                f"{name} must reference the shared interview skill "
                f"({SHARED_REFERENCE})"
            )

    def test_consuming_skills_do_not_restate_recommendation_rule(self) -> None:
        """The explicit *Recommended:* rule must live only in the shared skill."""
        for name in CONSUMING_SKILLS:
            text = _read(SKILL_DIR / name / "SKILL.md")
            assert "**Recommended:** <option>" not in text, (
                f"{name} must not duplicate the shared preferred-option rule"
            )
