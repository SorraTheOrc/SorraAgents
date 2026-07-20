#!/usr/bin/env python3
"""
Tests for the refactored helper functions extracted from main() in
orchestrate_estimate.py.

Validates that each extracted function behaves correctly in isolation
(unit-tested with mocked dependencies where needed) and that the
refactored main() remains functionally equivalent to the original.
"""

import io
import json
import os
import sys
import unittest
from unittest.mock import patch, mock_open, MagicMock

# Add the target script directory to sys.path
_SCRIPT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skill", "effort-and-risk", "scripts"
)
sys.path.insert(0, _SCRIPT_DIR)

# Patch sys.exit to raise instead of exit during tests
_exit_codes = []


class ExitCaptured(BaseException):
    """Raised when sys.exit is called, carrying the exit code.

    Inherits from BaseException (like SystemExit) so it is NOT caught
    by ``except Exception`` handlers in the production code.
    """

    def __init__(self, code=0):
        self.code = code
        super().__init__(f"sys.exit({code})")


def _mock_exit(code=0):
    raise ExitCaptured(code)


# Import target module
import orchestrate_estimate as oe  # noqa: E402

# Also import shared for test assertions
from _shared import DEFAULT_THRESHOLDS, TSHIRT_MAP, pick_tshirt, level_from_score  # noqa: E402


class TestLoadThresholds(unittest.TestCase):
    """Tests for _load_thresholds()."""

    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps({
        "thresholds": {"XS": {"min": 0, "max": 5}, "S": {"min": 5, "max": None}}
    }))
    def test_loads_thresholds_from_file(self, mock_file):
        """Successfully loads thresholds from references/t-shirt_sizes.json."""
        result = oe._load_thresholds()
        self.assertEqual(result, {"XS": {"min": 0, "max": 5}, "S": {"min": 5, "max": None}})

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_falls_back_to_defaults_on_missing_file(self, mock_file):
        """Falls back to DEFAULT_THRESHOLDS when file is missing."""
        result = oe._load_thresholds()
        self.assertEqual(result, DEFAULT_THRESHOLDS)

    @patch("builtins.open", new_callable=mock_open, read_data="not valid json")
    def test_falls_back_on_parse_error(self, mock_file):
        """Falls back to DEFAULT_THRESHOLDS on JSON parse failure."""
        result = oe._load_thresholds()
        self.assertEqual(result, DEFAULT_THRESHOLDS)

    @patch("builtins.open", side_effect=PermissionError)
    def test_falls_back_on_permission_error(self, mock_file):
        """Falls back to DEFAULT_THRESHOLDS on permission error."""
        result = oe._load_thresholds()
        self.assertEqual(result, DEFAULT_THRESHOLDS)

    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps({"not_thresholds": {}}))
    def test_returns_empty_dict_when_no_thresholds_key(self, mock_file):
        """Returns an empty dict when the JSON lacks a 'thresholds' key."""
        result = oe._load_thresholds()
        self.assertEqual(result, {})


