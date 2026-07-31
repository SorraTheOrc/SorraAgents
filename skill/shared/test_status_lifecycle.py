#!/usr/bin/env python3
"""Unit and integration tests for StatusLifecycle context manager."""  # noqa: EXE001

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.shared.status_lifecycle import StatusLifecycle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_run():
    """Fixture that patches subprocess.run and returns the mock."""
    with mock.patch("skill.shared.status_lifecycle.subprocess.run") as m:
        yield m


def _make_wl_show_proc(status: str = "open", stage: str = "idea"):
    """Build a mock CompletedProcess for ``wl show <id> --json``."""
    data = {
        "success": True,
        "workItem": {
            "id": "TEST-123",
            "status": status,
            "stage": stage,
        },
    }
    return subprocess.CompletedProcess(
        args=["wl", "show", "TEST-123", "--json"],
        returncode=0,
        stdout=json.dumps(data),
        stderr="",
    )


def _make_wl_update_proc():
    """Build a mock CompletedProcess for a successful ``wl update``."""
    return subprocess.CompletedProcess(
        args=["wl", "update", "TEST-123", "--status", "in_progress", "--json"],
        returncode=0,
        stdout=json.dumps({"success": True}),
        stderr="",
    )


def _make_wl_failure_proc(returncode: int = 1, stderr: str = "error"):
    """Build a mock CompletedProcess for a failed ``wl`` command."""
    return subprocess.CompletedProcess(
        args=["wl", "update", "TEST-123", "--status", "in_progress", "--json"],
        returncode=returncode,
        stdout="",
        stderr=stderr,
    )


# ===========================================================================
# Unit tests (mocked subprocess)
# ===========================================================================


