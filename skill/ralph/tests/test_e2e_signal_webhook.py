"""End-to-end verification tests for the Ralph signal + webhook pipeline.

Tests cover event propagation through SignalWriter and WebhookNotifier
within a mock RalphLoop context.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import EventType, RalphLoop


class SignalValidationLoop(RalphLoop):
    """A RalphLoop subclass that exposes _notify_event for testing.

    Uses a temp directory for signal files and allows injecting a mock
    webhook URL.
    """

    def __init__(self, signal_dir: Path, webhook_url: str | None = None):
        super().__init__(
            pi_bin="pi",
            stream=False,
            signal_file_path=str(signal_dir / "event.pending"),
            webhook_url=webhook_url,
        )


# ── Signal file coverage for all 8 event types ─────────────────────────────


class TestAllEventTypesProduceSignalFiles:
    """Verify each of the 8 event types produces a corresponding signal file."""

    @pytest.mark.parametrize(
        "event_type",
        [
            EventType.STARTED,
            EventType.COMPLETED,
            EventType.CANCELLED,
            EventType.ERROR,
            EventType.MAX_ATTEMPTS,
            EventType.PHASE_CHANGE,
            EventType.STATUS_TRANSITION,
            EventType.PI_STARTED,
        ],
    )
    def test_event_produces_signal_file(self, tmp_path: Path, event_type: EventType):
        """Each event type generates a signal file when _notify_event is called."""
        loop = SignalValidationLoop(tmp_path)
        ids = ["SA-001"]
        loop._notify_event(event_type, work_item_ids=ids)

        signal_file = tmp_path / "event.pending"
        assert signal_file.exists(), f"No signal file for {event_type.value}"

        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == event_type.value
        assert data["work_item_ids"] == ids
        assert "timestamp" in data

    def test_all_events_together(self, tmp_path: Path):
        """All 8 events can be written sequentially, each overwriting the previous."""
        loop = SignalValidationLoop(tmp_path)
        all_types = [
            EventType.STARTED,
            EventType.PI_STARTED,
            EventType.PHASE_CHANGE,
            EventType.STATUS_TRANSITION,
            EventType.ERROR,
            EventType.MAX_ATTEMPTS,
            EventType.CANCELLED,
            EventType.COMPLETED,
        ]

        for i, et in enumerate(all_types):
            loop._notify_event(et, work_item_ids=[f"SA-{i:03d}"])

        # Only the last event should remain
        signal_file = tmp_path / "event.pending"
        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == "completed"
        assert data["work_item_ids"] == ["SA-007"]


# ── Webhook integration ────────────────────────────────────────────────────


class TestWebhookIntegration:
    """Verify that when a webhook URL is configured, HTTP POSTs are made."""

    def test_webhook_called_for_each_event(self, tmp_path: Path):
        """Each event triggers an HTTP POST when webhook URL is configured."""
        webhook_url = "https://discord.com/api/webhooks/test/abc"
        loop = SignalValidationLoop(tmp_path, webhook_url=webhook_url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204

            loop._notify_event(EventType.STARTED, work_item_ids=["SA-1"])
            loop._notify_event(EventType.COMPLETED, work_item_ids=["SA-2"])

        assert mock_urlopen.call_count == 2, "Expected 2 HTTP POST calls for 2 events"

    def test_no_webhook_no_http_call(self, tmp_path: Path):
        """When no webhook URL, no HTTP calls are made."""
        loop = SignalValidationLoop(tmp_path, webhook_url=None)

        with patch("urllib.request.urlopen") as mock_urlopen:
            loop._notify_event(EventType.STARTED)
            loop._notify_event(EventType.ERROR)

        mock_urlopen.assert_not_called()

    def test_webhook_payload_is_valid_discord_embed(self, tmp_path: Path):
        """The POST payload is a valid Discord embed for each event."""
        webhook_url = "https://discord.com/api/webhooks/test/abc"
        loop = SignalValidationLoop(tmp_path, webhook_url=webhook_url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204

            loop._notify_event(EventType.ERROR, work_item_ids=["SA-X"], description="Test failure")

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        assert "embeds" in body
        assert len(body["embeds"]) == 1
        embed = body["embeds"][0]
        assert embed["title"] is not None
        assert embed["description"] is not None
        assert embed["color"] is not None
        assert embed["timestamp"] is not None

    def test_webhook_signal_independence(self, tmp_path: Path):
        """Signal file is written regardless of webhook success/failure."""
        webhook_url = "https://discord.com/api/webhooks/test/abc"
        loop = SignalValidationLoop(tmp_path, webhook_url=webhook_url)

        # Webhook fails, but signal file should still be written
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("Webhook failed")
            loop._notify_event(EventType.STARTED, work_item_ids=["SA-1"])

        signal_file = tmp_path / "event.pending"
        assert signal_file.exists(), "Signal file should exist even when webhook fails"
        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == "started"


# ── Signal file content verification ───────────────────────────────────────


class TestSignalFileContent:
    """Verify signal file content correctness."""

    def test_event_type_and_ids_in_signal_file(self, tmp_path: Path):
        """Signal file contains correct event_type and work_item_ids."""
        loop = SignalValidationLoop(tmp_path)
        loop._notify_event(EventType.PHASE_CHANGE, work_item_ids=["SA-10", "SA-20"])

        data = json.loads((tmp_path / "event.pending").read_text(encoding="utf-8"))
        assert data["event_type"] == "phase_change"
        assert data["work_item_ids"] == ["SA-10", "SA-20"]

    def test_timestamp_is_iso8601(self, tmp_path: Path):
        """The timestamp field is an ISO8601-formatted string."""
        loop = SignalValidationLoop(tmp_path)
        loop._notify_event(EventType.STARTED)

        data = json.loads((tmp_path / "event.pending").read_text(encoding="utf-8"))
        ts = data["timestamp"]
        assert isinstance(ts, str)
        assert "T" in ts, f"timestamp not ISO8601: {ts}"

    def test_empty_ids_when_none_provided(self, tmp_path: Path):
        """work_item_ids is an empty list when not provided."""
        loop = SignalValidationLoop(tmp_path)
        loop._notify_event(EventType.STARTED)

        data = json.loads((tmp_path / "event.pending").read_text(encoding="utf-8"))
        assert data["work_item_ids"] == []

    def test_signal_file_overwrites(self, tmp_path: Path):
        """Each event overwrites the signal file with new content."""
        loop = SignalValidationLoop(tmp_path)

        loop._notify_event(EventType.STARTED, work_item_ids=["SA-OLD"])
        loop._notify_event(EventType.COMPLETED, work_item_ids=["SA-NEW"])

        data = json.loads((tmp_path / "event.pending").read_text(encoding="utf-8"))
        assert data["event_type"] == "completed"
        assert data["work_item_ids"] == ["SA-NEW"]

    def test_content_single_json_object(self, tmp_path: Path):
        """The signal file contains exactly one JSON object (no appending)."""
        loop = SignalValidationLoop(tmp_path)

        loop._notify_event(EventType.STARTED)
        loop._notify_event(EventType.PHASE_CHANGE)
        loop._notify_event(EventType.COMPLETED)

        content = (tmp_path / "event.pending").read_text(encoding="utf-8").strip()
        assert content.count("{") == 1, "File appears to contain multiple JSON objects"
        assert content.count("}") == 1, "File appears to contain multiple JSON objects"


# ── Rapid successive events ────────────────────────────────────────────────


class TestRapidSuccessiveEvents:
    """Verify rapid successive events each produce unique signal overwrites."""

    def test_rapid_events_no_batching(self, tmp_path: Path):
        """Rapid events each overwrite the signal — only the last remains."""
        loop = SignalValidationLoop(tmp_path)

        for i in range(20):
            loop._notify_event(EventType.STATUS_TRANSITION, work_item_ids=[f"SA-{i:03d}"])

        signal_file = tmp_path / "event.pending"
        content = signal_file.read_text(encoding="utf-8").strip()
        # Should be a single JSON object, not 20 concatenated
        assert content.count("{") == 1, "Events appear batched/appended in signal file"

        data = json.loads(content)
        assert data["work_item_ids"] == ["SA-019"]

    def test_rapid_events_with_webhook(self, tmp_path: Path):
        """Rapid events each trigger individual webhook calls."""
        webhook_url = "https://discord.com/api/webhooks/test/abc"
        loop = SignalValidationLoop(tmp_path, webhook_url=webhook_url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204

            for i in range(5):
                loop._notify_event(EventType.STARTED, work_item_ids=[f"SA-{i}"])

        # 5 events should produce 5 HTTP POSTs
        assert mock_urlopen.call_count == 5

    def test_signal_file_always_overwritten(self, tmp_path: Path):
        """Even for the same event type repeated, overwrite (not append)."""
        loop = SignalValidationLoop(tmp_path)

        loop._notify_event(EventType.STARTED, work_item_ids=["SA-1"])
        loop._notify_event(EventType.STARTED, work_item_ids=["SA-2"])

        content = (tmp_path / "event.pending").read_text(encoding="utf-8").strip()
        assert content.count("{") == 1
        data = json.loads(content)
        assert data["work_item_ids"] == ["SA-2"]


# ── Error resilience ───────────────────────────────────────────────────────


class TestErrorResilience:
    """Verify the pipeline handles errors gracefully."""

    def test_signal_file_error_does_not_affect_webhook(self, tmp_path: Path):
        """If signal file write fails, webhook is still sent."""
        webhook_url = "https://discord.com/api/webhooks/test/abc"
        loop = SignalValidationLoop(tmp_path, webhook_url=webhook_url)

        # Write to a path where we cannot create parent directories
        # This should cause signal write to fail silently
        with patch.object(loop, "_signal_writer") as mock_writer:
            mock_writer.write_event.side_effect = PermissionError("No write access")

            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_ctx = MagicMock()
                mock_urlopen.return_value.__enter__.return_value = mock_ctx
                mock_ctx.status = 204

                loop._notify_event(EventType.ERROR, work_item_ids=["SA-1"])

            # Webhook should still be called even if signal write fails
            mock_urlopen.assert_called_once()

    def test_webhook_error_does_not_affect_signal_file(self, tmp_path: Path):
        """If webhook fails, signal file is still written."""
        webhook_url = "https://discord.com/api/webhooks/test/abc"
        loop = SignalValidationLoop(tmp_path, webhook_url=webhook_url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("Webhook down")
            loop._notify_event(EventType.COMPLETED, work_item_ids=["SA-1"])

        signal_file = tmp_path / "event.pending"
        assert signal_file.exists()
        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == "completed"

    def test_both_fail_does_not_raise(self, tmp_path: Path):
        """When both signal and webhook fail, no exception propagates."""
        webhook_url = "https://discord.com/api/webhooks/test/abc"
        loop = SignalValidationLoop(tmp_path, webhook_url=webhook_url)

        with patch.object(loop, "_signal_writer") as mock_writer:
            mock_writer.write_event.side_effect = PermissionError("No write")
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = OSError("Webhook down")
                # This should not raise
                loop._notify_event(EventType.STARTED, work_item_ids=["SA-1"])

    def test_no_notifier_does_not_crash(self, tmp_path: Path):
        """Loop without signal_writer or webhook_notifier handles notify gracefully."""
        loop = SignalValidationLoop(tmp_path, webhook_url=None)
        loop._signal_writer = None
        loop._webhook_notifier = None

        # This should not raise
        loop._notify_event(EventType.STARTED)
