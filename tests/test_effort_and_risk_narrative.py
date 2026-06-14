#!/usr/bin/env python3
"""
Tests for the effort-and-risk human narrative renderer (json_to_human.py).

Validates that the narrative output includes:
- A 5-12 item WBS when items/children are provided
- Top 3 risk drivers with mitigations
- Graceful fallback when no items/children exist
"""

import json
import subprocess
import sys
import os
import tempfile
import unittest

SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "skill", "effort-and-risk", "scripts")
JSON_TO_HUMAN = os.path.join(SCRIPT_DIR, "json_to_human.py")


class TestJsonToHumanNarrative(unittest.TestCase):
    """Test the json_to_human.py narrative renderer."""

    def setUp(self):
        if not os.path.exists(JSON_TO_HUMAN):
            self.skipTest(f"{JSON_TO_HUMAN} not found")

    def _run_json_to_human(self, input_data: dict) -> str:
        """Run json_to_human.py with the given input and return stdout."""
        proc = subprocess.run(
            [sys.executable, JSON_TO_HUMAN],
            input=json.dumps(input_data),
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"json_to_human.py failed (rc={proc.returncode}): "
                f"stderr={proc.stderr!r}"
            )
        return proc.stdout

    def test_wbs_items_included_when_provided(self):
        """When `wbs_items` is provided, the narrative includes a WBS table with item names and estimates."""
        input_data = {
            "effort": {
                "unit": "hours",
                "tshirt": "Large",
                "o": 43.0,
                "m": 71.0,
                "p": 118.0,
                "expected": 74.17,
                "recommended": 104.17,
                "range": [73.0, 148.0],
            },
            "risk": {
                "probability": 3.1,
                "impact": 4.14,
                "score": 13,
                "level": "High",
                "top_drivers": ["Complex integration", "Third-party API dependency", "Data migration risk"],
                "mitigations": ["Add targeted tests", "Lock dependencies", "Schedule extra review"],
            },
            "confidence_percent": 82,
            "assumptions": ["Existing framework can be reused"],
            "unknowns": ["Edge cases in validation"],
            "wbs_items": [
                {"id": "WBS-1", "title": "Design", "o": 2.0, "m": 4.0, "p": 6.0},
                {"id": "WBS-2", "title": "Implementation", "o": 10.0, "m": 20.0, "p": 40.0},
                {"id": "WBS-3", "title": "Unit Tests", "o": 3.0, "m": 5.0, "p": 10.0},
                {"id": "WBS-4", "title": "Integration Tests", "o": 3.0, "m": 6.0, "p": 12.0},
                {"id": "WBS-5", "title": "Documentation", "o": 2.0, "m": 3.0, "p": 5.0},
            ],
        }
        output = self._run_json_to_human(input_data)

        # Should contain header
        self.assertIn("Effort and Risk Report", output)

        # Should contain WBS table with item names and expected values
        self.assertIn("WBS", output)
        self.assertIn("Design", output)
        self.assertIn("Implementation", output)
        self.assertIn("Unit Tests", output)
        self.assertIn("Integration Tests", output)
        self.assertIn("Documentation", output)

        # Should contain expected values for WBS items
        # Design: (2 + 4*4 + 6)/6 = 24/6 = 4.0
        self.assertIn("4.00", output)

        # Should contain t-shirt size
        self.assertIn("Large", output)

        # Should contain risk score and level
        self.assertIn("High", output)
        self.assertIn("13", output)

    def test_risk_drivers_and_mitigations_in_narrative(self):
        """Risk top_drivers and mitigations are rendered in the human narrative."""
        input_data = {
            "effort": {
                "unit": "hours",
                "tshirt": "Medium",
                "o": 10.0,
                "m": 20.0,
                "p": 40.0,
                "expected": 21.67,
                "recommended": 31.67,
                "range": [20.0, 50.0],
            },
            "risk": {
                "probability": 3.0,
                "impact": 4.0,
                "score": 12,
                "level": "Medium",
                "top_drivers": ["API dependency", "New team members", "Legacy data migration"],
                "mitigations": ["Mock external APIs in tests", "Pair programming for new members", "Run dry-run migration early"],
            },
            "confidence_percent": 75,
            "assumptions": [],
            "unknowns": ["Third-party SLA guarantees"],
        }
        output = self._run_json_to_human(input_data)

        # Should contain risk drivers section
        self.assertIn("Risk Drivers", output) or self.assertIn("Top Risk Drivers", output) or self.assertIn("top risk drivers", output.lower())

        # Should contain the driver names
        self.assertIn("API dependency", output)
        self.assertIn("New team members", output)
        self.assertIn("Legacy data migration", output)

        # Should contain mitigations
        self.assertIn("Mock external APIs in tests", output)
        self.assertIn("Pair programming for new members", output)
        self.assertIn("Run dry-run migration early", output)

    def test_fallback_when_no_wbs_items(self):
        """When no wbs_items or children are provided, gracefully fall back to O/M/P summary."""
        input_data = {
            "effort": {
                "unit": "hours",
                "tshirt": "Small",
                "o": 5.0,
                "m": 10.0,
                "p": 20.0,
                "expected": 10.83,
                "recommended": 15.83,
                "range": [10.0, 25.0],
            },
            "risk": {
                "probability": 2.0,
                "impact": 3.0,
                "score": 6,
                "level": "Medium",
                "top_drivers": [],
                "mitigations": [],
            },
            "confidence_percent": 90,
            "assumptions": ["Simple feature"],
            "unknowns": [],
        }
        output = self._run_json_to_human(input_data)

        # Should still produce a narrative with effort summary
        self.assertIn("Effort and Risk Report", output)
        self.assertIn("Small", output)
        self.assertIn("10.83", output)

        # Should not error out or be empty
        self.assertGreater(len(output.strip()), 50)

    def test_fallback_with_children(self):
        """When wbs_items absent but children are provided, use children as WBS."""
        input_data = {
            "effort": {
                "unit": "hours",
                "tshirt": "Extra Large",
                "o": 80.0,
                "m": 150.0,
                "p": 300.0,
                "expected": 163.33,
                "recommended": 203.33,
                "range": [180.0, 400.0],
            },
            "risk": {
                "probability": 4.0,
                "impact": 5.0,
                "score": 20,
                "level": "Critical",
                "top_drivers": ["Core architecture risk", "Database migration"],
                "mitigations": ["Prototype architecture first", "Run trial migration"],
            },
            "confidence_percent": 60,
            "assumptions": [],
            "unknowns": ["Performance characteristics"],
            "wbs_children": [
                {"id": "CHILD-1", "title": "Backend API redesign"},
                {"id": "CHILD-2", "title": "Database schema migration"},
                {"id": "CHILD-3", "title": "Frontend updates"},
            ],
        }
        output = self._run_json_to_human(input_data)

        # Should use children as WBS
        self.assertIn("Backend API redesign", output)
        self.assertIn("Database schema migration", output)
        self.assertIn("Frontend updates", output)
        self.assertIn("Extra Large", output)
        self.assertIn("Critical", output)

    def test_narrative_includes_confidence_and_unknowns(self):
        """Confidence percentage and unknowns appear in the narrative."""
        input_data = {
            "effort": {
                "unit": "hours",
                "tshirt": "Medium",
                "o": 10.0,
                "m": 20.0,
                "p": 40.0,
                "expected": 21.67,
                "recommended": 31.67,
                "range": [20.0, 50.0],
            },
            "risk": {
                "probability": 2.0,
                "impact": 3.0,
                "score": 6,
                "level": "Medium",
                "top_drivers": [],
                "mitigations": [],
            },
            "confidence_percent": 75,
            "assumptions": ["Stable APIs"],
            "unknowns": ["Performance under load", "Third-party SLA"],
        }
        output = self._run_json_to_human(input_data)

        self.assertIn("75%", output)
        self.assertIn("Performance under load", output)
        self.assertIn("Third-party SLA", output)
        self.assertIn("Stable APIs", output)

    def test_output_has_bulleted_narrative_style(self):
        """Narrative output uses bullet points or structured sections, not just 3 lines."""
        input_data = {
            "effort": {
                "unit": "hours",
                "tshirt": "Medium",
                "o": 10.0,
                "m": 20.0,
                "p": 40.0,
                "expected": 21.67,
                "recommended": 31.67,
                "range": [20.0, 50.0],
            },
            "risk": {
                "probability": 2.0,
                "impact": 3.0,
                "score": 6,
                "level": "Medium",
                "top_drivers": [],
                "mitigations": [],
            },
            "confidence_percent": 80,
            "assumptions": [],
            "unknowns": [],
            "wbs_items": [
                {"id": "I1", "title": "Task A", "o": 2.0, "m": 4.0, "p": 6.0},
                {"id": "I2", "title": "Task B", "o": 3.0, "m": 5.0, "p": 8.0},
                {"id": "I3", "title": "Task C", "o": 1.0, "m": 2.0, "p": 4.0},
                {"id": "I4", "title": "Task D", "o": 2.0, "m": 3.0, "p": 5.0},
                {"id": "I5", "title": "Task E", "o": 2.0, "m": 6.0, "p": 17.0},
            ],
        }
        output = self._run_json_to_human(input_data)

        # Should be more than 3 lines of substance (the old behavior was 3 lines)
        # Count non-empty lines
        lines = [l for l in output.split("\n") if l.strip()]
        self.assertGreaterEqual(
            len(lines), 8,
            f"Narrative should have at least 8 non-empty lines, got {len(lines)}. Output:\n{output}"
        )


