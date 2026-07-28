"""Unit tests for StatusLifecycle module (skill/shared/status_lifecycle.py).

Tests the update_status() static method, particularly the new optional
``needs_producer_review`` parameter (AC2).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from skill.shared.status_lifecycle import StatusLifecycle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Tests: update_status with needs_producer_review
# ---------------------------------------------------------------------------


class TestStatusLifecycleUpdateStatus:
    """Unit tests for StatusLifecycle.update_status()."""

    def test_update_status_backward_compatible_no_needs(self):
        """Calling update_status without needs_producer_review must NOT add
        --needs-producer-review to the wl command (backward compatibility)."""
        calls = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            calls.append(cmd_list)
            return _fake_proc(stdout=json.dumps({"success": True}))

        StatusLifecycle.update_status("SA-TEST1", "completed", runner=fake_runner)

        assert len(calls) >= 1, "Expected at least one wl update call"
        for cmd_list in calls:
            assert "--needs-producer-review" not in cmd_list, (
                f"Backward-compatible call should not include --needs-producer-review, got: {cmd_list}"
            )

    def test_update_status_with_needs_true(self):
        """Calling update_status with needs_producer_review=True must include
        --needs-producer-review yes."""
        calls = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            calls.append(cmd_list)
            return _fake_proc(stdout=json.dumps({"success": True}))

        StatusLifecycle.update_status(
            "SA-TEST2", "completed", needs_producer_review=True, runner=fake_runner
        )

        assert len(calls) >= 1, "Expected at least one wl update call"
        found = False
        for cmd_list in calls:
            if "--needs-producer-review" in cmd_list:
                idx = cmd_list.index("--needs-producer-review")
                assert cmd_list[idx + 1] == "yes", (
                    f"--needs-producer-review value should be 'yes', got: {cmd_list}"
                )
                found = True
        assert found, "Expected --needs-producer-review in wl update command"

    def test_update_status_with_needs_false(self):
        """Calling update_status with needs_producer_review=False must include
        --needs-producer-review no."""
        calls = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            calls.append(cmd_list)
            return _fake_proc(stdout=json.dumps({"success": True}))

        StatusLifecycle.update_status(
            "SA-TEST3", "completed", needs_producer_review=False, runner=fake_runner
        )

        assert len(calls) >= 1, "Expected at least one wl update call"
        found = False
        for cmd_list in calls:
            if "--needs-producer-review" in cmd_list:
                idx = cmd_list.index("--needs-producer-review")
                assert cmd_list[idx + 1] == "no", (
                    f"--needs-producer-review value should be 'no', got: {cmd_list}"
                )
                found = True
        assert found, "Expected --needs-producer-review in wl update command"

    def test_update_status_with_needs_and_stage(self):
        """Calling update_status with needs_producer_review=True and stage must
        include both --needs-producer-review yes and --stage in_review."""
        calls = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            calls.append(cmd_list)
            return _fake_proc(stdout=json.dumps({"success": True}))

        StatusLifecycle.update_status(
            "SA-TEST4",
            "completed",
            stage="in_review",
            needs_producer_review=True,
            runner=fake_runner,
        )

        assert len(calls) >= 1, "Expected at least one wl update call"
        cmd_list = calls[0]
        assert "--stage" in cmd_list, f"Expected --stage in command, got: {cmd_list}"
        assert "--needs-producer-review" in cmd_list, (
            f"Expected --needs-producer-review in command, got: {cmd_list}"
        )
        stage_idx = cmd_list.index("--stage")
        assert cmd_list[stage_idx + 1] == "in_review", (
            f"--stage should be 'in_review', got: {cmd_list}"
        )
        npr_idx = cmd_list.index("--needs-producer-review")
        assert cmd_list[npr_idx + 1] == "yes", (
            f"--needs-producer-review should be 'yes', got: {cmd_list}"
        )

    def test_update_status_raises_on_wl_failure(self):
        """Calling update_status must raise RuntimeError when wl command fails."""

        def fake_runner(cmd, **kwargs):
            return _fake_proc(returncode=1, stderr="wl: work item not found")

        import pytest

        with pytest.raises(RuntimeError, match="wl command failed"):
            StatusLifecycle.update_status(
                "SA-NONEXIST", "completed", runner=fake_runner
            )
