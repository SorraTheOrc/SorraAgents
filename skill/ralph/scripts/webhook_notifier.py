"""Optional Discord webhook notifier for Ralph events.

Sends a Discord embed via HTTP POST when a webhook URL is configured.
This is a fire-and-forget pipeline independent of the signal file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Sequence

from skill.ralph.scripts.signal_system import EventType

logger = logging.getLogger("ralph")

# Discord embed colour (decimal) — a muted blue
_DISCORD_EMBED_COLOR = 5_797_046


def resolve_webhook_url(config: dict) -> str | None:
    """Extract the Discord webhook URL from Ralph's configuration dict.

    Reads ``config["discord"]["webhook_url"]`` and returns None when the
    key is missing, empty, or not a valid non-empty string.

    Args:
        config: Ralph configuration dict.

    Returns:
        The webhook URL string, or None if not configured.
    """
    discord_section = config.get("discord")
    if not isinstance(discord_section, dict):
        return None
    url = discord_section.get("webhook_url")
    if not url or not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    return url if url else None


class WebhookNotifier:
    """Sends Discord embed notifications for Ralph events.

    This is a fire-and-forget notifier: HTTP failures are logged at WARNING
    level and never propagated. When no webhook URL is configured, all
    ``send_event`` calls are no-ops.
    """

    def __init__(self, webhook_url: str | None) -> None:
        self._webhook_url = webhook_url

    def send_event(
        self,
        event_type: EventType,
        work_item_ids: Sequence[str] | None = None,
        description: str | None = None,
        timestamp: str | None = None,
        title: str | None = None,
        cmd: str | None = None,
    ) -> bool | None:
        """Send a Discord embed notification for the given event.

        This is a fire-and-forget method:
        - When ``webhook_url`` is None or empty, it is a no-op (returns None).
        - HTTP failures are logged at WARNING level and swallowed (returns False).
        - Successful delivery returns True.

        Args:
            event_type: The event type that occurred.
            work_item_ids: Optional list of related work-item IDs.
            description: Optional human-readable description. A default
                         description is generated when not supplied.
            timestamp: ISO8601 timestamp. Defaults to current UTC time.
            title: Optional work-item title. When provided, the embed title
                   becomes "Ralph: {title}". When omitted or empty, the embed
                   title defaults to "Ralph Event: {event_type}".
            cmd: Optional pi command string to include in the embed.

        Returns:
            True on successful HTTP delivery, False on failure, None when
            no webhook URL is configured.
        """
        if not self._webhook_url:
            return None

        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()

        if description is None:
            description = f"Ralph event: {event_type.value}"

        embed = self._build_embed(event_type, description, work_item_ids, timestamp, title=title, cmd=cmd)
        payload = {"embeds": [embed]}

        try:
            self._post_payload(payload)
            logger.info(
                "ralph.webhook.sent event=%s ids=%s",
                event_type.value,
                list(work_item_ids) if work_item_ids else [],
            )
            return True
        except Exception as exc:
            logger.warning(
                "ralph.webhook.failed event=%s error=%s",
                event_type.value,
                exc,
            )
            return False

    def _build_embed(
        self,
        event_type: EventType,
        description: str,
        work_item_ids: Sequence[str] | None,
        timestamp: str,
        title: str | None = None,
        cmd: str | None = None,
    ) -> dict:
        """Build a Discord embed dict for the event.

        When ``title`` is provided and non-empty, the embed title becomes
        "Ralph: {title}". Otherwise it defaults to
        "Ralph Event: {event_type}".

        When ``cmd`` is provided and non-empty, it is included as a
        "Pi Command" field in the embed.
        """
        ids_str = ", ".join(work_item_ids) if work_item_ids else "None"

        if title:
            embed_title = f"Ralph: {title}"
        else:
            embed_title = f"Ralph Event: {event_type.value.replace('_', ' ').title()}"

        fields: list[dict] = [
            {"name": "Event Type", "value": event_type.value, "inline": True},
            {"name": "Work Item IDs", "value": ids_str, "inline": True},
        ]
        if cmd:
            fields.insert(0, {"name": "Pi Command", "value": cmd, "inline": False})

        embed: dict = {
            "title": embed_title,
            "description": description,
            "color": _DISCORD_EMBED_COLOR,
            "timestamp": timestamp,
            "fields": fields,
        }
        return embed

    def _post_payload(self, payload: dict) -> None:
        """POST the JSON payload to the webhook URL.

        Uses stdlib ``urllib.request`` for zero-dependency HTTP.
        Raises on any network or HTTP error.
        """
        import urllib.request

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Ralph/1.0 (Worklog Orchestration Agent)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            # Read and discard response body; status is validated by urlopen
            resp.read()
