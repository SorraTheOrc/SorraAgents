"""
Tests for the report renderer (skill/report/scripts/render_report.py).

Validates the canonical end-of-session report template: sections present,
AC table formatting, Meta-Data icon rendering, default handling, and
the `## Notes` section positioning.

Tests are decoupled from live `wl` — they use fixture JSON and patch
the renderer to accept explicit parameters.
"""

import json
import os
import sys
import textwrap
import unittest

# Ensure the scripts directory is on the path so we can import the renderer.
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

# ─── Fixture data ────────────────────────────────────────────────────────

FULL_WORK_ITEM_JSON = textwrap.dedent("""\
{
  "id": "SA-0TEST0000000001",
  "title": "Test work item for report renderer",
  "status": "in-progress",
  "priority": "high",
  "issueType": "feature",
  "stage": "in_progress",
  "risk": "medium",
  "effort": "M",
  "childCount": 3,
  "auditResult": true
}
""")

MINIMAL_WORK_ITEM_JSON = textwrap.dedent("""\
{
  "id": "SA-0TEST0000000002",
  "title": "Minimal work item",
  "status": "open",
  "priority": "low",
  "issueType": "task",
  "stage": "idea",
  "risk": null,
  "effort": null,
  "childCount": 0,
  "auditResult": null
}
""")

EMPTY_WORK_ITEM_JSON = "{}"


