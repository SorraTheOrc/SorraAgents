"""F3 tests: hide internal/helper skills from model invocation.

Verifies SA-0MSLK7ENO0053WXG acceptance criteria:

- AC1: the six internal/helper skills carry ``disable-model-invocation: true``
  in frontmatter.
- AC2: AGENTS_GLOBAL.md contains the skill-invocation map listing each hidden
  skill and its /skill:name or script entry point.
- AC3: the measured skills section (description prose of visible skills +
  hidden-skill slots) drops ≥25% vs the F2 baseline.
- AC4: hidden skills remain invocable — their entry points are documented in
  the invocation map.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SCRIPT_DIR = (
    Path(__file__).resolve().parent.parent
    / "skill" / "context-audit" / "scripts"
)
sys.path.insert(0, str(_SCRIPT_DIR))

import measure_context as mc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HIDDEN_SKILLS = [
    "owner-inference",
    "triage",
    "find-related",
    "effort-and-risk",
    "speak",
    "git-management",
]
AGENTS_GLOBAL = REPO_ROOT / "AGENTS_GLOBAL.md"


def _frontmatter(skill_name: str) -> str:
    p = REPO_ROOT / "skill" / skill_name / "SKILL.md"
    assert p.exists(), f"missing skill dir: {p}"
    return mc.extract_frontmatter(p.read_text(encoding="utf-8"))


class TestHiddenFrontmatter:
    """AC1: the six skills carry disable-model-invocation: true."""

    def test_all_six_skills_have_disable_flag(self):
        for name in HIDDEN_SKILLS:
            assert "disable-model-invocation: true" in _frontmatter(name), (
                f"{name} must set disable-model-invocation: true"
            )

    def test_other_skills_are_not_hidden(self):
        visible = [
            "audit", "author-command", "cleanup", "code-review",
            "implement", "intake", "plan", "refactor",
            "resolve-pr-comments", "ship", "test",
        ]
        for name in visible:
            assert "disable-model-invocation: true" not in _frontmatter(name), (
                f"{name} must stay model-invocable"
            )


class TestInvocationMap:
    """AC2/AC4: AGENTS_GLOBAL.md documents each hidden skill's entry point."""

    def test_map_section_present(self):
        text = AGENTS_GLOBAL.read_text(encoding="utf-8")
        assert "Skill invocation map" in text

    def test_each_hidden_skill_listed_with_entry_point(self):
        text = AGENTS_GLOBAL.read_text(encoding="utf-8")
        for name in HIDDEN_SKILLS:
            assert f"`{name}`" in text, f"invocation map must list `{name}`"
        # Every entry point must be a /skill: path or a script path.
        for entry in re.findall(r"`(/skill:[a-z-]+|\.?/?[a-zA-Z0-9_./-]+\.py)`", text):
            assert entry.startswith("/skill:") or entry.endswith(".py"), (
                f"map entry point not invocable: {entry!r}"
            )


class TestSkillsSectionReduction:
    """AC3: measured visible skills section drops ≥25% vs F2 baseline."""

    def test_visible_skills_prose_drops_at_least_25_percent(self):
        comps = mc.measure(REPO_ROOT)
        visible_prose = comps["skills_prose"]["bytes"]
        # F2 baseline: all 17 skills' prose after description compaction.
        baseline = sum(
            len(d) for d in mc.skill_description_prose(REPO_ROOT, include_hidden=True).values()
        )
        assert visible_prose <= baseline * 0.75, (
            f"visible prose {visible_prose} B not ≥25% below all-skills "
            f"baseline {baseline} B"
        )
