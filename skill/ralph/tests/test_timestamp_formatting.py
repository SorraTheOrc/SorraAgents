"""Tests for timestamp formatting in ralph status output.

These tests verify that JSON log entries with timestamp fields are
converted to human-readable format and that a summary sentence
indicating activity recency is appended to the Recent Activity section.
"""

import json
from datetime import datetime, timezone


from skill.ralph.scripts.ralph_control import (
    _format_log_line,
    _format_timestamp,
    _humanize_time_delta,
    format_status,
)


class TestFormatTimestamp:
    """Verify that _format_timestamp converts millisecond timestamps to HH:MM:SS."""

    def test_formats_current_time(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        result = _format_timestamp(now_ms)
        # Should not raise and should produce a time-like string
        assert result is not None
        parts = result.split(":")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_formats_epoch(self):
        result = _format_timestamp(0)
        assert result == "00:00:00"

    def test_formats_specific_timestamp(self):
        # 2024-01-01 12:30:45 UTC in milliseconds
        ts = int(datetime(2024, 1, 1, 12, 30, 45, tzinfo=timezone.utc).timestamp() * 1000)
        result = _format_timestamp(ts)
        assert result == "12:30:45"

    def test_handles_none(self):
        result = _format_timestamp(None)
        assert result == ""


class TestHumanizeTimeDelta:
    """Verify that _humanize_time_delta produces human-readable relative times."""

    def test_zero_seconds(self):
        result = _humanize_time_delta(0)
        assert result == "just now"

    def test_seconds(self):
        result = _humanize_time_delta(30)
        assert result == "30 seconds ago"

    def test_one_minute(self):
        result = _humanize_time_delta(60)
        assert result == "1 minute ago"

    def test_minutes(self):
        result = _humanize_time_delta(300)
        assert result == "5 minutes ago"

    def test_one_hour(self):
        result = _humanize_time_delta(3600)
        assert result == "1 hour ago"

    def test_hours(self):
        result = _humanize_time_delta(7200)
        assert result == "2 hours ago"

    def test_large_delta(self):
        result = _humanize_time_delta(86400 * 2 + 3600)
        assert result == "2 days ago"

    def test_handles_negative(self):
        # Future timestamps should not produce negative values
        result = _humanize_time_delta(-100)
        assert result == "just now"

    def test_handles_none(self):
        result = _humanize_time_delta(None)
        assert result == ""

    def test_just_under_minute(self):
        result = _humanize_time_delta(59)
        assert result == "59 seconds ago"

    def test_just_over_hour(self):
        result = _humanize_time_delta(3661)
        assert result == "1 hour ago"

    def test_just_under_hour(self):
        result = _humanize_time_delta(3599)
        assert result == "59 minutes ago"

    def test_just_under_day(self):
        result = _humanize_time_delta(86399)
        assert result == "23 hours ago"


class TestFormatLogLine:
    """Verify that _format_log_line parses JSON and formats timestamps."""

    def test_plain_text_unchanged(self):
        """Plain text lines should be returned unchanged."""
        result = _format_log_line("child_focus parent=SA-001 child=SA-002")
        assert result == "child_focus parent=SA-001 child=SA-002"

    def test_json_with_timestamp(self):
        """JSON lines with a 'ts' field should have timestamp prepended."""
        ts = int(datetime(2024, 1, 1, 12, 30, 45, tzinfo=timezone.utc).timestamp() * 1000)
        entry = json.dumps({"ts": ts, "level": "INFO", "msg": "test message"})
        result = _format_log_line(entry)
        assert "12:30:45" in result
        assert "test message" in result

    def test_json_without_timestamp(self):
        """JSON lines without a 'ts' field should be returned unchanged."""
        entry = json.dumps({"level": "INFO", "msg": "no timestamp"})
        result = _format_log_line(entry)
        assert result == entry

    def test_invalid_json_unchanged(self):
        """Invalid JSON should be returned unchanged."""
        result = _format_log_line("not valid json {{{")
        assert result == "not valid json {{{"

    def test_empty_string(self):
        result = _format_log_line("")
        assert result == ""

    def test_json_with_message_field(self):
        """Lines with both ts and msg should show formatted timestamp + message."""
        ts = int(datetime(2024, 6, 15, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        entry = json.dumps({"ts": ts, "level": "INFO", "msg": "implementing work item SA-001"})
        result = _format_log_line(entry)
        assert "09:00:00" in result
        assert "implementing work item SA-001" in result


class TestFormatStatusWithTimestamps:
    """Verify that format_status includes timestamps and summary in Recent Activity."""

    def test_recent_activity_with_json_timestamps(self):
        """Recent activity with JSON entries should show formatted timestamps."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        one_min_ago_ms = now_ms - 60_000
        two_min_ago_ms = now_ms - 120_000

        entries = [
            json.dumps({"ts": two_min_ago_ms, "level": "INFO", "msg": "first activity"}),
            "plain text log entry",
            json.dumps({"ts": one_min_ago_ms, "level": "INFO", "msg": "second activity"}),
        ]
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": entries,
        }
        output = format_status(snapshot)
        assert "## Recent Activity" in output
        # Should contain formatted timestamps
        # (exact format depends on current time, but should have HH:MM:SS pattern)
        assert "first activity" in output
        assert "plain text log entry" in output
        assert "second activity" in output
        # Should have a summary line about activity recency
        assert "Last recorded activity" in output

    def test_recent_activity_plain_text_no_timestamps(self):
        """Recent activity with only plain text lines should still show summary."""
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": ["child_focus parent=SA-001 child=SA-002"],
        }
        output = format_status(snapshot)
        assert "## Recent Activity" in output
        assert "child_focus parent=SA-001 child=SA-002" in output
        # When no JSON timestamps are present, summary shows state but no recency
        assert "Ralph is running" in output
        assert "Last recorded activity" not in output

    def test_summary_mentions_running_state(self):
        """Summary should indicate Ralph is running."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        entry = json.dumps({"ts": now_ms, "level": "INFO", "msg": "test"})
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": [entry],
        }
        output = format_status(snapshot)
        assert "Ralph is running" in output

    def test_summary_mentions_stopped_state(self):
        """Summary should indicate Ralph is stopped."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        entry = json.dumps({"ts": now_ms, "level": "INFO", "msg": "test"})
        snapshot = {
            "state": "stopped",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": [entry],
        }
        output = format_status(snapshot)
        assert "Ralph has stopped" in output

    def test_summary_mentions_time_since_activity(self):
        """Summary should include time since last activity."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        five_min_ago_ms = now_ms - 300_000
        entry = json.dumps({"ts": five_min_ago_ms, "level": "INFO", "msg": "test"})
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": [entry],
        }
        output = format_status(snapshot)
        assert "5 minutes ago" in output

    def test_no_recent_activity(self):
        """When no recent activity exists, no summary about last activity should appear."""
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
        }
        output = format_status(snapshot)
        # No Recent Activity section at all
        assert "Recent Activity" not in output

    def test_backward_compatibility_plain_text(self):
        """Plain text log entries should remain unchanged."""
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": [
                "implementing work item SA-001",
                "completed audit phase",
                "child_focus parent=SA-001 child=SA-002",
            ],
        }
        output = format_status(snapshot)
        assert "implementing work item SA-001" in output
        assert "completed audit phase" in output
        assert "child_focus parent=SA-001 child=SA-002" in output


