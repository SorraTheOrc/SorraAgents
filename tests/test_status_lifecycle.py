"""Unit tests for StatusLifecycle module (skill/shared/status_lifecycle.py).

Tests the update_status() static method, particularly the new optional
``needs_producer_review`` parameter (AC2), and the worklog-dir resolution
that makes ``wl`` work from inside git worktrees (SA-0MSGKAWXQ009VVG2).
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from skill.shared import status_lifecycle as status_lifecycle_module
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

        with pytest.raises(RuntimeError, match="wl command failed"):
            StatusLifecycle.update_status(
                "SA-NONEXIST", "completed", runner=fake_runner
            )


# ---------------------------------------------------------------------------
# Tests: worklog-dir resolution from inside git worktrees (SA-0MSGKAWXQ009VVG2)
# ---------------------------------------------------------------------------


@pytest.fixture
def worktree_project(tmp_path):
    """A git repo with an initialized .worklog in the MAIN checkout and a
    worktree under ``<main>/.worklog/worktrees/`` whose .worklog holds only
    the committed config.yaml (mimicking a real project worktree).
    """
    repo_root = tmp_path / "project"
    repo_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(repo_root), check=True, capture_output=True)
    (repo_root / "README.md").write_text("# Project")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"],
                   cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "branch", "dev"], cwd=str(repo_root), check=True,
                   capture_output=True)

    # Initialize .worklog in the MAIN checkout only
    main_worklog = repo_root / ".worklog"
    main_worklog.mkdir()
    (main_worklog / "initialized").write_text('{"version": "1.0.3"}')
    (main_worklog / "config.yaml").write_text("projectName: Test\n")

    # Worktree under the main checkout's .worklog/worktrees/ (as created by
    # `implement.py start`); its .worklog holds only config.yaml (not initialized)
    worktree_dir = (main_worklog / "worktrees" / "wl-test").resolve()
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--track", "-b",
         "wl-test", str(worktree_dir), "dev"],
        cwd=str(repo_root), check=True, capture_output=True,
    )
    wt_worklog = worktree_dir / ".worklog"
    wt_worklog.mkdir(exist_ok=True)
    (wt_worklog / "config.yaml").write_text("projectName: Test\n")

    return {
        "repo_root": repo_root.resolve(),
        "worktree_dir": worktree_dir,
    }


class TestWorklogDirResolutionFromWorktree:
    """wl commands must resolve the main checkout's initialized .worklog
    when run from inside a git worktree (whose own .worklog is not
    initialized). Without this, `implement.py finish` (run from the
    worktree as documented) fails with 'Worklog system is not initialized'.
    """

    def test_worklog_dir_flag_from_worktree_points_at_main(
        self, worktree_project, monkeypatch
    ):
        main = worktree_project["repo_root"]
        wt = worktree_project["worktree_dir"]
        monkeypatch.chdir(wt)

        flag = status_lifecycle_module.worklog_dir_flag()

        assert flag == ["--worklog-dir", str(main / ".worklog")], (
            f"Expected --worklog-dir flag for main checkout, got {flag}"
        )

    def test_worklog_dir_flag_empty_at_initialized_root(
        self, worktree_project, monkeypatch
    ):
        main = worktree_project["repo_root"]
        monkeypatch.chdir(main)

        flag = status_lifecycle_module.worklog_dir_flag()

        assert flag == [], (
            f"Expected empty flag at initialized project root, got {flag}"
        )

    def test_run_wl_injects_worklog_dir_from_worktree(
        self, worktree_project, monkeypatch
    ):
        main = worktree_project["repo_root"]
        wt = worktree_project["worktree_dir"]
        monkeypatch.chdir(wt)

        calls = []

        def fake_runner(cmd):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"success": True}), stderr="",
            )

        status_lifecycle_module.run_wl(
            ["wl", "show", "SA-X", "--json"], runner=fake_runner
        )

        assert calls, "Expected a wl command to run"
        assert calls[0][0] == "wl"
        assert calls[0][1] == "--worklog-dir"
        assert calls[0][2] == str(main / ".worklog"), (
            f"Expected --worklog-dir to point at the main checkout, got {calls[0]}"
        )

    def test_worklog_dir_flag_empty_when_only_config_worklog(
        self, tmp_path, monkeypatch
    ):
        """A config-only .worklog with no initialized ancestor must yield no
        flag (wl runs as-is and surfaces the real 'not initialized' error)."""
        d = tmp_path / "somedir"
        (d / ".worklog").mkdir(parents=True)
        (d / ".worklog" / "config.yaml").write_text("x: 1\n")
        monkeypatch.chdir(d)

        assert status_lifecycle_module.worklog_dir_flag() == []