class TestFetchIssueStage(unittest.TestCase):
    """Tests for _fetch_issue_stage()."""

    def setUp(self):
        # Patch sys.exit to raise instead of exiting
        self.exit_patcher = patch.object(oe, "sys")
        self.mock_sys = self.exit_patcher.start()
        self.mock_sys.exit.side_effect = _mock_exit
        # Redirect stdout to avoid prints during tests
        self.mock_sys.stdout = MagicMock()
        self.addCleanup(self.exit_patcher.stop)

    @patch("subprocess.run")
    def test_returns_stage_on_success(self, mock_run):
        """Returns the stage when wl show succeeds with a valid stage."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"workItem": {"stage": "plan_complete"}}),
            stderr="",
        )
        result = oe._fetch_issue_stage("TEST-123")
        self.assertEqual(result, "plan_complete")

    @patch("subprocess.run")
    def test_returns_stage_intake_complete(self, mock_run):
        """Returns intake_complete stage as-is."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"workItem": {"stage": "intake_complete"}}),
            stderr="",
        )
        result = oe._fetch_issue_stage("TEST-123")
        self.assertEqual(result, "intake_complete")

    @patch("subprocess.run")
    def test_exits_on_show_failure(self, mock_run):
        """Exits with code 3 when wl show returns non-zero."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error",
        )
        with self.assertRaises(ExitCaptured) as ctx:
            oe._fetch_issue_stage("TEST-123")
        self.assertEqual(ctx.exception.code, 3)

    @patch("subprocess.run")
    def test_exits_on_invalid_stage(self, mock_run):
        """Exits with code 4 when issue stage is not acceptable."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"workItem": {"stage": "in_progress"}}),
            stderr="",
        )
        with self.assertRaises(ExitCaptured) as ctx:
            oe._fetch_issue_stage("TEST-123")
        self.assertEqual(ctx.exception.code, 4)

    @patch("subprocess.run", side_effect=Exception("subprocess crashed"))
    def test_exits_on_exception(self, mock_run):
        """Exits with code 5 when subprocess raises an exception."""
        with self.assertRaises(ExitCaptured) as ctx:
            oe._fetch_issue_stage("TEST-123")
        self.assertEqual(ctx.exception.code, 5)

    @patch("subprocess.run")
    def test_exits_on_json_decode_error(self, mock_run):
        """Exits with code 5 when wl show output is not valid JSON."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not valid json",
            stderr="",
        )
        with self.assertRaises(ExitCaptured) as ctx:
            oe._fetch_issue_stage("TEST-123")
        self.assertEqual(ctx.exception.code, 5)


class TestComputeTshirt(unittest.TestCase):
    """Tests for _compute_tshirt()."""

    def test_returns_mapped_label_for_xs(self):
        """Low recommended hours maps to 'Extra Small'."""
        result = oe._compute_tshirt(3.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "Extra Small")

    def test_returns_mapped_label_for_small(self):
        """Moderate recommended hours maps to 'Small'."""
        result = oe._compute_tshirt(10.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "Small")

    def test_returns_mapped_label_for_medium(self):
        """Higher recommended hours maps to 'Medium'."""
        result = oe._compute_tshirt(50.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "Medium")

    def test_returns_mapped_label_for_large(self):
        """Large recommended hours maps to 'Large'."""
        result = oe._compute_tshirt(120.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "Large")

    def test_returns_mapped_label_for_xl(self):
        """Extra large recommended hours maps to 'Extra Large'."""
        result = oe._compute_tshirt(500.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "Extra Large")

    def test_custom_thresholds(self):
        """Uses custom thresholds when provided."""
        custom = {"Small": {"min": 0, "max": 20}, "Large": {"min": 20, "max": None}}
        result = oe._compute_tshirt(10.0, custom)
        self.assertEqual(result, "Small")
        result = oe._compute_tshirt(30.0, custom)
        self.assertEqual(result, "Large")

    def test_handles_unknown_threshold_key(self):
        """Passes through unknown threshold keys that TSHIRT_MAP doesn't have."""
        custom = {"Tiny": {"min": 0, "max": None}}
        result = oe._compute_tshirt(5.0, custom)
        self.assertEqual(result, "Tiny")

    def test_zero_hours(self):
        """Zero hours maps to Extra Small."""
        result = oe._compute_tshirt(0.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "Extra Small")


