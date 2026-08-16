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


# ── Conclusion ───────────────────────────────────────────────────────────────

def test_conclusion_format():
    report = _render(next_action="review")
    expected = (
        "This completes the plan process for SA-0MSJ082OY003IQ8S "
        "(Standardize skill session end-of-session reporting via helper report skill). "
        "Ready for review."
    )
    assert expected in report
