"""Tests for _format_in_progress_items() parsing logic.

Covers acceptance criteria from SA-0MLZSJP7T0FR5F90:
  - Empty string returns []
  - 'No in-progress work items found' returns []
  - Error messages return []
  - Real SA- item lines are correctly extracted
  - _build_dry_run_report() produces idle messaging when items list is empty
"""

import pytest

from ampa.delegation import _format_in_progress_items, _build_dry_run_report


class TestFormatInProgressItems:
    """Unit tests for _format_in_progress_items()."""

    def test_empty_string_returns_empty_list(self):
        assert _format_in_progress_items("") == []

    def test_none_returns_empty_list(self):
        assert _format_in_progress_items(None) == []  # type: ignore[arg-type]

    def test_no_items_message_returns_empty_list(self):
        assert _format_in_progress_items("No in-progress work items found") == []

    def test_error_message_returns_empty_list(self):
        assert _format_in_progress_items("Error: connection refused") == []

    def test_whitespace_only_returns_empty_list(self):
        assert _format_in_progress_items("   \n  \n  ") == []

    def test_random_text_returns_empty_list(self):
        assert _format_in_progress_items("some random output text") == []

    def test_single_real_item(self):
        text = "- SA-ABC123 Fix the bug (status: in-progress)"
        result = _format_in_progress_items(text)
        assert len(result) == 1
        assert "SA-ABC123" in result[0]

    def test_multiple_real_items(self):
        text = "- SA-001 First item\n- SA-002 Second item\n"
        result = _format_in_progress_items(text)
        assert len(result) == 2
        assert "SA-001" in result[0]
        assert "SA-002" in result[1]

    def test_items_with_tree_characters(self):
        text = "├ - SA-001 First item\n└ - SA-002 Second item\n"
        result = _format_in_progress_items(text)
        assert len(result) == 2
        assert "SA-001" in result[0]
        assert "SA-002" in result[1]

    def test_mixed_lines_only_extracts_items(self):
        text = (
            "In-progress work items:\n"
            "- SA-001 First item\n"
            "Some other text\n"
            "- SA-002 Second item\n"
        )
        result = _format_in_progress_items(text)
        assert len(result) == 2
        assert "SA-001" in result[0]
        assert "SA-002" in result[1]

    def test_no_items_message_with_extra_whitespace(self):
        text = "  No in-progress work items found  \n"
        assert _format_in_progress_items(text) == []


class TestBuildDryRunReportIdleBranch:
    """Verify _build_dry_run_report() idle path when no in-progress items."""

    def test_idle_report_when_empty_in_progress(self):
        report = _build_dry_run_report(
            in_progress_output="",
            candidates=[],
            top_candidate=None,
        )
        assert "Agents are currently busy" not in report
        assert "idle" in report.lower() or "(none)" in report.lower()

    def test_idle_report_with_no_items_message(self):
        report = _build_dry_run_report(
            in_progress_output="No in-progress work items found",
            candidates=[],
            top_candidate=None,
        )
        assert "Agents are currently busy" not in report

    def test_busy_report_with_real_items(self):
        report = _build_dry_run_report(
            in_progress_output="- SA-001 Working on something",
            candidates=[],
            top_candidate=None,
        )
        assert "Agents are currently busy" in report
        assert "SA-001" in report

    def test_idle_report_includes_candidates(self):
        candidates = [
            {"id": "SA-42", "title": "Do thing", "status": "open", "priority": "high"},
        ]
        report = _build_dry_run_report(
            in_progress_output="No in-progress work items found",
            candidates=candidates,
            top_candidate=candidates[0],
        )
        assert "Agents are currently busy" not in report
        assert "SA-42" in report
        assert "Do thing" in report
