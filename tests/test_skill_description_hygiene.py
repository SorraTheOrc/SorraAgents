"""Description-hygiene tests for the compact skill-description template (F2).

Enforces SA-0MSLK78W7009HIXC acceptance criteria across all skill
frontmatter descriptions:

- AC1: every description ≤140 chars with a "Use when..." guidance clause.
- AC2: total description prose ≤1,800 B (from 3,492 B baseline, ≥48% cut),
  measured with the F1 tooling (skill/context-audit/scripts/measure_context.py).
- AC3: distinctive action verbs/nouns and "Use when" phrasing retained.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = (
    Path(__file__).resolve().parent.parent
    / "skill" / "context-audit" / "scripts"
)
sys.path.insert(0, str(_SCRIPT_DIR))

import measure_context as mc

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_DESC_BYTES = 140
# 1800 B covered the original 17 skills; the 18th (report helper,
# SA-0MSRFTP2Y008BH6L) adds at most one more ≤140 B description.
MAX_TOTAL_PROSE_BYTES = 1940


def _prose() -> dict[str, str]:
    # F2's template budget applies to every skill description, including
    # hidden (disable-model-invocation) skills — audit all of them.
    return mc.skill_description_prose(REPO_ROOT, include_hidden=True)


class TestDescriptionTemplate:
    """Every skill description follows the compact template."""

    def test_all_skills_have_descriptions(self):
        prose = _prose()
        # 18 skills at F2 (SA-0MSLK78W7009HIXC + report helper
        # SA-0MSRFTP2Y008BH6L); 16 after retiring owner-inference and
        # git-management (SA-0MSN81W9G006K0K8).
        assert len(prose) == 16, f"expected 16 skills, got {len(prose)}"

    def test_each_description_within_140_chars(self):
        for name, desc in _prose().items():
            assert len(desc) <= MAX_DESC_BYTES, (
                f"{name} description is {len(desc)} chars (> {MAX_DESC_BYTES}): "
                f"{desc!r}"
            )

    def test_each_description_has_use_when_guidance(self):
        for name, desc in _prose().items():
            assert "Use when" in desc, (
                f"{name} description lacks 'Use when' guidance: {desc!r}"
            )

    def test_each_description_has_action_verb(self):
        for name, desc in _prose().items():
            first_word = desc.split()[0] if desc.split() else ""
            assert first_word.isalpha(), (
                f"{name} description must start with an action word: {desc!r}"
            )

    def test_total_prose_within_budget(self):
        total = sum(len(d) for d in _prose().values())
        assert total <= MAX_TOTAL_PROSE_BYTES, (
            f"total description prose {total} B exceeds {MAX_TOTAL_PROSE_BYTES} B"
        )

    def test_measured_reduction_is_at_least_48_percent(self):
        baseline = 3492  # intake baseline (SA-0MSJI53RX006E2PS)
        prose = _prose()
        # The ≥48% cut was measured on the original 17 skills
        # (SA-0MSLK78W7009HIXC). The 18th skill (report) is new prose that
        # never existed at intake, so it cannot participate in the compaction
        # cut; its compactness is enforced by the ≤140 B per-description test
        # and the total-prose budget. Exclude it to keep the AC anchored to
        # the skills that were actually compacted.
        compacted_total = sum(len(d) for k, d in prose.items() if k != "report")
        reduction = 1 - compacted_total / baseline
        assert reduction >= 0.48, (
            f"reduction {reduction:.1%} < 48% (total {compacted_total} B vs {baseline} B)"
        )


class TestDistinctiveTriggersRetained:
    """AC3: distinctive verbs/nouns must survive the compaction."""

    def test_audit_trigger_words(self):
        desc = _prose()["audit"].lower()
        assert "status" in desc
        assert "audit" in desc

    def test_implement_trigger_words(self):
        desc = _prose()["implement"].lower()
        assert "implement" in desc or "write" in desc

    def test_test_trigger_words(self):
        desc = _prose()["test"].lower()
        assert "test" in desc

    def test_plan_trigger_words(self):
        desc = _prose()["plan"].lower()
        assert "plan" in desc

    def test_cleanup_trigger_words(self):
        desc = _prose()["cleanup"].lower()
        assert "clean" in desc or "prune" in desc

    def test_ship_trigger_words(self):
        desc = _prose()["ship"].lower()
        assert "release" in desc

    def test_refactor_trigger_words(self):
        desc = _prose()["refactor"].lower()
        assert "refactor" in desc or "smell" in desc
