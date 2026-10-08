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

import re
import sys
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skill"

# Load the context-audit measurement module by path so the skill inventory
# (and the untracked-dir filter) has a single source of truth.
_MEASURE_DIR = REPO_ROOT / "skill" / "context-audit" / "scripts"
if str(_MEASURE_DIR) not in sys.path:
    sys.path.insert(0, str(_MEASURE_DIR))

import measure_context as mc

# Load the report renderer by path (same technique as the measurement module)
# so the regression test can render the ACs the plan skill documents.
_REPORT_SCRIPTS = REPO_ROOT / "skill" / "report" / "scripts"
if str(_REPORT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPORT_SCRIPTS))

import render_report as report_renderer

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
    "machine-hygiene",
    "plan",
    "refactor",
    "resolve-pr-comments",
    "ship",
    "test",
    "triage",
]

NON_WORK_ITEM_SKILLS = ["interview", "speak", "standup"]

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


# ─── Plan skill: skill-session ACs vs work-item feature ACs ────────────────
#
# Regression guard for the operator finding that `/skill:plan` ended with
# "Plan session for <id> (<title>) is incomplete. Requires attention
# (implement)." even though planning completed and implement is simply the
# next phase (SA-0MUYADP50005EYA2). The renderer derives session status from
# the supplied `--ac` verdicts, so the plan skill must pass its *own*
# planning-phase deliverables (all `met`), not the work item's feature ACs.

_FINAL_STEP_SECTION_RE = re.compile(
    r"## Final step: standardized end-of-session report(.*)\Z", re.DOTALL,
)
_PLACEHOLDER_AC_RE = re.compile(r'--ac\s+"<AC# description>')
_AC_LINE_RE = re.compile(r'--ac\s+"([^"]+)"')


def _final_step_section(skill_name: str) -> str:
    """Return the text from the final-step heading to end of SKILL.md."""
    match = _FINAL_STEP_SECTION_RE.search(_read(skill_name))
    assert match, f"{skill_name}: no final-step section found"
    return match.group(1)


def _documented_acs(section: str) -> list[tuple[str, str, str]]:
    """Parse the `--ac "desc|metric|verdict"` lines from a section."""
    parsed = []
    for match in _AC_LINE_RE.finditer(section):
        value = match.group(1)
        parts = [p.strip() for p in value.split("|")]
        assert len(parts) == 3, (
            f"malformed --ac value (expected desc|metric|verdict): {value!r}"
        )
        parsed.append((parts[0], parts[1], parts[2]))
    return parsed


class TestPlanSkillPassesPlanningPhaseAcs:
    def test_final_step_has_no_placeholder_ac(self):
        """A generic `<AC# description>` placeholder lets agents pass the
        work item's feature ACs, which render the report as incomplete."""
        section = _final_step_section("plan")
        assert not _PLACEHOLDER_AC_RE.search(section), (
            "plan: final step still uses placeholder --ac values; pass concrete "
            "planning-phase ACs so a completed plan never renders as incomplete"
        )

    def test_final_step_passes_concrete_met_acs(self):
        section = _final_step_section("plan")
        acs = _documented_acs(section)
        assert len(acs) >= 3, (
            f"plan: expected >=3 concrete planning-phase ACs, found {len(acs)}"
        )
        for desc, metric, verdict in acs:
            assert desc and metric, f"plan: empty AC field in {desc!r}/{metric!r}"
            assert verdict == "met", (
                f"plan: AC {desc!r} must be documented as 'met' on a successful "
                f"plan, got {verdict!r}"
            )

    def test_final_step_distinguishes_skill_session_from_feature_acs(self):
        section = _final_step_section("plan").lower()
        assert "skill session" in section, (
            "plan: final step must state that --ac records the skill session's "
            "own deliverables"
        )
        assert "feature acceptance criteria" in section or "feature acs" in section, (
            "plan: final step must warn against passing the work item's feature ACs"
        )

    def test_documented_planning_phase_acs_render_as_completed(self):
        """The ACs the skill tells agents to pass must render as a completed
        plan — this is the operator-visible behaviour under test."""
        acs = _documented_acs(_final_step_section("plan"))
        criteria = [
            (str(i), desc, metric, verdict)
            for i, (desc, metric, verdict) in enumerate(acs, start=1)
        ]
        metadata = {
            "Type": "feature",
            "Priority": "medium",
            "Status": "open",
            "Stage": "plan_complete",
            "Risk": "medium",
            "Effort": "M",
            "Children": "3",
            "Audit": "not run",
        }
        report = report_renderer.render_report(
            skill_name="plan",
            work_item_id="SA-0EXAMPLE00000",
            title="Example plan",
            headline="Produced the feature breakdown.",
            acceptance_criteria=criteria,
            metadata=metadata,
            next_action="implement",
        )
        assert report.startswith("# Completed plan")
        assert "Ready for implement." in report
        assert "incomplete" not in report.lower()


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
        """Guard against adding a new work-item skill without wiring it.

        Untracked ``skill/<name>/`` directories are another agent's WIP and
        are ignored with a clear warning rather than failing the suite
        (SA-0MUPDDMXB0088CMM).
        """
        untracked = mc.untracked_skill_dirs(REPO_ROOT)
        if untracked:
            warnings.warn(
                "ignoring untracked skill dir(s) (concurrent WIP): "
                + ", ".join(untracked),
                stacklevel=2,
            )
        tracked = mc.tracked_skill_dirs(REPO_ROOT)
        actual = sorted(
            d.name
            for d in SKILL_DIR.iterdir()
            if d.is_dir()
            and (d / "SKILL.md").exists()
            and (tracked is None or d.name in tracked)
        )
        expected = sorted(WORK_ITEM_SKILLS + NON_WORK_ITEM_SKILLS + ["report"])
        assert actual == expected, (
            f"skill set changed: new/dropped skills not covered. "
            f"Actual: {actual} vs expected: {expected}"
        )