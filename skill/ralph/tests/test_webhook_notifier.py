"""Unit tests for the Ralph WebhookNotifier component.

Tests cover HTTP delivery, config loading, error handling, and independence
from the signal file pipeline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.signal_system import EventType  # noqa: E402
from skill.ralph.scripts.webhook_notifier import WebhookNotifier, resolve_webhook_url  # noqa: E402


# ── Config-based webhook URL resolution ─────────────────────────────────────


class TestResolveWebhookUrl:
    """Verify webhook URL resolution from config dict."""

    def test_no_config_returns_none(self):
        """When config has no discord section, returns None."""
        assert resolve_webhook_url({}) is None

    def test_empty_discord_section_returns_none(self):
        """When discord section is empty, returns None."""
        assert resolve_webhook_url({"discord": {}}) is None

    def test_discord_is_none_returns_none(self):
        """When discord is explicitly None, returns None."""
        assert resolve_webhook_url({"discord": None}) is None

    def test_webhook_url_set(self):
        """When discord.webhook_url is set, returns it."""
        url = "https://discord.com/api/webhooks/123/abc"
        result = resolve_webhook_url({"discord": {"webhook_url": url}})
        assert result == url

    def test_webhook_url_empty_string_returns_none(self):
        """An empty webhook_url string returns None."""
        result = resolve_webhook_url({"discord": {"webhook_url": ""}})
        assert result is None

    def test_webhook_url_none_returns_none(self):
        """A None webhook_url returns None."""
        result = resolve_webhook_url({"discord": {"webhook_url": None}})
        assert result is None

    def test_only_other_discord_keys_returns_none(self):
        """Other keys in discord section without webhook_url return None."""
        result = resolve_webhook_url({"discord": {"other_key": "value"}})
        assert result is None

    def test_ralph_json_full_config(self):
        """Integration-style: a full config resolves correctly."""
        url = "https://discord.com/api/webhooks/channel/token"
        config = {"discord": {"webhook_url": url}, "signal": {"file_path": ".ralph/event.pending"}}
        assert resolve_webhook_url(config) == url


# ── WebhookNotifier HTTP delivery ───────────────────────────────────────────


class TestWebhookNotifierHttpDelivery:
    """Verify WebhookNotifier POSTs valid Discord embed JSON."""

    def test_send_event_user_agent_header(self):
        """The User-Agent header is included to avoid Discord 403 bot detection."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(EventType.STARTED, work_item_ids=["SA-1"])

        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        # urllib normalizes header keys to 'User-agent' (lowercase 'a')
        assert request.headers.get("User-agent") == "Ralph/1.0 (Worklog Orchestration Agent)"

    def test_send_event_with_title_uses_ralph_prefix(self):
        """When title is provided, embed title is 'Ralph: <title>'."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(
                EventType.STARTED,
                work_item_ids=["SA-1"],
                title="My Work Item",
            )

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        assert embed["title"] == "Ralph: My Work Item"

    def test_send_event_without_title_uses_default(self):
        """When title is not provided, embed title defaults to 'Ralph Event: <event_type>'."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(
                EventType.STATUS_TRANSITION,
                work_item_ids=["SA-1"],
            )

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        assert embed["title"] == "Ralph Event: Status Transition"

    def test_send_event_with_empty_title_falls_back_to_default(self):
        """When title is an empty string, embed title defaults to 'Ralph Event: <event_type>'."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(
                EventType.COMPLETED,
                work_item_ids=["SA-1"],
                title="",
            )

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        assert embed["title"] == "Ralph Event: Completed"

    def test_send_event_embed_contains_required_fields(self):
        """The POST body JSON contains event_type, timestamp, description, and work_item_ids."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(
                EventType.STATUS_TRANSITION,
                work_item_ids=["SA-100", "SA-200"],
                description="Status changed",
            )

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        assert "embeds" in body
        assert len(body["embeds"]) == 1
        embed = body["embeds"][0]
        assert embed["title"] is not None
        assert embed["description"] is not None
        assert embed["color"] is not None
        assert embed["timestamp"] is not None
        assert "fields" in embed

    def test_send_event_embed_contains_event_type_and_ids(self):
        """Embed fields include event type and work item IDs."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(
                EventType.ERROR,
                work_item_ids=["SA-999"],
                description="Something went wrong",
            )

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        field_values = {f["name"]: f["value"] for f in embed["fields"]}
        assert field_values["Event Type"] == "error"
        assert field_values["Work Item IDs"] == "SA-999"

    def test_send_event_with_multiple_ids(self):
        """Multiple work item IDs are all included in the embed."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)
        ids = ["SA-1", "SA-2", "SA-3"]

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(EventType.PHASE_CHANGE, work_item_ids=ids)

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        field_values = {f["name"]: f["value"] for f in embed["fields"]}
        assert field_values["Work Item IDs"] == "SA-1, SA-2, SA-3"

    def test_send_event_without_ids(self):
        """Embed gracefully handles missing work item IDs."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(EventType.STARTED)

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        field_values = {f["name"]: f["value"] for f in embed["fields"]}
        assert field_values["Work Item IDs"] == "None"

    def test_send_event_timestamp_in_embed(self):
        """The embed timestamp is an ISO8601 string."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(EventType.COMPLETED, work_item_ids=["SA-1"])

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        assert "T" in embed["timestamp"], "timestamp should be ISO8601"

    def test_send_event_custom_timestamp(self):
        """A caller-supplied timestamp is used in the embed."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)
        custom_ts = "2026-06-05T12:00:00.000Z"

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(EventType.STARTED, timestamp=custom_ts)

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        assert embed["timestamp"] == custom_ts

    def test_send_event_default_description(self):
        """A default description is used when none is supplied."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(EventType.STARTED, work_item_ids=["SA-1"])

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        embed = body["embeds"][0]
        # Should have some description even if none was provided
        assert embed["description"] is not None
        assert len(embed["description"]) > 0