class TestFormatStatusConsistencyWithTimestamps:
    """Verify that format_status remains consistent with timestamp enhancements."""

    def test_section_order_preserved_with_timestamps(self):
        """Section order should be preserved: Header, Active Task, Status Counts, Recent Activity (with timestamps), Summary, Exit Code, Final Summary."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        entry = json.dumps({"ts": now_ms, "level": "INFO", "msg": "test"})
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "active_task": "SA-002",
            "status_counts": {"open": 1},
            "status_deltas": {"open": 0},
            "recent_activity": [entry],
        }
        output = format_status(snapshot)
        lines = output.split("\n")
        header_idx = next(i for i, line in enumerate(lines) if line == "# Ralph Status")
        active_idx = next(i for i, line in enumerate(lines) if "**Active Task**" in line)
        counts_idx = next(i for i, line in enumerate(lines) if line == "## Status Counts")
        activity_idx = next(i for i, line in enumerate(lines) if line == "## Recent Activity")
        # Activity recency line comes after recent activity lines
        activity_end_idx = next(i for i, line in enumerate(lines) if "Last recorded activity" in line)
        assert header_idx < active_idx < counts_idx < activity_idx < activity_end_idx

    def test_output_is_stable_across_repeated_calls(self):
        """Repeated calls with same snapshot should produce identical output."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        entry = json.dumps({"ts": now_ms, "level": "INFO", "msg": "test"})
        snapshot = {
            "state": "running",
            "pid": 1234,
            "target_id": "SA-001",
            "recent_activity": [entry],
        }
        outputs = [format_status(snapshot) for _ in range(5)]
        assert all(o == outputs[0] for o in outputs)