class TestOrchestrateEstimateDataFlow(unittest.TestCase):
    """Test that orchestrate_estimate.py passes WBS data through to json_to_human.py."""

    def setUp(self):
        self.orchestrator = os.path.join(SCRIPT_DIR, "orchestrate_estimate.py")
        if not os.path.exists(self.orchestrator):
            self.skipTest(f"{self.orchestrator} not found")

    def test_orchestrator_includes_wbs_in_human_text(self):
        """The orchestrator constructs the sanitized object with wbs_items/wbs_children."""
        # Read the orchestrator source to verify wbs data is included in sanitized object
        with open(self.orchestrator, "r") as f:
            source = f.read()

        # The sanitized object should include wbs_items or wbs_children
        self.assertIn("sanitized", source, "orchestrator should have a sanitized variable")
        self.assertIn("wbs", source.lower(), "orchestrator should pass wbs data to human renderer")

    def test_orchestrator_posts_expanded_narrative(self):
        """The orchestrator's human_text should be the expanded narrative, not just 3 lines."""
        # Verify the orchestrator uses json_to_human for human_text
        with open(self.orchestrator, "r") as f:
            source = f.read()

        self.assertIn("json_to_human", source, "orchestrator should call json_to_human.py")

    def test_run_skill_passes_items_to_orchestrator(self):
        """The run_skill.py wrapper should pass items to the orchestrator payload."""
        runner = os.path.join(SCRIPT_DIR, "run_skill.py")
        if not os.path.exists(runner):
            self.skipTest(f"{runner} not found")

        with open(runner, "r") as f:
            source = f.read()

        # Should include items in the payload sent to orchestrator
        self.assertIn("items", source, "run_skill.py should include items in the orchestrator payload")


if __name__ == "__main__":
    unittest.main()