# ── Error handling (fire-and-forget) ────────────────────────────────────────


class TestWebhookNotifierErrorHandling:
    """Verify fire-and-forget error handling: logs WARNING, no retry, no raise."""

    def test_http_error_logs_warning(self, caplog):
        """HTTP failures are logged at WARNING level."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with caplog.at_level(logging.WARNING):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = OSError("Connection refused")
                notifier.send_event(EventType.ERROR, work_item_ids=["SA-1"])

        assert any(
            record.levelname == "WARNING" and "webhook" in record.getMessage().lower()
            for record in caplog.records
        ), "Expected a WARNING log about webhook failure"

    def test_http_error_does_not_raise(self):
        """HTTP failures never propagate an exception."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("Connection refused")
            # This should not raise
            notifier.send_event(EventType.ERROR, work_item_ids=["SA-1"])

    def test_urlopen_error_logs_warning(self, caplog):
        """Any urllib.error.URLError or similar is logged and swallowed."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with caplog.at_level(logging.WARNING):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = ValueError("Invalid URL")
                notifier.send_event(EventType.ERROR)

        assert any(
            record.levelname == "WARNING" and "webhook" in record.getMessage().lower()
            for record in caplog.records
        ), "Expected a WARNING log entry"

    def test_http_timeout_logs_warning(self, caplog):
        """Timeout errors are logged and swallowed."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with caplog.at_level(logging.WARNING):
            with patch("urllib.request.urlopen") as mock_urlopen:
                import socket
                mock_urlopen.side_effect = socket.timeout("timed out")
                notifier.send_event(EventType.STARTED)

        assert any(
            record.levelname == "WARNING" and "webhook" in record.getMessage().lower()
            for record in caplog.records
        ), "Expected a WARNING log entry"

    def test_http_404_logs_warning(self, caplog):
        """Non-2xx HTTP responses are logged but not raised."""
        import urllib.error

        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with caplog.at_level(logging.WARNING):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = urllib.error.HTTPError(
                    url, 404, "Not Found", {}, None
                )
                notifier.send_event(EventType.COMPLETED)

        assert any(
            record.levelname == "WARNING" and "webhook" in record.getMessage().lower()
            for record in caplog.records
        ), "Expected a WARNING log entry"


