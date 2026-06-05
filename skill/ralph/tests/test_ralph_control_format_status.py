"""Tests for ralph_control.format_status markdown output consistency.

These tests verify that the structured markdown output from format_status
has a consistent section ordering and structure, ensuring that repeated
calls with the same input produce identical output.
"""

import pytest
from skill.ralph.scripts.ralph_control import format_status


class TestFormatStatusStructure:
    """Verify that format_status produces consistent, structured markdown."""

    def test_header_section_always_present(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
        }
        output = format_status(snapshot)
        lines = output.split("\n")
        # Header: "# Ralph Status"
        assert lines[0] == "# Ralph Status"
        # Second line: bold state, pid, target
        assert "**State**" in lines[2]
        assert "**PID**" in lines[2]
        assert "**Target**" in lines[2]

    def test_active_task_shown_when_present(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "active_task": "SA-002",
        }
        output = format_status(snapshot)
        assert "**Active Task**: `SA-002`" in output

    def test_active_task_absent_when_none(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
        }
        output = format_status(snapshot)
        assert "Active Task" not in output

    def test_status_counts_table_format(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "status_counts": {"open": 5, "completed": 3},
            "status_deltas": {"open": -1, "completed": +1},
        }
        output = format_status(snapshot)
        assert "## Status Counts" in output
        assert "| Status | Count | Delta |" in output
        assert "| `completed` | 3 | +1 |" in output
        assert "| `open` | 5 | -1 |" in output

    def test_status_counts_empty_when_no_counts(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
        }
        output = format_status(snapshot)
        assert "Status Counts" not in output

    def test_recent_activity_shows_last_20_lines(self):
        recent = [f"Log line {i}" for i in range(25)]
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": recent,
        }
        output = format_status(snapshot)
        assert "## Recent Activity" in output
        # Should only show last 20 lines
        line_count = sum(1 for line in output.split("\n") if line.startswith("- Log line"))
        assert line_count == 20
        # Should start from line 5 (25 - 20 = 5)
        assert "- Log line 5" in output
        assert "- Log line 4" not in output

    def test_recent_activity_absent_when_empty(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
        }
        output = format_status(snapshot)
        assert "Recent Activity" not in output

    def test_exit_code_shown_when_present(self):
        snapshot = {
            "state": "stopped",
            "pid": 1234,
            "target_id": "SA-001",
            "exit_code": 0,
        }
        output = format_status(snapshot)
        assert "**Exit Code**: `0`" in output

    def test_exit_code_absent_when_none(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
        }
        output = format_status(snapshot)
        assert "Exit Code" not in output

    def test_final_summary_shown_when_present(self):
        snapshot = {
            "state": "stopped",
            "pid": 1234,
            "target_id": "SA-001",
            "final_summary": {"status": "success", "summary": "All tasks completed"},
        }
        output = format_status(snapshot)
        assert "**Final Status**: `success`" in output
        assert "**Summary**: All tasks completed" in output

    def test_final_summary_absent_when_none(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
        }
        output = format_status(snapshot)
        assert "Final Status" not in output


class TestFormatStatusConsistency:
    """Verify that repeated calls produce identical output."""

    def test_identical_input_produces_identical_output(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "active_task": "SA-002",
            "status_counts": {"open": 5, "completed": 3},
            "status_deltas": {"open": -1, "completed": +1},
            "recent_activity": ["Log line 1", "Log line 2"],
            "exit_code": 0,
            "final_summary": {"status": "success", "summary": "Done"},
        }
        output1 = format_status(snapshot)
        output2 = format_status(snapshot)
        assert output1 == output2

    def test_output_structure_invariant_across_calls(self):
        """Each call must start with the same section order."""
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "active_task": "SA-002",
            "status_counts": {"open": 2, "completed": 1},
            "status_deltas": {"open": 0, "completed": 1},
            "recent_activity": ["Activity 1"],
        }
        outputs = [format_status(snapshot) for _ in range(10)]
        assert all(o == outputs[0] for o in outputs)

    def test_section_order_consistent(self):
        """Verify the canonical section order: Header, Active Task, Status Counts, Recent Activity, Exit Code, Final Summary."""
        snapshot = {
            "state": "stopped",
            "pid": 1234,
            "target_id": "SA-001",
            "active_task": "SA-002",
            "status_counts": {"open": 1},
            "status_deltas": {"open": 0},
            "recent_activity": ["Activity 1"],
            "exit_code": 0,
            "final_summary": {"status": "success", "summary": "Done"},
        }
        output = format_status(snapshot)
        lines = output.split("\n")
        # Find section markers
        header_idx = next(i for i, l in enumerate(lines) if l == "# Ralph Status")
        active_idx = next(i for i, l in enumerate(lines) if "**Active Task**" in l)
        counts_idx = next(i for i, l in enumerate(lines) if l == "## Status Counts")
        activity_idx = next(i for i, l in enumerate(lines) if l == "## Recent Activity")
        exit_idx = next(i for i, l in enumerate(lines) if "**Exit Code**" in l)
        final_idx = next(i for i, l in enumerate(lines) if "**Final Status**" in l)
        # Verify order
        assert header_idx < active_idx < counts_idx < activity_idx < exit_idx < final_idx


class TestFormatStatusEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_snapshot(self):
        snapshot: dict = {}
        output = format_status(snapshot)
        assert "# Ralph Status" in output

    def test_unicode_in_activity(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": ["Test avec français", "日本語ログ"],
        }
        output = format_status(snapshot)
        assert "français" in output
        assert "日本語ログ" in output

    def test_many_status_counts_sorted(self):
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "status_counts": {"zulu": 1, "alpha": 2, "bravo": 3},
            "status_deltas": {"zulu": 0, "alpha": 1, "bravo": 0},
        }
        output = format_status(snapshot)
        # Verify alphabetical ordering in table
        alpha_pos = output.find("| `alpha`")
        bravo_pos = output.find("| `bravo`")
        zulu_pos = output.find("| `zulu`")
        assert alpha_pos < bravo_pos < zulu_pos
