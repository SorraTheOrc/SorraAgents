"""Report-wiring hygiene tests (SA-0MSJ082OY003IQ8S follow-up).

Regression guard for the operator finding that the report was produced as
tool output only and never put in front of the user ("This is not
consistently applied across any of the skills" — reports must be visible in
the agent's final response, not just as script stdout).

Enforces, for every work-item skill:

- AC: the SKILL.md ends with the `## Final step: standardized end-of-session
  report` section.
- AC: that section invokes `python3 $(skill_path report)/scripts/render_report.py`.
- AC: the section explicitly instructs the agent to **paste the rendered
  report verbatim into its final response** — the visibility guarantee that
  makes the report reach the operator (not just a tool call).

Non-work-item skills (speak) must remain unwired.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skill"

# Skills whose flow creates/updates work items and must end with the
# standardized report. git-management and owner-inference were retired
# (SA-0MSN81W9G006K0K8); report/speak are excluded (helper / non-work-item).
WORK_ITEM_SKILLS = [
    "audit",
    "author-command",
    "cleanup",
    "code-review",
    "effort-and-risk",
    "find-related",
    "implement",
    "intake",
    "plan",
    "refactor",
    "resolve-pr-comments",
    "ship",
    "test",
    "triage",
]

NON_WORK_ITEM_SKILLS = ["speak"]

FINAL_STEP_HEADER = "## Final step: standardized end-of-session report"
INVOCATION = (
    "python3 $(skill_path report)/scripts/render_report.py <work-item-id>"
)
VISIBILITY_INSTRUCTION = "paste it verbatim into"


def _read(skill_name: str) -> str:
    path = SKILL_DIR / skill_name / "SKILL.md"
    assert path.exists(), f"missing SKILL.md for {skill_name}: {path}"
    return path.read_text(encoding="utf-8")


class TestEveryWorkItemSkillWired:
    def test_every_work_item_skill_has_final_step_section(self):
        for skill_name in WORK_ITEM_SKILLS:
            text = _read(skill_name)
            assert FINAL_STEP_HEADER in text, (
                f"{skill_name}: missing '{FINAL_STEP_HEADER}'"
            )

    def test_final_step_section_invokes_render_report(self):
        for skill_name in WORK_ITEM_SKILLS:
            text = _read(skill_name)
            assert INVOCATION in text, (
                f"{skill_name}: missing render_report invocation"
            )

    def test_final_step_section_requires_pasting_report_into_response(self):
        """The report must reach the operator — the skill must instruct the
        agent to paste the rendered stdout into its final response, not
        leave it as tool output."""
        for skill_name in WORK_ITEM_SKILLS:
            text = _read(skill_name)
            assert VISIBILITY_INSTRUCTION in text, (
                f"{skill_name}: final step does not require pasting the "
                "rendered report into the final response"
            )

    def test_report_skill_itself_documents_the_visibility_contract(self):
        """The helper's own SKILL.md must document that callers paste the
        rendered output into their final response."""
        text = _read("report")
        assert VISIBILITY_INSTRUCTION in text, (
            "report: invocation contract does not require pasting output "
            "into the final response"
        )


class TestNonWorkItemSkillsUnwired:
    def test_speak_is_not_wired(self):
        for skill_name in NON_WORK_ITEM_SKILLS:
            text = _read(skill_name)
            assert FINAL_STEP_HEADER not in text, (
                f"{skill_name}: non-work-item skill must not carry the "
                "report final-step section"
            )
            assert INVOCATION not in text, (
                f"{skill_name}: non-work-item skill must not invoke render_report"
            )

    def test_all_skilled_skills_are_covered_by_one_of_the_lists(self):
        """Guard against adding a new work-item skill without wiring it."""
        actual = sorted(
            d.name
            for d in SKILL_DIR.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        )
        expected = sorted(WORK_ITEM_SKILLS + NON_WORK_ITEM_SKILLS + ["report"])
        assert actual == expected, (
            f"skill set changed: new/dropped skills not covered. "
            f"Actual: {actual} vs expected: {expected}"
        )