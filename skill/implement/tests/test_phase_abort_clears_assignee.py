"""Regression tests: ``phase_abort`` releases the assignee hold.

A non-empty assignee is a scheduling hold (AGENTS_GLOBAL: "non-empty
assignee = scheduling hold (producer `Map`; clear to release)"). Aborting an
implementation must reset the item to ``open`` AND clear the assignee so the
released item is never left on a hold that blocks re-dispatch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement


def test_abort_resets_open_and_clears_assignee():
    """phase_abort's reset-to-open also releases the assignee hold."""
    lifecycle = MagicMock()
    with (
        patch.object(implement, "_discover_worktree", return_value=None),
        patch.object(implement, "StatusLifecycle", lifecycle),
        patch.object(implement, "_mark_pane_aborted"),
        patch.object(implement, "wl_add_comment"),
        patch.object(implement, "_restore_repo_state"),
    ):
        report = implement.phase_abort("SA-TEST", json_output=True)

    assert report["success"] is True
    lifecycle.update_status.assert_called_once_with(
        "SA-TEST", "open", assignee=""
    )


def test_abort_safety_reset_clears_assignee():
    """The signal/error safety reset also clears the assignee.

    The safety reset runs when the phase cannot complete normally (signal,
    KeyboardInterrupt, unhandled exception); releasing the hold there is what
    guarantees an aborted run never strands the item on an assignee hold.
    """
    lifecycle = MagicMock()
    show = MagicMock(return_value={"workItem": {"status": "in-progress"}})
    with (
        patch.object(implement, "StatusLifecycle", lifecycle),
    ):
        lifecycle.show = show
        implement._safety_reset_if_in_progress("SA-TEST")

    lifecycle.update_status.assert_called_once_with(
        "SA-TEST", "open", assignee=""
    )