class TestReportSectionsPresent(unittest.TestCase):
    """Verify that every required section appears in the rendered report."""

    def setUp(self):
        from render_report import render_report
        self.render_report = render_report

    def test_all_required_sections_present(self):
        """All 7 sections should be present in the output."""
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test title",
            headline="A test headline",
            acceptance_criteria=[
                ("1", "Test AC", "verified by unit test", "met"),
            ],
            metadata={
                "Type": "🔷 feature",
                "Priority": "⭐ high",
                "Status": "🔄 in-progress",
                "Stage": "🛠️ in_progress",
                "Risk": "⚠️ medium",
                "Effort": "🐕 M",
                "Children": "👥 3",
                "Audit": "✅ passed",
            },
            producer_actions="Review the implementation.",
            notes="Some notes here.",
            next_action="plan",
        )
        self.assertIn("# Completed test-skill", result)
        self.assertIn("## Meta-Data", result)
        self.assertIn("## Acceptance Criteria", result)
        self.assertIn("## Producer Actions", result)
        self.assertIn("## Notes", result)
        self.assertIn("## Conclusion", result)
        self.assertIn("A test headline", result)

    def test_headline_present(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test title",
            headline="My headline",
            acceptance_criteria=[],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("My headline", result)

    def test_work_item_id_in_header(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test title",
            headline="",
            acceptance_criteria=[],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("SA-0TEST0000000001", result)
        # Title should be bold and id in parens per canonical template
        self.assertIn("**Test title**", result)


class TestACTableRows(unittest.TestCase):
    """Verify AC table rows render as `|<ac#>|<Description>|<Metric>|<met|unmet>|`."""

    def setUp(self):
        from render_report import render_report
        self.render_report = render_report

    def test_single_met_ac(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[
                ("1", "Feature works", "unit test passes", "met"),
            ],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("|1|", result)
        self.assertIn("Feature works", result)
        self.assertIn("unit test passes", result)
        self.assertIn("|met|", result)

    def test_single_unmet_ac(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[
                ("2", "Still broken", "see notes", "unmet"),
            ],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="plan",
        )
        self.assertIn("|2|", result)
        self.assertIn("Still broken", result)
        self.assertIn("see notes", result)
        self.assertIn("|unmet|", result)

    def test_multiple_ac_rows(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[
                ("1", "AC one", "check", "met"),
                ("2", "AC two", "check", "unmet"),
                ("3", "AC three", "check", "met"),
            ],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("|1|", result)
        self.assertIn("|2|", result)
        self.assertIn("|3|", result)
        self.assertIn("AC one", result)
        self.assertIn("AC two", result)
        self.assertIn("AC three", result)

    def test_empty_ac_list(self):
        """Empty AC list renders the placeholder row."""
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("## Acceptance Criteria", result)
        self.assertIn("No acceptance criteria supplied", result)


class TestMetaDataIcons(unittest.TestCase):
    """Verify Meta-Data icons and values are rendered correctly."""

    def setUp(self):
        from render_report import render_report
        self.render_report = render_report

    def test_meta_data_type(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={"Type": "🔷 feature"},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("Type", result)
        self.assertIn("feature", result)

    def test_meta_data_priority(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={"Priority": "⭐ high"},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("Priority", result)
        self.assertIn("high", result)

    def test_meta_data_status(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={"Status": "🔄 in-progress"},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("Status", result)
        self.assertIn("in-progress", result)

    def test_meta_data_stage(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={"Stage": "🛠️ in_progress"},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("Stage", result)
        self.assertIn("in_progress", result)

    def test_meta_data_risk(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={"Risk": "⚠️ medium"},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("Risk", result)
        self.assertIn("medium", result)

    def test_meta_data_effort(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={"Effort": "🐕 M"},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("Effort", result)
        self.assertIn("M", result)

    def test_meta_data_children(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={"Children": "👥 3"},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("Children", result)
        self.assertIn("3", result)

    def test_meta_data_audit(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={"Audit": "✅ passed"},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("Audit", result)
        self.assertIn("passed", result)

    def test_all_meta_data_fields(self):
        """All 8 Meta-Data fields present."""
        metadata = {
            "Type": "🔷 feature",
            "Priority": "⭐ high",
            "Status": "🔄 in-progress",
            "Stage": "🛠️ in_progress",
            "Risk": "⚠️ medium",
            "Effort": "🐕 M",
            "Children": "👥 3",
            "Audit": "✅ passed",
        }
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata=metadata,
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        for key in metadata:
            self.assertIn(key, result)


class TestDefaults(unittest.TestCase):
    """Verify default handling: empty Producer Actions, missing metadata."""

    def setUp(self):
        from render_report import render_report
        self.render_report = render_report

    def test_empty_producer_actions_defaults_to_none_needed(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={},
            producer_actions="",
            notes=None,
            next_action="review",
        )
        self.assertIn("None needed", result)

    def test_none_producer_actions_defaults_to_none_needed(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("None needed", result)

    def test_empty_metadata_neutral_fallback(self):
        """Empty metadata dict should still produce a Meta-Data section."""
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("## Meta-Data", result)


class TestNotesSectionPosition(unittest.TestCase):
    """Verify `## Notes` sits between `## Producer Actions` and `## Conclusion`."""

    def setUp(self):
        from render_report import render_report
        self.render_report = render_report

    def test_notes_between_producer_actions_and_conclusion(self):
        result = self.render_report(
            skill_name="test-skill",
            work_item_id="SA-0TEST0000000001",
            title="Test",
            headline="",
            acceptance_criteria=[],
            metadata={},
            producer_actions="Do this.",
            notes="Some notes.",
            next_action="plan",
        )
        producer_actions_pos = result.find("## Producer Actions")
        notes_pos = result.find("## Notes")
        conclusion_pos = result.find("## Conclusion")
        self.assertLess(producer_actions_pos, notes_pos)
        self.assertLess(notes_pos, conclusion_pos)


class TestConclusion(unittest.TestCase):
    """Verify the Conclusion section format."""

    def setUp(self):
        from render_report import render_report
        self.render_report = render_report

    def test_conclusion_format(self):
        result = self.render_report(
            skill_name="implement",
            work_item_id="SA-0TEST0000000001",
            title="Test title",
            headline="",
            acceptance_criteria=[],
            metadata={},
            producer_actions=None,
            notes=None,
            next_action="plan",
        )
        self.assertIn("This completes the implement process for SA-0TEST0000000001 (Test title).", result)
        self.assertIn("Ready for plan.", result)


class TestParseAcArgs(unittest.TestCase):
    """Verify the _parse_ac_args helper function."""

    def setUp(self):
        from render_report import _parse_ac_args
        self._parse_ac_args = _parse_ac_args

    def test_empty_list(self):
        result = self._parse_ac_args(None)
        self.assertEqual(result, [])

    def test_single_ac(self):
        result = self._parse_ac_args(["desc|metric|met"])
        self.assertEqual(result, [("1", "desc", "metric", "met")])

    def test_multiple_acs(self):
        result = self._parse_ac_args(["desc1|metric1|met", "desc2|metric2|unmet"])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], ("1", "desc1", "metric1", "met"))
        self.assertEqual(result[1], ("2", "desc2", "metric2", "unmet"))

    def test_bad_format_defaults_to_unmet(self):
        result = self._parse_ac_args(["bad format"])
        self.assertEqual(result[0][3], "unmet")

    def test_verdict_normalization(self):
        result = self._parse_ac_args(["desc|metric|MET"])
        self.assertEqual(result[0][3], "met")


class TestRendererWithFixture(unittest.TestCase):
    """Integration-style tests: feed a work-item fixture into the renderer
    that reads from `wl show --json`-style JSON."""

    def test_full_report_from_fixture(self):
        """Build a report from a full work-item JSON fixture."""
        from render_report import render_report_from_workitem

        work_item = json.loads(FULL_WORK_ITEM_JSON)
        result = render_report_from_workitem(
            work_item=work_item,
            skill_name="test-skill",
            headline="Test headline",
            acceptance_criteria=[
                ("1", "Feature works", "unit test passes", "met"),
                ("2", "Docs updated", "README checked", "met"),
            ],
            producer_actions="Review the changes.",
            notes="This is a test.",
            next_action="plan",
        )
        # Check key sections
        self.assertIn("# Completed test-skill", result)
        self.assertIn("SA-0TEST0000000001", result)
        self.assertIn("Test work item for report renderer", result)
        self.assertIn("Test headline", result)
        self.assertIn("|1|", result)
        self.assertIn("|2|", result)
        self.assertIn("Feature works", result)
        self.assertIn("Docs updated", result)
        self.assertIn("## Meta-Data", result)
        self.assertIn("## Producer Actions", result)
        self.assertIn("Review the changes.", result)
        self.assertIn("## Notes", result)
        self.assertIn("This is a test.", result)
        self.assertIn("## Conclusion", result)
        self.assertIn("Ready for plan.", result)

    def test_minimal_report_from_fixture(self):
        """Work with a minimal fixture (null/missing fields)."""
        from render_report import render_report_from_workitem

        work_item = json.loads(MINIMAL_WORK_ITEM_JSON)
        result = render_report_from_workitem(
            work_item=work_item,
            skill_name="test-skill",
            headline="",
            acceptance_criteria=[],
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("# Completed test-skill", result)
        self.assertIn("SA-0TEST0000000002", result)
        self.assertIn("Minimal work item", result)
        self.assertIn("## Meta-Data", result)
        self.assertIn("## Acceptance Criteria", result)
        self.assertIn("## Producer Actions", result)
        self.assertIn("None needed", result)
        self.assertIn("## Notes", result)
        self.assertIn("## Conclusion", result)

    def test_empty_work_item_defaults(self):
        """Even an empty fixture produces valid output."""
        from render_report import render_report_from_workitem

        work_item = json.loads(EMPTY_WORK_ITEM_JSON)
        result = render_report_from_workitem(
            work_item=work_item,
            skill_name="test-skill",
            headline="",
            acceptance_criteria=[],
            producer_actions=None,
            notes=None,
            next_action="review",
        )
        self.assertIn("# Completed test-skill", result)
        self.assertIn("## Conclusion", result)


if __name__ == "__main__":
    unittest.main()
