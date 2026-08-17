"""Shared mock-runner helpers for the audit skill test suite.

The audit runner's post-update lifecycle readback verification
(WL-0MSVVFBJ2003RRYK) requires ``wl show <id> --json`` to reflect the state
a previous ``wl update --status/--stage`` actually applied — exactly how a
real worklog behaves. Many test mocks previously returned a STATIC pre-audit
state regardless of updates, which would make the verification fail (or,
worse, silently pass a test that models a swallowed update).

:func:`stateful_wl_side_effect` wraps any side_effect-style mock runner
(``mock.side_effect = stateful_wl_side_effect(side_effect)``) and
:func:`make_stateful_runner` wraps a plain callable runner used directly
(e.g. ``cmd_issue(..., runner=make_stateful_runner(fake_runner))``).

Only item-level ``wl show <id> --json`` responses are rewritten; commands
that carry no work-item id, ``--children`` shows, ``audit-show`` lookups,
and non-update commands pass through untouched.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

_WL_ITEM_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9-]+$")


def _wl_item_id(cmd) -> str | None:
    """Return the work-item id referenced by a wl command, if any."""
    for token in cmd:
        if _WL_ITEM_ID_RE.match(token):
            return token
    return None


def stateful_wl_side_effect(side_effect):
    """Wrap a mock runner's ``side_effect`` so it models a real worklog.

    ``wl update --status <s> --stage <t>`` mutates a per-item state that
    subsequent ``wl show <id> --json`` responses reflect. This keeps the
    audit runner's post-update lifecycle readback verification
    (WL-0MSVVFBJ2003RRYK) satisfied in tests whose mocks previously
    returned a static pre-audit state regardless of updates.

    Commands that are not item shows/updates (``audit-show``, ``--children``
    shows, ``create``/``comment``/``list``) pass through untouched.

    Returns a new side_effect callable; assign it to ``mock.side_effect``.
    """
    states: dict[str, dict[str, str]] = {}

    def _apply_update(item_id: str, cmd) -> None:
        state = states.setdefault(item_id, {})
        try:
            idx = cmd.index("--status")
            state["status"] = cmd[idx + 1]
        except (ValueError, IndexError):
            pass
        try:
            idx = cmd.index("--stage")
            state["stage"] = cmd[idx + 1]
        except (ValueError, IndexError):
            pass

    def _wrapped(cmd):
        cmd_str = " ".join(cmd)
        item_id = _wl_item_id(cmd)
        result = side_effect(cmd)
        # A wl update applies only when it actually succeeds (rc 0) — a
        # failed/transient update must NOT mutate the modeled state.
        if (
            item_id
            and "update" in cmd_str
            and "--status" in cmd_str
            and getattr(result, "returncode", 1) == 0
        ):
            _apply_update(item_id, cmd)
        if (
            item_id
            and item_id in states
            and "show" in cmd_str
            and "--children" not in cmd_str
            and "--json" in cmd_str
            and "audit-show" not in cmd_str
            and getattr(result, "returncode", 1) == 0
            and result.stdout
        ):
            try:
                data = json.loads(result.stdout)
            except (TypeError, ValueError):
                return result
            if isinstance(data, dict):
                wi = data.get("workItem")
                if not isinstance(wi, dict):
                    wi = data  # some fixtures return the item dict directly
                if isinstance(wi, dict):
                    state = states[item_id]
                    wi["status"] = state.get("status", wi.get("status"))
                    wi["stage"] = state.get("stage", wi.get("stage") or "")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(data),
                        stderr=result.stderr,
                    )
        return result

    return _wrapped


def make_stateful_runner(runner):
    """Wrap a plain-function runner (cmd -> CompletedProcess) with the same
    stateful behavior as :func:`stateful_wl_side_effect`."""
    return stateful_wl_side_effect(runner)