class TestComputeRisk(unittest.TestCase):
    """Tests for _compute_risk()."""

    def test_with_parent_and_children(self):
        """Computes risk dict with parent and children data."""
        data = {
            "parent": {"probability": 3.0, "impact": 4.0},
            "children": [
                {"id": "C1", "title": "Child 1", "probability": 2.0, "impact": 3.0},
                {"id": "C2", "title": "Child 2", "probability": 1.0, "impact": 5.0},
            ],
            "certainty": 100,
        }
        result = oe._compute_risk(data, 100.0)
        self.assertIn("probability", result)
        self.assertIn("impact", result)
        self.assertIn("score", result)
        self.assertIn("level", result)
        self.assertIn("top_drivers", result)
        self.assertIn("mitigations", result)
        self.assertEqual(len(result["mitigations"]), 3)

    def test_with_empty_children(self):
        """Computes risk with empty children list."""
        data = {
            "parent": {"probability": 2.0, "impact": 3.0},
            "children": [],
            "certainty": 100,
        }
        result = oe._compute_risk(data, 100.0)
        self.assertIsInstance(result["probability"], (int, float))
        self.assertEqual(result["top_drivers"], [])

    def test_with_no_parent_data(self):
        """Computes risk with no parent data."""
        data = {
            "children": [{"id": "C1", "probability": 4.0, "impact": 4.0}],
            "certainty": 100,
        }
        result = oe._compute_risk(data, 100.0)
        # Parent defaults to probability 0, impact 0, so max prob comes from children
        self.assertEqual(result["probability"], min(5, 4.0 * 1.0))

    def test_with_no_children_and_no_parent(self):
        """Returns zero risk when no parent or children data."""
        data = {"certainty": 100}
        result = oe._compute_risk(data, 100.0)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["level"], "Low")

    def test_certainty_factor_reduces_risk_at_low_certainty(self):
        """Low certainty increases the risk aggregation factor."""
        data = {
            "parent": {"probability": 3.0, "impact": 4.0},
            "children": [],
            "certainty": 50,
        }
        result_low = oe._compute_risk(data, 50.0)
        result_high = oe._compute_risk(data, 100.0)
        # Lower certainty should produce higher or equal risk
        self.assertGreaterEqual(result_low["score"], result_high["score"])

    def test_mitigations_are_present(self):
        """Risk dict includes three standard mitigations."""
        result = oe._compute_risk({}, 100.0)
        self.assertEqual(
            result["mitigations"],
            [
                "Add targeted tests and integration checks",
                "Lock dependencies and add compatibility tests",
                "Schedule extra review for risky components",
            ],
        )

    def test_top_drivers_uses_title_or_id(self):
        """Top drivers uses title when available, falls back to id."""
        data = {
            "children": [
                {"id": "C1", "title": "Complex integration", "probability": 5.0, "impact": 5.0},
                {"id": "C2", "title": "", "probability": 4.0, "impact": 4.0},
            ],
            "certainty": 100,
        }
        result = oe._compute_risk(data, 100.0)
        self.assertIn("Complex integration", result["top_drivers"])
        # C2 has empty title, falls back to id
        self.assertIn("C2", result["top_drivers"])


