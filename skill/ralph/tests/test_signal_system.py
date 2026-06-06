"""Unit tests for the Ralph signal system: EventType, SignalWriter, and config-based path resolution.

These tests are written first (TDD) and define the expected API before the
implementation is built.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.signal_system import EventType, SignalWriter, resolve_signal_path


# ── EventType constants ──────────────────────────────────────────────────────


class TestEventType:
    """Verify all 7 event types are defined and return expected string values."""

    def test_all_event_types_defined(self):
        """All 8 required event types exist as enum members."""
        expected = {
            "STATUS_TRANSITION",
            "PHASE_CHANGE",
            "ERROR",
            "MAX_ATTEMPTS",
            "CANCELLED",
            "COMPLETED",
            "STARTED",
            "PI_STARTED",
        }
        actual = set(EventType.__members__)
        assert actual == expected, f"Missing or extra event types: {expected ^ actual}"

    def test_event_type_values_are_strings(self):
        """Each EventType member has a non-empty string value."""
        for member in EventType:
            assert isinstance(member.value, str), f"{member} value is not a string"
            assert member.value, f"{member} value is empty"

    @pytest.mark.parametrize(
        "member, expected_value",
        [
            (EventType.STATUS_TRANSITION, "status_transition"),
            (EventType.PHASE_CHANGE, "phase_change"),
            (EventType.ERROR, "error"),
            (EventType.MAX_ATTEMPTS, "max_attempts"),
            (EventType.CANCELLED, "cancelled"),
            (EventType.COMPLETED, "completed"),
            (EventType.STARTED, "started"),
            (EventType.PI_STARTED, "pi_started"),
        ],
    )
    def test_event_type_value(self, member: EventType, expected_value: str):
        """Each event type maps to its expected string value."""
        assert member.value == expected_value

    def test_event_type_is_json_serializable(self):
        """EventType values can be serialised to JSON."""
        payload = {"event_type": EventType.STARTED.value}
        serialized = json.dumps(payload)
        deserialized = json.loads(serialized)
        assert deserialized["event_type"] == "started"


# ── SignalWriter ──────────────────────────────────────────────────────────────


class TestSignalWriter:
    """Verify SignalWriter writes valid JSON signal files."""

    def test_write_event_creates_file(self, tmp_path: Path):
        """SignalWriter creates a signal file at the configured path."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.STARTED)
        assert signal_path.exists(), "Signal file was not created"

    def test_write_event_contains_valid_json(self, tmp_path: Path):
        """The written signal file contains valid JSON."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.COMPLETED)
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), "Signal file content is not a JSON object"

    def test_write_event_contains_required_fields(self, tmp_path: Path):
        """The JSON payload includes event_type, timestamp, and work_item_ids."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.ERROR, work_item_ids=["SA-123"])
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        assert "event_type" in data, "Missing event_type field"
        assert "timestamp" in data, "Missing timestamp field"
        assert "work_item_ids" in data, "Missing work_item_ids field"

    def test_write_event_timestamp_is_iso8601(self, tmp_path: Path):
        """The timestamp field is an ISO8601-formatted string."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.PHASE_CHANGE)
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        ts = data["timestamp"]
        assert isinstance(ts, str), f"timestamp is not a string: {type(ts)}"
        # ISO8601 contains at least date and time separators
        assert "T" in ts, f"timestamp does not look ISO8601: {ts}"

    def test_write_event_correct_type(self, tmp_path: Path):
        """The event_type field matches the supplied EventType."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.STATUS_TRANSITION)
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        assert data["event_type"] == "status_transition"

    def test_write_event_with_work_item_ids(self, tmp_path: Path):
        """work_item_ids contains the supplied IDs."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        ids = ["SA-100", "SA-200"]
        writer.write_event(EventType.CANCELLED, work_item_ids=ids)
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        assert data["work_item_ids"] == ids

    def test_write_event_without_work_item_ids(self, tmp_path: Path):
        """work_item_ids is an empty list when no IDs are supplied."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.STARTED)
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        assert data["work_item_ids"] == []

    def test_write_event_overwrites_previous_file(self, tmp_path: Path):
        """Writing a new event overwrites (does not append to) the signal file."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.STARTED, work_item_ids=["SA-001"])
        writer.write_event(EventType.COMPLETED, work_item_ids=["SA-999"])
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        # The file should contain only the SECOND event, not both
        assert data["event_type"] == "completed"
        assert data["work_item_ids"] == ["SA-999"]

    def test_write_event_preserves_previous_file_when_no_overwrite(self, tmp_path: Path):
        """If the file already exists, it is overwritten (not appended) — so only
        one JSON object exists in the file."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.STARTED)
        writer.write_event(EventType.COMPLETED)
        content = signal_path.read_text(encoding="utf-8").strip()
        # Ensure the content is a single JSON object, not a concatenation
        assert content.count("{") == 1, "File appears to contain multiple JSON objects (append rather than overwrite)"
        assert content.count("}") == 1, "File appears to contain multiple JSON objects (append rather than overwrite)"

    def test_write_event_returns_path(self, tmp_path: Path):
        """write_event returns the Path to the written signal file."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        result = writer.write_event(EventType.STARTED)
        assert result == signal_path

    def test_write_event_custom_timestamp(self, tmp_path: Path):
        """A caller-supplied timestamp is used instead of auto-generated one."""
        signal_path = tmp_path / "event.pending"
        writer = SignalWriter(signal_path)
        custom_ts = "2026-06-05T12:00:00.000Z"
        writer.write_event(EventType.STARTED, timestamp=custom_ts)
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        assert data["timestamp"] == custom_ts


# ── Config-based path resolution ────────────────────────────────────────────


class TestResolveSignalPath:
    """Verify signal file path resolution from config with fallback."""

    def test_default_path(self):
        """When no signal config is provided, returns the default path."""
        config: dict[str, object] = {}
        path = resolve_signal_path(config)
        assert path == Path(".ralph") / "event.pending"

    def test_config_provides_signal_file_path(self):
        """When signal.file_path is in the config, it is returned."""
        config = {"signal": {"file_path": "/tmp/my-signal.json"}}
        path = resolve_signal_path(config)
        assert path == Path("/tmp/my-signal.json")

    def test_config_empty_signal_section(self):
        """An empty 'signal' section uses the default path."""
        config: dict[str, object] = {"signal": {}}
        path = resolve_signal_path(config)
        assert path == Path(".ralph") / "event.pending"

    def test_config_signal_is_none(self):
        """When signal is explicitly set to None, the default path is used."""
        config = {"signal": None}
        path = resolve_signal_path(config)
        assert path == Path(".ralph") / "event.pending"

    def test_config_signal_file_path_is_none(self):
        """When signal.file_path is None, the default path is used."""
        config = {"signal": {"file_path": None}}
        path = resolve_signal_path(config)
        assert path == Path(".ralph") / "event.pending"

    def test_config_signal_file_path_empty_string(self):
        """When signal.file_path is an empty string, the default path is used."""
        config = {"signal": {"file_path": ""}}
        path = resolve_signal_path(config)
        assert path == Path(".ralph") / "event.pending"

    def test_config_only_signal_path_provided(self):
        """Only the signal.file_path key is used; other keys are ignored."""
        config = {"signal": {"file_path": ".ralph/custom_event.json", "webhook_url": "https://discord.com/api/webhooks/xxx"}}
        path = resolve_signal_path(config)
        assert path == Path(".ralph/custom_event.json")

    def test_ralph_json_full_config(self):
        """Integration-style: a full .ralph.json structure resolves correctly."""
        config = {"signal": {"file_path": ".ralph/my-events.json"}}
        path = resolve_signal_path(config)
        assert path == Path(".ralph/my-events.json")


# ── SignalWriter with config integration ────────────────────────────────────


class TestSignalWriterWithConfig:
    """Verify SignalWriter works end-to-end when constructed with a config-derived path."""

    def test_signal_writer_with_config_path(self, tmp_path: Path):
        """A SignalWriter constructed from a resolved config path works correctly."""
        signal_path = tmp_path / ".ralph" / "events.json"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.STARTED, work_item_ids=["SA-X"])
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        assert data["event_type"] == "started"
        assert data["work_item_ids"] == ["SA-X"]

    def test_signal_writer_creates_parent_dirs(self, tmp_path: Path):
        """Parent directories are created if they don't exist."""
        signal_path = tmp_path / "deep" / "nested" / "signal.json"
        writer = SignalWriter(signal_path)
        writer.write_event(EventType.COMPLETED)
        assert signal_path.exists()
