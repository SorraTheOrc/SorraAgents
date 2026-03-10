"""Scheduler initialization and operational helpers.

Module-level utility functions extracted from the Scheduler class to keep
the main scheduler module focused on scheduling logic.  All functions
operate on a :class:`~ampa.scheduler_store.SchedulerStore` and do not
require Scheduler instance state.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List

from .scheduler_types import CommandSpec, _utc_now, _from_iso
from .scheduler_store import SchedulerStore

LOG = logging.getLogger("ampa.scheduler")

# ---------------------------------------------------------------------------
# Well-known command identifiers for auto-registered built-in commands.
# ---------------------------------------------------------------------------

_WATCHDOG_COMMAND_ID = "stale-delegation-watchdog"
_TEST_BUTTON_COMMAND_ID = "test-button"


# ---------------------------------------------------------------------------
# Initialization helpers (called from Scheduler.__init__)
# ---------------------------------------------------------------------------


def clear_stale_running_states(store: SchedulerStore) -> None:
    """Clear ``running`` flags for commands whose last_start_ts is older
    than ``AMPA_STALE_RUNNING_THRESHOLD_SECONDS`` (default 3600s).

    This prevents commands from remaining marked as running due to a
    previous crash or unhandled exception which would otherwise block
    future scheduling.
    """
    try:
        thresh_raw = os.getenv("AMPA_STALE_RUNNING_THRESHOLD_SECONDS", "3600")
        try:
            threshold = int(thresh_raw)
        except Exception:
            threshold = 3600
        now = _utc_now()
        for cmd in store.list_commands():
            try:
                st = store.get_state(cmd.command_id) or {}
                if st.get("running") is not True:
                    continue
                last_start_iso = st.get("last_start_ts")
                last_start = _from_iso(last_start_iso) if last_start_iso else None
                age = (
                    None
                    if last_start is None
                    else int((now - last_start).total_seconds())
                )
                if age is None or age > threshold:
                    st["running"] = False
                    store.update_state(cmd.command_id, st)
                    LOG.info(
                        "Cleared stale running flag for %s (age_s=%s)",
                        cmd.command_id,
                        age,
                    )
            except Exception:
                LOG.exception(
                    "Failed to evaluate/clear running state for %s",
                    getattr(cmd, "command_id", "?"),
                )
    except Exception:
        LOG.exception("Unexpected error while clearing stale running states")


def ensure_watchdog_command(store: SchedulerStore) -> None:
    """Register the stale-delegation-watchdog command if absent."""
    try:
        existing = store.list_commands()
        for cmd in existing:
            if cmd.command_id == _WATCHDOG_COMMAND_ID:
                LOG.debug(
                    "Watchdog command already registered: %s", _WATCHDOG_COMMAND_ID
                )
                return
        watchdog_spec = CommandSpec(
            command_id=_WATCHDOG_COMMAND_ID,
            command="echo watchdog",
            requires_llm=False,
            frequency_minutes=30,
            priority=0,
            metadata={},
            title="Stale Delegation Watchdog",
            max_runtime_minutes=5,
            command_type="stale-delegation-watchdog",
        )
        store.add_command(watchdog_spec)
        LOG.info(
            "Auto-registered watchdog command: %s (every %dm)",
            _WATCHDOG_COMMAND_ID,
            watchdog_spec.frequency_minutes,
        )
    except Exception:
        LOG.exception("Failed to auto-register watchdog command")


def ensure_test_button_command(store: SchedulerStore) -> None:
    """Register the interactive test-button command if absent.

    Only registers when ``AMPA_DISCORD_BOT_TOKEN`` is set — without a bot
    the buttons cannot be rendered or clicked.
    """
    if not os.getenv("AMPA_DISCORD_BOT_TOKEN"):
        return
    try:
        existing = store.list_commands()
        for cmd in existing:
            if cmd.command_id == _TEST_BUTTON_COMMAND_ID:
                LOG.debug(
                    "Test-button command already registered: %s",
                    _TEST_BUTTON_COMMAND_ID,
                )
                return
        test_button_spec = CommandSpec(
            command_id=_TEST_BUTTON_COMMAND_ID,
            command="echo test-button",
            requires_llm=False,
            frequency_minutes=15,
            priority=0,
            metadata={},
            title="Interactive Test Button",
            max_runtime_minutes=1,
            command_type="test-button",
        )
        store.add_command(test_button_spec)
        LOG.info(
            "Auto-registered test-button command: %s (every %dm)",
            _TEST_BUTTON_COMMAND_ID,
            test_button_spec.frequency_minutes,
        )
    except Exception:
        LOG.exception("Failed to auto-register test-button command")


# ---------------------------------------------------------------------------
# Runtime helpers (called from Scheduler.start_command / run_forever)
# ---------------------------------------------------------------------------


def send_test_button_message(notifier: Any) -> None:
    """Send the "Blue or Red?" test message with interactive buttons."""
    components = [
        {
            "type": "button",
            "label": "Blue",
            "style": "primary",
            "custom_id": "test_blue",
        },
        {"type": "button", "label": "Red", "style": "danger", "custom_id": "test_red"},
    ]
    notifier.notify(
        title="Blue or Red?",
        body="Pick a colour by clicking a button below.",
        message_type="command",
        components=components,
    )


def log_health(store: SchedulerStore) -> None:
    """Emit a periodic health report about scheduled commands."""
    try:
        cmds = store.list_commands()
    except Exception:
        LOG.exception("Failed to read commands for health report")
        return
    lines: List[str] = []
    now = _utc_now()
    for cmd in cmds:
        try:
            state = store.get_state(cmd.command_id) or {}
            last_run_iso = state.get("last_run_ts")
            last_run_dt = _from_iso(last_run_iso) if last_run_iso else None
            age = (
                int((now - last_run_dt).total_seconds())
                if last_run_dt is not None
                else None
            )
            running = bool(state.get("running"))
            last_exit = state.get("last_exit_code")
            lines.append(
                f"{cmd.command_id} title={cmd.title!r} last_run={last_run_iso or 'never'} "
                f"age_s={age if age is not None else 'NA'} exit={last_exit} running={running}"
            )
        except Exception:
            LOG.exception(
                "Failed to build health line for %s",
                getattr(cmd, "command_id", "?"),
            )
    LOG.info("Scheduler health report: %d commands\n%s", len(lines), "\n".join(lines))