class TestStatusLifecycleUnit:
    """Unit tests using mocked subprocess.run."""

    def test_normal_lifecycle_open_to_completed(self, mock_run):
        """Normal: open → in_progress (entry) → completed (exit)."""
        # Simulate: wl show returns open, wl update succeeds
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),   # __enter__: capture original
            _make_wl_update_proc(),               # __enter__: set in_progress
            _make_wl_update_proc(),               # __exit__: set completed
        ]

        with StatusLifecycle("TEST-123"):
            pass

        # Verify calls
        assert mock_run.call_count == 3
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ["wl", "show", "TEST-123", "--json"]
        assert calls[1] == ["wl", "update", "TEST-123", "--status", "in_progress", "--json"]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "completed", "--json"]

    def test_failure_lifecycle_restores_original(self, mock_run):
        """Exception: open → in_progress → restore to open."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),  # restore
        ]

        with pytest.raises(ValueError, match="test error"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                raise ValueError("test error")

        # Verify: original status restored (open), no assignee (wasn't set)
        assert mock_run.call_count == 3
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "open", "--json"]
        assert "--assignee" not in calls[2]

    def test_idempotent_already_in_progress(self, mock_run):
        """Idempotent: already in_progress → still works (no unnecessary update)."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="in_progress"),  # already in_progress
            _make_wl_update_proc(),                      # set in_progress (no-op)
            _make_wl_update_proc(),                      # set completed
        ]

        with StatusLifecycle("TEST-123"):
            pass

        assert mock_run.call_count == 3
        calls = [c.args[0] for c in mock_run.call_args_list]
        # Still calls update in_progress (harmless) and then completed
        assert "in_progress" in calls[1]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "completed", "--json"]

    def test_stage_advancement_on_success(self, mock_run):
        """Successful exit with target_stage sets both completed + stage."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", target_stage="in_review"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]
        assert "--stage" in calls[2]
        assert "in_review" in calls[2]

    def test_assignee_on_entry(self, mock_run):
        """Entry with assignee sets assignee, success does NOT clear."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123", assignee="bot"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        # Entry sets assignee
        assert calls[1][:5] == ["wl", "update", "TEST-123", "--status", "in_progress"]
        assert "--assignee" in calls[1]
        assert "bot" in calls[1]
        # Exit is completed without assignee
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "completed"]

    def test_assignee_cleared_on_failure(self, mock_run):
        """Failure with assignee restores status AND clears assignee."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),  # restore
        ]

        with pytest.raises(RuntimeError):  # noqa: SIM117
            with StatusLifecycle("TEST-123", assignee="bot"):
                raise RuntimeError("boom")

        calls = [c.args[0] for c in mock_run.call_args_list]
        # Restore status AND clear assignee
        assert calls[2][:5] == ["wl", "update", "TEST-123", "--status", "open"]
        assert "--assignee" in calls[2]

    def test_wl_show_failure_then_update_fails(self, mock_run):
        """wl show failure is caught, but in_progress update failure raises."""
        mock_run.side_effect = [
            _make_wl_failure_proc(returncode=1, stderr="not found"),  # show fails
            _make_wl_failure_proc(returncode=1, stderr="update failed"),  # update fails
        ]

        with pytest.raises(RuntimeError, match="update failed"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                pass

    def test_wl_update_failure_on_entry_raises(self, mock_run):
        """wl update failure on entry raises and does NOT restore."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_failure_proc(returncode=1, stderr="permission denied"),
        ]

        with pytest.raises(RuntimeError, match="permission denied"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                pass

    def test_wl_update_failure_on_exit_raises(self, mock_run):
        """wl update failure on successful exit raises (caller catches)."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_failure_proc(returncode=1, stderr="timeout"),
        ]

        with pytest.raises(RuntimeError, match="timeout"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                pass

    def test_wl_update_failure_on_restore_logged(self, mock_run, caplog):
        """wl update failure during exception restore is logged, not raised."""
        import logging
        caplog.set_level(logging.WARNING)

        mock_run.side_effect = [
            _make_wl_show_proc(status="open"),
            _make_wl_update_proc(),
            _make_wl_failure_proc(returncode=1, stderr="restore failed"),
        ]

        with pytest.raises(ValueError, match="inner error"):  # noqa: SIM117
            with StatusLifecycle("TEST-123"):
                raise ValueError("inner error")

        # The original exception is reraised, restore failure is logged
        assert "Failed to restore" in caplog.text
        assert "TEST-123" in caplog.text

    def test_completed_already(self, mock_run):
        """Starting from completed: capture, set in_progress, set completed on exit."""
        mock_run.side_effect = [
            _make_wl_show_proc(status="completed"),
            _make_wl_update_proc(),
            _make_wl_update_proc(),
        ]

        with StatusLifecycle("TEST-123"):
            pass

        calls = [c.args[0] for c in mock_run.call_args_list]
        # Still sets in_progress on entry, completed on exit
        assert "in_progress" in calls[1]
        assert calls[2] == ["wl", "update", "TEST-123", "--status", "completed", "--json"]


# ===========================================================================
# Integration tests (real wl CLI, optional)
# ===========================================================================

@pytest.mark.skipif(
    not bool(os.environ.get("RUN_INTEGRATION_TESTS")),
    reason="Integration tests require RUN_INTEGRATION_TESTS=1",
)
class TestStatusLifecycleIntegration:
    """Integration tests using the real wl CLI.

    These create a real work item, run the lifecycle, and verify transitions.
    """

    @pytest.fixture
    def temp_work_item(self):
        """Create a temporary work item for testing, clean up afterward."""
        result = subprocess.run(
            [
                "wl", "create",
                "--title", "Temp test item (StatusLifecycle integration)",
                "--description", "Auto-created by test_status_lifecycle.py",
                "--priority", "low",
                "--issue-type", "task",
                "--status", "open",
                "--json",
            ],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        work_item = data.get("workItem", data)
        item_id = work_item["id"]
        yield item_id
        # Cleanup: close the item
        subprocess.run(  # noqa: PLW1510
            ["wl", "update", item_id, "--status", "completed", "--json"],
            capture_output=True,
        )

    def test_status_transitions_real(self, temp_work_item):
        """Verify real status transitions: open → in_progress → completed."""
        item_id = temp_work_item

        # Verify starts as open
        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        initial = json.loads(show.stdout)
        assert initial["workItem"]["status"] == "open"

        # Run lifecycle
        with StatusLifecycle(item_id):
            # Inside the context: should be in_progress
            show = subprocess.run(
                ["wl", "show", item_id, "--json"],
                capture_output=True, text=True, check=True,
            )
            inside = json.loads(show.stdout)
            assert inside["workItem"]["status"] == "in_progress"

        # After context: should be completed
        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        after = json.loads(show.stdout)
        assert after["workItem"]["status"] == "completed"

    def test_failure_restores_status_real(self, temp_work_item):
        """Verify exception restores original status."""
        item_id = temp_work_item

        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        initial = json.loads(show.stdout)
        original_status = initial["workItem"]["status"]

        with pytest.raises(ValueError):  # noqa: SIM117
            with StatusLifecycle(item_id):
                raise ValueError("intentional failure")

        # After exception: status should be restored
        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        after = json.loads(show.stdout)
        assert after["workItem"]["status"] == original_status

    def test_stage_advancement_real(self, temp_work_item):
        """Verify stage advancement on success."""
        item_id = temp_work_item

        with StatusLifecycle(item_id, target_stage="in_review"):
            pass

        show = subprocess.run(
            ["wl", "show", item_id, "--json"],
            capture_output=True, text=True, check=True,
        )
        after = json.loads(show.stdout)
        assert after["workItem"]["status"] == "completed"
        assert after["workItem"]["stage"] == "in_review"

    def test_assignee_real(self, temp_work_item):
        """Verify assignee is set on entry."""
        item_id = temp_work_item

        with StatusLifecycle(item_id, assignee="integration-test"):
            show = subprocess.run(
                ["wl", "show", item_id, "--json"],
                capture_output=True, text=True, check=True,
            )
            inside = json.loads(show.stdout)
            assert inside["workItem"]["assignee"] == "integration-test"

    def test_work_item_not_found(self):
        """Verify RuntimeError for non-existent work item."""
        with pytest.raises(RuntimeError):  # noqa: SIM117
            with StatusLifecycle("NONEXISTENT-999"):
                pass


if __name__ == "__main__":
    pytest.main(sys.argv)
