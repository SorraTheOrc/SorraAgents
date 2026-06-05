"""Ralph signal system: event type definitions, SignalWriter, and config-based path resolution.

Ralph writes a signal file (``event.pending`` by default) when major events
occur during the loop lifecycle. This module defines:

- ``EventType`` — an enum of all event types that trigger signals
- ``SignalWriter`` — writes a JSON signal file on each event (overwrite, no append)
- ``resolve_signal_path`` — resolves the signal file path from Ralph's config
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("ralph")


class EventType(str, Enum):
    """All event types that can trigger a signal file write.

    Each member's value is the string that appears in the signal file's
    ``event_type`` field.
    """

    STATUS_TRANSITION = "status_transition"
    PHASE_CHANGE = "phase_change"
    ERROR = "error"
    MAX_ATTEMPTS = "max_attempts"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    STARTED = "started"


class SignalWriter:
    """Writes a JSON signal file on each event.

    The signal file is overwritten on each call (never appended).  This is
    a fire-and-forget component: errors are logged but never propagated so
    the Ralph loop is never blocked by signal-file I/O.
    """

    def __init__(self, signal_file_path: Path) -> None:
        self._signal_file_path = signal_file_path

    @property
    def signal_file_path(self) -> Path:
        return self._signal_file_path

    def write_event(
        self,
        event_type: EventType,
        work_item_ids: Sequence[str] | None = None,
        timestamp: str | None = None,
    ) -> Path:
        """Write an event signal file.

        Args:
            event_type: The type of event that occurred.
            work_item_ids: Optional list of relevant work-item IDs.
            timestamp: ISO8601 timestamp string. If not provided, the current
                       UTC time is used.

        Returns:
            The Path to the written signal file.

        This method never raises. I/O errors are logged at WARNING level and
        silently ignored so that the Ralph loop is never blocked by a signal
        write failure.
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()

        payload: dict[str, object] = {
            "event_type": event_type.value,
            "timestamp": timestamp,
            "work_item_ids": list(work_item_ids) if work_item_ids else [],
        }

        try:
            self._signal_file_path.parent.mkdir(parents=True, exist_ok=True)
            self._signal_file_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "ralph.signal.write_failed path=%s error=%s",
                self._signal_file_path,
                exc,
            )

        return self._signal_file_path


_DEFAULT_SIGNAL_PATH = Path(".ralph") / "event.pending"


def resolve_signal_path(config: dict) -> Path:
    """Resolve the signal file path from Ralph's configuration dict.

    Reads ``config["signal"]["file_path"]`` and falls back to
    ``.ralph/event.pending`` when the key is missing, empty, or not a
    valid path string.

    Args:
        config: Ralph configuration dict (from ``_load_config()``).

    Returns:
        Path to the signal file.
    """
    signal_section = config.get("signal")
    if not isinstance(signal_section, dict):
        return _DEFAULT_SIGNAL_PATH

    file_path = signal_section.get("file_path")
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        return _DEFAULT_SIGNAL_PATH

    return Path(file_path.strip())
