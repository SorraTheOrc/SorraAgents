#!/usr/bin/env python3
"""Tests: report helper skill renderer (SA-0MSRFPUTN0089F3U).

Validates the canonical end-of-session report produced by
``skill/report/scripts/render_report.py`` against the report spec in the
parent item (SA-0MSJ082OY003IQ8S):

- All required sections present in the renderer output, in spec order.
- AC table rows render as ``|<ac#>|<Description>|<Metric>|<met|unmet>|``.
- Meta-Data icons + values for Type, Priority, Status, Stage, Risk,
  Effort, Children, Audit — including bracketed-text fallbacks
  (ContextHub canonical set; ``no_icons`` mode).
- Defaults: empty Producer Actions renders ``None needed``; missing/unset
  metadata renders a neutral fallback.
- ``## Notes`` sits between ``## Producer Actions`` and ``## Conclusion``.

Tests run offline: they exercise the renderer with fixture JSON matching
the ``wl show <id> --children --json`` shape — no live ``wl`` calls.
"""  # noqa: EXE001
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

_SCRIPT_PATH = REPO_ROOT / "skill" / "report" / "scripts" / "render_report.py"
_spec = importlib.util.spec_from_file_location("render_report", _SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

# ── Fixtures: `wl show <id> --children --json` shape ────────────────────────

VALID_DATA = {
    "workItem": {
        "id": "SA-0MSJ082OY003IQ8S",
        "title": "Standardize skill session end-of-session reporting via helper report skill",
        "issueType": "feature",
        "priority": "medium",
        "status": "in-progress",
        "stage": "plan_complete",
        "risk": "Medium",
        "effort": "Medium",
    },
    "auditResult": None,
    "children": [
        {"id": "SA-0MSRFPUTN0089F3U", "title": "Write tests for report renderer"},
        {"id": "SA-0MSRFTP2Y008BH6L", "title": "Create report helper skill"},
    ],
}

MINIMAL_DATA = {
    "workItem": {
        "id": "SA-0MSRFPUTN0089F3U",
        "title": "Write tests for report renderer",
        "issueType": "",
        "priority": "",
        "status": "",
        "stage": "",
        "risk": "",
        "effort": "",
    },
    "auditResult": None,
    "children": [],
}

AC_ROWS = [
    {
        "description": "Helper skill exists",
        "metric": "skill/report/SKILL.md present",
        "met": True,
    },
    {
        "description": "All work-item skills wired",
        "metric": "grep across 16 SKILL.md files",
        "met": False,
    },
]

HEADLINE = "Implemented the canonical report helper and verified the full suite."


def _render(data=VALID_DATA, **kwargs):
    """Render a report with sensible defaults, overriding via kwargs."""
    defaults = {
        "skill_name": "plan",
        "headline": HEADLINE,
        "ac_rows": AC_ROWS,
        "notes": "Freeform notes text.",
    }
    defaults.update(kwargs)
    return mod.render_report(data, **defaults)


# ── Sections present, in spec order ─────────────────────────────────────────

def test_all_required_sections_present():
    report = _render()
    expected = [
        "# Completed plan",
        "**Standardize skill session end-of-session reporting via helper report skill** (SA-0MSJ082OY003IQ8S)",
        HEADLINE,
        "## Acceptance Criteria",
        "## Meta-Data",
        "## Producer Actions",
        "## Notes",
        "## Conclusion",
    ]
    for part in expected:
        assert part in report, f"missing expected part: {part!r}"
    # Spec order: AC → Meta-Data → Producer Actions → Notes → Conclusion.
    order = [report.index(p) for p in [
        "## Acceptance Criteria",
        "## Meta-Data",
        "## Producer Actions",
        "## Notes",
        "## Conclusion",
    ]]
    assert order == sorted(order), "report sections out of spec order"


def test_notes_between_producer_actions_and_conclusion():
    report = _render()
    pa = report.index("## Producer Actions")
    notes = report.index("## Notes")
    conclusion = report.index("## Conclusion")
    assert pa < notes < conclusion


# ── AC table rows ───────────────────────────────────────────────────────────

def test_ac_table_rows_format():
    report = _render()
    assert "| AC# | Description | Metric | Verdict |" in report
    assert "| 1 | Helper skill exists | skill/report/SKILL.md present | met |" in report
    assert "| 2 | All work-item skills wired | grep across 16 SKILL.md files | unmet |" in report


def test_ac_table_empty_rows():
    report = _render(ac_rows=[])
    assert "## Acceptance Criteria" in report
    assert "| AC# | Description | Metric | Verdict |" in report


def test_ac_table_renders_audit_runner_verdict_field():
    # Audit-runner rows carry a string `verdict` (met/unmet/adjusted/partial).
    report = _render(ac_rows=[
        {"description": "A", "metric": "m", "verdict": "adjusted"},
        {"description": "B", "metric": "m", "verdict": "partial"},
        {"description": "C", "metric": "m", "verdict": "met"},
        {"description": "D", "metric": "m", "verdict": "unmet"},
    ])
    assert "| 1 | A | m | adjusted |" in report
    assert "| 2 | B | m | partial |" in report
    assert "| 3 | C | m | met |" in report
    assert "| 4 | D | m | unmet |" in report


def test_ac_table_verdict_field_takes_precedence_over_met():
    # When both present, the explicit verdict must win over the met boolean.
    report = _render(ac_rows=[
        {"description": "A", "metric": "m", "verdict": "adjusted", "met": False},
    ])
    assert "| 1 | A | m | adjusted |" in report


def test_ac_table_string_met_is_passed_through_as_verdict():
    # _parse_ac keeps the boolean-only return shape, so adjusted/partial
    # travel as a string in the `met` field; render_ac_table must not
    # collapse that back to met/unmet.
    report = _render(ac_rows=[
        {"description": "A", "metric": "m", "met": "adjusted"},
        {"description": "B", "metric": "m", "met": "partial"},
    ])
    assert "| 1 | A | m | adjusted |" in report
    assert "| 2 | B | m | partial |" in report


def test_parse_ac_adjusted_partial_roundtrip():
    # CLI --ac acceptance of adjusted/partial, round-tripped through the
    # renderer so the final table shows the exact verdict string.
    rows = [
        mod._parse_ac("A|m|adjusted"),
        mod._parse_ac("B|m|partial"),
        mod._parse_ac("C|m|met"),
        mod._parse_ac("D|m|unmet"),
    ]
    report = _render(ac_rows=rows)
    assert "| 1 | A | m | adjusted |" in report
    assert "| 2 | B | m | partial |" in report
    assert "| 3 | C | m | met |" in report
    assert "| 4 | D | m | unmet |" in report


# ── Meta-Data: icons + values (ContextHub canonical set) ────────────────────

def test_metadata_icons_and_values():
    report = _render()
    expected_lines = [
        "- Type: feature",
        "- Priority: 📋 medium",
        "- Status: 🔄 in-progress",
        "- Stage: 📋 plan_complete",
        "- Risk: ⚠️ Medium",
        "- Effort: 🐕 Medium",
        "- Children: 2",
        "- Audit: ❔ not run",
    ]
    for line in expected_lines:
        assert line in report, f"missing Meta-Data line: {line!r}"


def test_metadata_bracketed_text_fallbacks():
    report = _render(no_icons=True)
    expected_lines = [
        "- Type: feature",
        "- Priority: [MED] medium",
        "- Status: [INPR] in-progress",
        "- Stage: [PLAN] plan_complete",
        "- Risk: [MED] Medium",
        "- Effort: [M] Medium",
        "- Children: 2",
        "- Audit: [UNKN] not run",
    ]
    for line in expected_lines:
        assert line in report, f"missing fallback Meta-Data line: {line!r}"
    # Emoji must NOT appear in no_icons mode.
    assert "📋" not in report
    assert "🔄" not in report


def test_metadata_epic_type_icon():
    data = dict(VALID_DATA)
    data["workItem"] = dict(data["workItem"], issueType="epic")
    report = _render(data)
    assert "- Type: 🏰 epic" in report


# ── Defaults: Producer Actions + neutral fallback for missing metadata ──────

def test_producer_actions_default_none_needed():
    report = _render(producer_actions=None)
    assert "## Producer Actions" in report
    assert "None needed" in report


def test_producer_actions_custom():
    report = _render(producer_actions="Run /skill:ship release")
    assert "Run /skill:ship release" in report


def test_missing_metadata_renders_neutral_fallback():
    report = _render(MINIMAL_DATA, ac_rows=[])
    for line in [
        "- Type: — N/A",
        "- Priority: — N/A",
        "- Status: — N/A",
        "- Stage: — N/A",
        "- Risk: — N/A",
        "- Effort: — N/A",
        "- Children: 0",
        "- Audit: ❔ not run",
    ]:
        assert line in report, f"missing neutral Meta-Data line: {line!r}"


def test_missing_metadata_neutral_fallback_no_icons():
    report = _render(MINIMAL_DATA, ac_rows=[], no_icons=True)
    assert "- Risk: — N/A" in report
    assert "- Audit: [UNKN] not run" in report


# ── Audit icon transitions ───────────────────────────────────────────────────

def test_audit_passed():
    data = dict(VALID_DATA)
    data["auditResult"] = {"workItemId": "SA-0MSJ082OY003IQ8S", "readyToClose": True}
    assert "- Audit: ✅ passed" in _render(data)
    assert "- Audit: [YES] passed" in _render(data, no_icons=True)


def test_audit_failed():
    data = dict(VALID_DATA)
    data["auditResult"] = {"workItemId": "SA-0MSJ082OY003IQ8S", "readyToClose": False}
    assert "- Audit: ❌ failed" in _render(data)
    assert "- Audit: [NO] failed" in _render(data, no_icons=True)


# ── Icon mapping drift guard (ContextHub canonical set) ─────────────────────
#
# Pins the renderer's icon tables to the values documented in
# ../ContextHub/docs/icons-design.md (the canonical set consumed by the wl
# CLI). If ContextHub's documented mapping changes, this test fails and the
# renderer tables + SKILL.md mapping table must be updated together — the
# report helper's icon sets cannot drift independently.

def test_priority_icons_match_contexthub_docs():
    expected = {
        "critical": ("🚨", "[CRIT]"),
        "high": ("⭐", "[HIGH]"),
        "medium": ("📋", "[MED]"),
        "low": ("🐢", "[LOW]"),
    }
    assert mod.PRIORITY_ICONS == expected


def test_status_icons_match_contexthub_docs():
    expected = {
        "open": ("🔓", "[OPEN]"),
        "in-progress": ("🔄", "[INPR]"),
        "completed": ("✔️", "[DONE]"),
        "blocked": ("⛔", "[BLKD]"),
        "deleted": ("🗑️", "[DEL]"),
        "input_needed": ("💬", "[HELP]"),
    }
    assert mod.STATUS_ICONS == expected


def test_stage_icons_match_contexthub_docs():
    expected = {
        "idea": ("💡", "[IDEA]"),
        "intake_complete": ("📥", "[INTAKE]"),
        "plan_complete": ("📋", "[PLAN]"),
        "in_progress": ("🛠️", "[PROG]"),
        "in_review": ("🔍", "[REVIEW]"),
        "done": ("🏁", "[DONE]"),
    }
    assert mod.STAGE_ICONS == expected


def test_risk_icons_match_contexthub_docs():
    expected = {
        "low": ("🌱", "[LOW]"),
        "medium": ("⚠️", "[MED]"),
        "high": ("🔥", "[HIGH]"),
        "severe": ("🚨", "[SEV]"),
    }
    assert mod.RISK_ICONS == expected


def test_effort_icons_match_contexthub_docs():
    expected = {
        "xs": ("🐜", "[XS]"),
        "s": ("🐇", "[S]"),
        "m": ("🐕", "[M]"),
        "l": ("🐘", "[L]"),
        "xl": ("🐋", "[XL]"),
        "extra small": ("🐜", "[XS]"),
        "small": ("🐇", "[S]"),
        "medium": ("🐕", "[M]"),
        "large": ("🐘", "[L]"),
        "extra large": ("🐋", "[XL]"),
        "xlarge": ("🐋", "[XL]"),
    }
    assert mod.EFFORT_ICONS == expected


def test_audit_icons_match_contexthub_docs():
    expected = {
        "yes": ("✅", "[YES]"),
        "no": ("❌", "[NO]"),
        "unknown": ("❔", "[UNKN]"),
    }
    assert mod.AUDIT_ICONS == expected


def test_epic_type_icon_matches_contexthub_docs():
    assert mod.EPIC_ICONS == {"epic": ("🏰", "[EPIC]")}


# ── CLI --ac argument parsing ─────────────────────────────────────────────────
#
# The parser accepts exactly 3 pipe-separated fields: description|metric|verdict
# where verdict is one of met/unmet (case-insensitive; yes/true/1 aliases).


def test_parse_ac_met_verdict():
    parsed = mod._parse_ac("Helper skill exists|grep SKILL.md|met")
    assert parsed == {
        "description": "Helper skill exists",
        "metric": "grep SKILL.md",
        "met": True,
    }


def test_parse_ac_unmet_verdict():
    parsed = mod._parse_ac("All skills wired|grep across 16 SKILL.md files|unmet")
    assert parsed == {"description": "All skills wired", "metric": "grep across 16 SKILL.md files", "met": False}


def test_parse_ac_strips_whitespace():
    parsed = mod._parse_ac("  Desc  |  Metric  |  met  ")
    assert parsed == {"description": "Desc", "metric": "Metric", "met": True}


def test_parse_ac_verdict_aliases():
    for alias in ("yes", "true", "1", "MET", "Met"):
        assert mod._parse_ac(f"Desc|Metric|{alias}")["met"] is True, f"alias {alias!r} should parse as met"


def test_parse_ac_rejects_four_fields():
    # The old (incorrectly documented) 4-field form must fail loudly.
    try:
        mod._parse_ac("Desc|Metric|met|unmet")
    except SystemExit as exc:
        assert "met" in str(exc.code)
    else:
        raise AssertionError("4-field spec should raise SystemExit")


def test_parse_ac_rejects_too_few_fields():
    try:
        mod._parse_ac("Desc|Metric")
    except SystemExit:
        return
    raise AssertionError("2-field spec should raise SystemExit")


# ── Conclusion ───────────────────────────────────────────────────────────────

def test_conclusion_format():
    report = _render(next_action="review")
    expected = (
        "This completes the plan process for SA-0MSJ082OY003IQ8S "
        "(Standardize skill session end-of-session reporting via helper report skill). "
        "Ready for review."
    )
    assert expected in report