class TestUpdateWorkItem(unittest.TestCase):
    """Tests for _update_work_item()."""

    @patch("subprocess.run")
    def test_successful_update(self, mock_run):
        """Returns success dict when wl update succeeds."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"updated": true}', stderr=""
        )
        result = oe._update_work_item("TEST-123", "Small", "Medium")
        self.assertTrue(result["success"])
        self.assertEqual(result["returncode"], 0)
        self.assertIn("stdout", result)

    @patch("subprocess.run")
    def test_failed_update(self, mock_run):
        """Returns failure dict when wl update returns non-zero."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="error occurred"
        )
        result = oe._update_work_item("TEST-123", "Small", "Medium")
        self.assertFalse(result["success"])
        self.assertEqual(result["returncode"], 1)

    @patch("subprocess.run", side_effect=Exception("command not found"))
    def test_exception_during_update(self, mock_run):
        """Returns failure dict when subprocess raises."""
        result = oe._update_work_item("TEST-123", "Small", "Medium")
        self.assertFalse(result["success"])
        self.assertIn("error", result)

    def test_effort_and_risk_passed_correctly(self):
        """Verifies the update command includes effort and risk flags."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="{}", stderr=""
            )
            oe._update_work_item("TEST-123", "Extra Large", "Severe")
            call_args = mock_run.call_args[0][0]
            self.assertIn("--effort", call_args)
            self.assertIn("Extra Large", call_args)
            self.assertIn("--risk", call_args)
            self.assertIn("Severe", call_args)
            self.assertIn("TEST-123", call_args)


class TestRenderHumanText(unittest.TestCase):
    """Tests for _render_human_text()."""

    def _make_final(self):
        return {
            "effort": {"unit": "hours", "tshirt": "Small", "o": 5.0, "m": 10.0, "p": 20.0},
            "risk": {"probability": 2.0, "impact": 3.0, "score": 6, "level": "Medium"},
            "confidence_percent": 85,
            "assumptions": ["Test assumption"],
            "unknowns": ["Test unknown"],
        }

    @patch("subprocess.run")
    def test_successful_render(self, mock_run):
        """Returns rendered text on successful subprocess call."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="# Effort and Risk Report\n\nSome narrative text",
            stderr="",
        )
        data = {"items": [], "children": []}
        final = self._make_final()
        result = oe._render_human_text(data, final)

        self.assertEqual(result, "# Effort and Risk Report\n\nSome narrative text")
        self.assertEqual(final["human_text"], "# Effort and Risk Report\n\nSome narrative text")
        self.assertEqual(final["human_render_rc"], 0)

    @patch("subprocess.run")
    def test_renders_with_wbs_data(self, mock_run):
        """Passes wbs_items and wbs_children in the sanitized object."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Narrative with WBS", stderr=""
        )
        data = {
            "items": [{"id": "I1", "title": "Task A", "o": 2, "m": 4, "p": 6}],
            "children": [{"id": "C1", "title": "Child 1"}],
        }
        final = self._make_final()
        oe._render_human_text(data, final)

        # Verify sanitized object passed to subprocess
        call_input = mock_run.call_args[1]["input"]
        sanitized = json.loads(call_input)
        self.assertIn("wbs_items", sanitized)
        self.assertIn("wbs_children", sanitized)
        self.assertEqual(len(sanitized["wbs_items"]), 1)
        self.assertEqual(len(sanitized["wbs_children"]), 1)

    @patch("subprocess.run")
    def test_empty_output(self, mock_run):
        """Returns empty string when subprocess produces no output."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        data = {}
        final = self._make_final()
        result = oe._render_human_text(data, final)
        self.assertEqual(result, "")
        self.assertEqual(final["human_text"], "")

    @patch("subprocess.run", side_effect=Exception("render crashed"))
    def test_exception_during_render(self, mock_run):
        """Returns empty string and sets error fields on exception."""
        data = {}
        final = self._make_final()
        result = oe._render_human_text(data, final)
        self.assertEqual(result, "")
        self.assertEqual(final["human_render_rc"], -1)
        self.assertIn("render crashed", final["human_render_stderr"])


