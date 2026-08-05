"""Tests: intake.py wl invocations are independent of the caller's cwd.

Covers the acceptance criteria for SA-0MS93J0ZC007IO8V:
  - ``intake.py start|finish|abort`` succeed when invoked from a cwd that is
    not a worklog project root (via ``--worklog-dir`` injection)
  - when ``wl`` fails, the raised error includes the underlying error detail
    (stdout JSON error and/or stderr — no more empty error strings)
  - no behaviour change to the statuses set by ``intake.py finish``
    (``open`` + ``intake_complete``)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.intake.scripts import intake


@pytest.fixture(autouse=True)
def _neutralize_prefix_scan():
    """Neutralize the prefix-to-sibling scan for deterministic unit tests.

    The shared resolution (SA-0MSG57UNY009DE51) would otherwise scan the
    real sibling-projects directory for a config matching the fake
    ``TEST-123`` prefix; no real project matches, so the cwd-chain fallback
    (``worklog_dir_flag``) is what these tests exercise. Patching the scan
    keeps the tests hermetic regardless of which projects exist on the host.
    """
    with mock.patch(
        "skill.shared.status_lifecycle._find_worklog_dir_by_prefix",
        return_value=None,
    ):
        yield


def _ok_proc(cmd):
    """A successful wl CompletedProcess returning success JSON."""
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"success": True}), "")


class TestIntakeCwdIndependence:
    """Unit tests: intake.py routes wl through the shared run_wl helper."""

    def test_description_update_injects_worklog_dir(self):
        """When a worklog dir is resolved, --worklog-dir is added to wl update."""
        with (
            mock.patch("skill.shared.status_lifecycle.subprocess.run") as m,
            mock.patch(
                "skill.shared.status_lifecycle.worklog_dir_flag",
                return_value=["--worklog-dir", "/fake/proj/.worklog"],
            ),
        ):
            m.return_value = _ok_proc(["wl"])
            intake._run_wl_update_description("TEST-123", "/tmp/draft.md")
            cmd = m.call_args[0][0]
            assert cmd[:4] == ["wl", "--worklog-dir", "/fake/proj/.worklog", "update"]
            assert "--description-file" in cmd
            assert "/tmp/draft.md" in cmd

    def test_description_update_failure_surfaces_detail(self):
        """wl failure detail (stdout JSON error) propagates in the RuntimeError."""
        with mock.patch("skill.shared.status_lifecycle.subprocess.run") as m:
            m.return_value = subprocess.CompletedProcess(
                ["wl"],
                1,
                stdout=json.dumps({
                    "success": False,
                    "error": "Worklog system is not initialized",
                }),
                stderr="",
            )
            with pytest.raises(RuntimeError, match="Worklog system is not initialized"):
                intake._run_wl_update_description("TEST-123", "/tmp/draft.md")

    def test_cmd_start_injects_worklog_dir(self):
        """cmd_start (via StatusLifecycle.update_status) gets the flag too."""
        with (
            mock.patch("skill.shared.status_lifecycle.subprocess.run") as m,
            mock.patch(
                "skill.shared.status_lifecycle.worklog_dir_flag",
                return_value=["--worklog-dir", "/fake/proj/.worklog"],
            ),
        ):
            m.return_value = _ok_proc(["wl"])
            result = intake.cmd_start("TEST-123", assignee="Map")
            assert result == {"success": True, "action": "started", "item_id": "TEST-123"}
            cmd = m.call_args[0][0]
            assert cmd[:4] == ["wl", "--worklog-dir", "/fake/proj/.worklog", "update"]
            assert "--status" in cmd and "in_progress" in cmd

    def test_cmd_abort_injects_worklog_dir(self):
        """cmd_abort (via StatusLifecycle.update_status) gets the flag too."""
        with (
            mock.patch("skill.shared.status_lifecycle.subprocess.run") as m,
            mock.patch(
                "skill.shared.status_lifecycle.worklog_dir_flag",
                return_value=["--worklog-dir", "/fake/proj/.worklog"],
            ),
        ):
            m.return_value = _ok_proc(["wl"])
            result = intake.cmd_abort("TEST-123")
            assert result == {"success": True, "action": "aborted", "item_id": "TEST-123"}
            cmd = m.call_args[0][0]
            assert cmd[:4] == ["wl", "--worklog-dir", "/fake/proj/.worklog", "update"]
            assert "--status" in cmd and "open" in cmd

    def test_cmd_finish_preserves_statuses(self):
        """finish still sets open + intake_complete (no behaviour change)."""
        with (
            mock.patch("skill.shared.status_lifecycle.subprocess.run") as m,
            mock.patch(
                "skill.shared.status_lifecycle.worklog_dir_flag",
                return_value=[],
            ),
        ):
            m.return_value = _ok_proc(["wl"])
            result = intake.cmd_finish("TEST-123")
            assert result == {"success": True, "action": "finished", "item_id": "TEST-123"}
            cmd = m.call_args[0][0]
            assert cmd[:2] == ["wl", "update"]
            assert "--status" in cmd and "open" in cmd
            assert "--stage" in cmd and "intake_complete" in cmd


# ===========================================================================
# Integration tests (real wl CLI, optional)
# ===========================================================================

@pytest.mark.skipif(
    not bool(os.environ.get("RUN_INTEGRATION_TESTS")),
    reason="Integration tests require RUN_INTEGRATION_TESTS=1",
)
class TestIntakeCwdIntegration:
    """Real-CLI tests: intake.py works from a cwd that is not the project root.

    A temp dir with a symlinked ``.worklog`` simulates a non-project cwd while
    still letting ``wl`` resolve the real worklog.
    """

    @pytest.fixture
    def scratch_item(self):
        """Create a temporary work item and clean it up afterward."""
        result = subprocess.run(
            [
                "wl", "create",
                "--title", "Temp item (intake cwd integration)",
                "--description", "Auto-created by test_intake_scripts_cwd.py",
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
        subprocess.run(  # noqa: PLW1510
            ["wl", "update", item_id, "--status", "completed", "--json"],
            capture_output=True,
        )

    def test_intake_abort_from_non_project_cwd(self, scratch_item, tmp_path):
        """intake.py abort succeeds when run from a cwd without its own worklog."""
        item_id = scratch_item

        # Symlink the repo's .worklog into the temp dir (non-project cwd)
        repo_worklog = REPO_ROOT / ".worklog"
        assert repo_worklog.is_dir()
        (tmp_path / ".worklog").symlink_to(repo_worklog, target_is_directory=True)

        script = REPO_ROOT / "skill" / "intake" / "scripts" / "intake.py"
        proc = subprocess.run(
            ["python3", str(script), "abort", item_id],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"intake abort failed: {proc.stdout} {proc.stderr}"
        result = json.loads(proc.stdout)
        assert result.get("success") is True
        assert result.get("action") == "aborted"