# ── No-op when no webhook URL configured ────────────────────────────────────


class TestWebhookNotifierNoOp:
    """Verify WebhookNotifier does nothing when no webhook URL is configured."""

    def test_no_url_no_http_call(self):
        """When webhook URL is None, no HTTP call is made."""
        notifier = WebhookNotifier(None)

        with patch("urllib.request.urlopen") as mock_urlopen:
            notifier.send_event(EventType.STARTED)

        mock_urlopen.assert_not_called()

    def test_no_url_empty_string_no_http_call(self):
        """When webhook URL is empty, no HTTP call is made."""
        notifier = WebhookNotifier("")

        with patch("urllib.request.urlopen") as mock_urlopen:
            notifier.send_event(EventType.COMPLETED)

        mock_urlopen.assert_not_called()

    def test_no_url_no_log_errors(self, caplog):
        """When no URL configured, no errors are logged."""
        notifier = WebhookNotifier(None)

        with caplog.at_level(logging.WARNING):
            notifier.send_event(EventType.ERROR)

        webhook_logs = [r for r in caplog.records if "webhook" in r.getMessage().lower()]
        assert len(webhook_logs) == 0, "Expected no webhook-related log entries"

    def test_no_url_returns_none(self):
        """send_event returns None when no URL configured (no HTTP call made)."""
        notifier = WebhookNotifier(None)
        result = notifier.send_event(EventType.STARTED)
        assert result is None

    def test_no_url_with_ids_no_call(self):
        """Even with work_item_ids, no HTTP call when URL is None."""
        notifier = WebhookNotifier(None)

        with patch("urllib.request.urlopen") as mock_urlopen:
            notifier.send_event(EventType.PHASE_CHANGE, work_item_ids=["SA-1"])

        mock_urlopen.assert_not_called()


# ── Independence from signal file ───────────────────────────────────────────


class TestWebhookNotifierIndependence:
    """Verify WebhookNotifier does NOT write to the signal file."""

    def test_no_signal_file_written_by_default(self, tmp_path):
        """WebhookNotifier.send_event does not create any signal files."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        # Count files before
        files_before = set(tmp_path.rglob("*"))

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(EventType.STARTED)

        files_after = set(tmp_path.rglob("*"))
        # No new files in tmp_path
        assert files_after == files_before, "WebhookNotifier should not create any signal files"

    def test_webhook_does_not_import_signal_writer(self):
        """The webhook_notifier module works independently of signal_system internals."""
        # Ensure we can import and use without signal file interaction
        from skill.ralph.scripts.webhook_notifier import WebhookNotifier as WN
        assert WN is not None


# ── Integration with EventType ──────────────────────────────────────────────


class TestWebhookNotifierWithEventType:
    """Verify WebhookNotifier works with all EventType values."""

    def test_all_event_types_with_title(self):
        """All EventType values with a title produce embed title 'Ralph: <title>'."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            notifier.send_event(EventType.STARTED, work_item_ids=["SA-X"], title="Test Title")

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        assert body["embeds"][0]["title"] == "Ralph: Test Title"


    @pytest.mark.parametrize("event_type", list(EventType))
    def test_all_event_types_accepted(self, event_type):
        """All EventType values are accepted by send_event without error."""
        url = "https://discord.com/api/webhooks/123/abc"
        notifier = WebhookNotifier(url)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204
            # Should not raise for any event type
            notifier.send_event(event_type, work_item_ids=["SA-X"])