class TestPostComment(unittest.TestCase):
    """Tests for _post_comment()."""

    @patch("subprocess.run")
    def test_successful_post(self, mock_run):
        """Returns success dict when wl comment add succeeds."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"success": true}', stderr="",
        )
        result = oe._post_comment("TEST-123", "Some comment text")
        self.assertTrue(result["success"])
        self.assertEqual(result["returncode"], 0)

    @patch("subprocess.run")
    def test_failed_post(self, mock_run):
        """Returns failure dict when wl comment add fails."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="error",
        )
        result = oe._post_comment("TEST-123", "Some text")
        self.assertFalse(result["success"])
        self.assertEqual(result["returncode"], 1)

    @patch("subprocess.run", side_effect=Exception("post crashed"))
    def test_exception_during_post(self, mock_run):
        """Returns failure dict on exception."""
        result = oe._post_comment("TEST-123", "Some text")
        self.assertFalse(result["success"])
        self.assertIn("error", result)

    def test_comment_command_uses_author_flag(self):
        """Verifies comment add uses --author flag."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="{}", stderr=""
            )
            oe._post_comment("TEST-123", "Body text")
            call_args = mock_run.call_args[0][0]
            self.assertIn("--author", call_args)
            self.assertIn("effort_and_risk_skill", call_args)
            self.assertIn("--comment", call_args)
            self.assertIn("Body text", call_args)


class TestMainRefactoredBehavior(unittest.TestCase):
    """Integration-level tests verifying the refactored main() behavior matches the original.

    These tests mock all subprocess and file I/O to verify that the refactored
    main() produces the expected output structure and handles errors identically.
    """

    def setUp(self):
        # Patch sys.exit to prevent actual exits and sys.stdin to provide input
        self.exit_patcher = patch.object(oe, "sys")
        self.mock_sys = self.exit_patcher.start()
        self.mock_sys.exit.side_effect = _mock_exit
        self.addCleanup(self.exit_patcher.stop)

    def _run_main(self, input_data, mock_run):
        """Helper: patch json.load and sys.stdout, then run main()."""
        buf = io.StringIO()
        with patch("json.load", return_value=input_data), \
             patch("sys.stdout", buf):
            try:
                oe.main()
            except ExitCaptured:
                pass
        return buf.getvalue()

    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps({
        "thresholds": DEFAULT_THRESHOLDS
    }))
    @patch("subprocess.run")
    def test_main_produces_expected_output_structure(self, mock_run, mock_file):
        """The refactored main() produces the full expected output dict structure."""
        # Simulate all subprocess calls succeeding
        def subprocess_side_effect(cmd, *args, **kwargs):
            if "show" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps({"workItem": {"stage": "plan_complete"}}),
                    stderr="",
                )
            elif "update" in cmd:
                return MagicMock(
                    returncode=0, stdout='{"updated": true}', stderr=""
                )
            elif "comment" in cmd:
                return MagicMock(
                    returncode=0, stdout='{"success": true}', stderr=""
                )
            elif "python3" in cmd or "json_to_human" in str(cmd):
                return MagicMock(
                    returncode=0,
                    stdout="# Effort and Risk Report\n\nNarrative body",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = subprocess_side_effect

        input_data = {
            "issue_id": "TEST-123",
            "o": 5.0,
            "m": 10.0,
            "p": 20.0,
            "overheads": {"coordination": 2.0, "review": 1.0},
            "parent": {"probability": 3.0, "impact": 4.0},
            "children": [
                {"id": "C1", "title": "Child 1", "probability": 2.0, "impact": 3.0},
            ],
            "certainty": 100,
            "assumptions": ["Stable API"],
            "unknowns": ["Edge cases"],
        }

        output_str = self._run_main(input_data, mock_run)
        self.assertTrue(output_str, "main() should produce output")
        output = json.loads(output_str)

        # Check expected structure
        self.assertIn("effort", output)
        self.assertIn("risk", output)
        self.assertIn("confidence_percent", output)
        self.assertIn("assumptions", output)
        self.assertIn("unknowns", output)
        self.assertIn("input_stage", output)
        self.assertIn("original_certainty", output)
        self.assertIn("adjusted_certainty", output)
        self.assertIn("update_result", output)
        self.assertIn("human_text", output)
        self.assertIn("human_render_rc", output)
        self.assertIn("comment_result", output)

        # Verify effort values
        self.assertEqual(output["effort"]["o"], 5.0)
        self.assertEqual(output["effort"]["m"], 10.0)
        self.assertEqual(output["effort"]["p"], 20.0)
        self.assertEqual(output["input_stage"], "plan_complete")

    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps({
        "thresholds": DEFAULT_THRESHOLDS
    }))
    @patch("subprocess.run")
    def test_main_missing_issue_id_exits(self, mock_run, mock_file):
        """main() exits with code 2 when issue_id is missing."""
        input_data = {"o": 1.0, "m": 2.0, "p": 3.0}

        with patch("json.load", return_value=input_data):
            with self.assertRaises(ExitCaptured) as ctx:
                oe.main()
        self.assertEqual(ctx.exception.code, 2)

    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps({
        "thresholds": DEFAULT_THRESHOLDS
    }))
    @patch("subprocess.run")
    def test_main_shows_intake_stage_scales_certainty(self, mock_run, mock_file):
        """main() scales certainty down when stage is intake_complete."""
        def subprocess_side_effect(cmd, *args, **kwargs):
            if "show" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps({"workItem": {"stage": "intake_complete"}}),
                    stderr="",
                )
            elif "update" in cmd:
                return MagicMock(returncode=0, stdout="{}", stderr="")
            elif "python3" in cmd or "json_to_human" in str(cmd):
                return MagicMock(returncode=0, stdout="Narrative", stderr="")
            elif "comment" in cmd:
                return MagicMock(returncode=0, stdout="{}", stderr="")
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = subprocess_side_effect

        input_data = {
            "issue_id": "TEST-123",
            "o": 2.0, "m": 5.0, "p": 10.0,
            "certainty": 80,
        }

        output_str = self._run_main(input_data, mock_run)
        self.assertTrue(output_str, "main() should produce output")
        output = json.loads(output_str)

        self.assertEqual(output["original_certainty"], 80.0)
        self.assertEqual(output["adjusted_certainty"], 48.0)  # 80 * 0.6
        self.assertEqual(output["input_stage"], "intake_complete")

    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps({
        "thresholds": DEFAULT_THRESHOLDS
    }))
    @patch("subprocess.run")
    def test_main_empty_rendered_text_handled(self, mock_run, mock_file):
        """main() handles empty human text render without crashing."""
        def subprocess_side_effect(cmd, *args, **kwargs):
            if "show" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps({"workItem": {"stage": "plan_complete"}}),
                    stderr="",
                )
            elif "update" in cmd:
                return MagicMock(returncode=0, stdout="{}", stderr="")
            elif "python3" in cmd or "json_to_human" in str(cmd):
                return MagicMock(returncode=0, stdout="", stderr="render failed")
            elif "comment" in cmd:
                return MagicMock(returncode=0, stdout="{}", stderr="")
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = subprocess_side_effect

        input_data = {"issue_id": "TEST-123", "o": 1.0, "m": 2.0, "p": 3.0}

        output_str = self._run_main(input_data, mock_run)
        self.assertTrue(output_str, "main() should produce output")
        output = json.loads(output_str)

        self.assertIn("comment_result", output)
        self.assertFalse(output["comment_result"]["success"])
        self.assertIn("empty rendered human text", output["comment_result"]["error"])

    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps({
        "thresholds": DEFAULT_THRESHOLDS
    }))
    @patch("subprocess.run")
    def test_main_handles_update_failure(self, mock_run, mock_file):
        """main() captures wl update failure without crashing."""
        def subprocess_side_effect(cmd, *args, **kwargs):
            if "show" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps({"workItem": {"stage": "plan_complete"}}),
                    stderr="",
                )
            elif "python3" in cmd or "json_to_human" in str(cmd):
                return MagicMock(returncode=0, stdout="Narrative", stderr="")
            elif "comment" in cmd:
                return MagicMock(returncode=0, stdout="{}", stderr="")
            elif "update" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="update error")
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = subprocess_side_effect

        input_data = {"issue_id": "TEST-123", "o": 1.0, "m": 2.0, "p": 3.0}

        output_str = self._run_main(input_data, mock_run)
        self.assertTrue(output_str, "main() should produce output")
        output = json.loads(output_str)

        self.assertIn("update_result", output)
        self.assertFalse(output["update_result"]["success"])
        self.assertEqual(output["update_result"]["returncode"], 1)

    @patch("builtins.open", new_callable=mock_open, read_data=json.dumps({
        "thresholds": DEFAULT_THRESHOLDS
    }))
    @patch("subprocess.run")
    def test_main_post_comment_uses_combined_text(self, mock_run, mock_file):
        """main() posts combined human text + JSON block as comment."""
        def subprocess_side_effect(cmd, *args, **kwargs):
            if "show" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps({"workItem": {"stage": "plan_complete"}}),
                    stderr="",
                )
            elif "update" in cmd:
                return MagicMock(returncode=0, stdout="{}", stderr="")
            elif "python3" in cmd or "json_to_human" in str(cmd):
                return MagicMock(returncode=0, stdout="Narrative body", stderr="")
            elif "comment" in cmd:
                return MagicMock(returncode=0, stdout="{}", stderr="")
            return MagicMock(returncode=0, stdout="{}", stderr="")

        mock_run.side_effect = subprocess_side_effect

        input_data = {"issue_id": "TEST-123", "o": 1.0, "m": 2.0, "p": 3.0}

        self._run_main(input_data, mock_run)

        # Find the comment add call
        comment_calls = [
            call for call in mock_run.call_args_list
            if "comment" in call[0][0] and "add" in call[0][0]
        ]
        self.assertEqual(len(comment_calls), 1)
        comment_args = comment_calls[0][0][0]
        self.assertIn("--comment", comment_args)
        # The combined text should contain both narrative and JSON block
        comment_idx = comment_args.index("--comment") + 1
        combined = comment_args[comment_idx]
        self.assertIn("Narrative body", combined)
        self.assertIn("```json", combined)
        self.assertIn("```", combined)


if __name__ == "__main__":
    unittest.main()
