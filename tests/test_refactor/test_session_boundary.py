"""Tests for session boundary detection (git diff logic).

These tests define the expected interface of the session boundary detection
module that identifies files modified in the current implementation session.
They mock git commands for isolation.

Related work item: SA-0MQA70XZD007UGRU
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_fake_subprocess_run(responses: dict[str, subprocess.CompletedProcess]):
    """Build a fake subprocess.run that returns predefined responses.

    ``responses`` maps a cmd string (``" ".join(cmd)`` joined) to a
    ``subprocess.CompletedProcess`` instance.  If a command is not found the
    fake raises ``FileNotFoundError``.
    """

    def fake_run(
        cmd: list[str] | str,
        *args: Any,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        key = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if key not in responses:
            raise FileNotFoundError(f"Unexpected command: {key}")
        return responses[key]

    return fake_run


def _cp(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Shortcut to build a subprocess.CompletedProcess instance."""
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestModifiedFilesDetection:
    """Session boundary detection for modified files via git diff."""

    def test_detects_modified_files(self, monkeypatch):
        """A modified file shows up with status 'M'."""
        responses = {
            "git diff --name-status dev": _cp(stdout="M\tsrc/foo.py\nM\tsrc/bar.py"),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        # The session boundary module (to be implemented) will call
        # subprocess.run(["git", "diff", "--name-status", "dev"])
        # and parse the output.
        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert files == [
            {"status": "M", "file": "src/foo.py"},
            {"status": "M", "file": "src/bar.py"},
        ]

    def test_detects_added_files(self, monkeypatch):
        """An added file shows up with status 'A'."""
        responses = {
            "git diff --name-status dev": _cp(stdout="A\tsrc/new.py"),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert files == [{"status": "A", "file": "src/new.py"}]

    def test_detects_deleted_files(self, monkeypatch):
        """A deleted file shows up with status 'D'."""
        responses = {
            "git diff --name-status dev": _cp(stdout="D\tsrc/old.py"),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert files == [{"status": "D", "file": "src/old.py"}]

    def test_detects_renamed_files(self, monkeypatch):
        """A renamed file shows up with status 'R' and similarity."""
        responses = {
            "git diff --name-status dev": _cp(stdout="R100\tsrc/old.py\tsrc/new.py"),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert files == [
            {"status": "R100", "file": "src/new.py", "old_file": "src/old.py"},
        ]

    def test_returns_empty_list_when_no_changes(self, monkeypatch):
        """When there are no changes, an empty list is returned."""
        responses = {
            "git diff --name-status dev": _cp(stdout=""),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert files == []


class TestUntrackedFilesHandling:
    """Session boundary detection for untracked files."""

    def test_detects_untracked_files(self, monkeypatch):
        """Untracked files are returned separately."""
        responses = {
            "git ls-files --others --exclude-standard": _cp(
                stdout="src/untracked.py\ndocs/TODO.md"
            ),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_untracked_files

        files = get_untracked_files()
        assert files == ["src/untracked.py", "docs/TODO.md"]

    def test_returns_empty_list_when_no_untracked_files(self, monkeypatch):
        """When there are no untracked files, an empty list is returned."""
        responses = {
            "git ls-files --others --exclude-standard": _cp(stdout=""),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_untracked_files

        files = get_untracked_files()
        assert files == []

    def test_combined_changed_and_untracked(self, monkeypatch):
        """get_session_files combines changed and untracked files."""
        responses = {
            "git diff --name-status dev": _cp(
                stdout="M\tsrc/foo.py\nA\tsrc/new.py"
            ),
            "git ls-files --others --exclude-standard": _cp(
                stdout="src/untracked.py"
            ),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_session_files

        files = get_session_files(parent_branch="dev")
        assert files == [
            {"status": "M", "file": "src/foo.py"},
            {"status": "A", "file": "src/new.py"},
            {"status": "?", "file": "src/untracked.py"},
        ]


class TestMergeCommitHandling:
    """Handle merge commits correctly."""

    def test_merge_commit_changes_included(self, monkeypatch):
        """Changes from merge commits are included in the diff."""
        responses = {
            # Simulate diff against the merge base for accurate merge results
            "git merge-base dev HEAD": _cp(stdout="abc123\n"),
            "git diff --name-status abc123": _cp(
                stdout="M\tsrc/merged.py\nA\tsrc/from-merge.py"
            ),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert {"status": "M", "file": "src/merged.py"} in files
        assert {"status": "A", "file": "src/from-merge.py"} in files

    def test_no_merge_base_fallback_to_parent(self, monkeypatch):
        """When merge-base fails, fall back to direct diff against parent."""
        responses = {
            "git merge-base dev HEAD": _cp(returncode=128, stderr="fatal: not a valid commit"),
            "git diff --name-status dev": _cp(stdout="M\tsrc/fallback.py"),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert files == [{"status": "M", "file": "src/fallback.py"}]


class TestEdgeCases:
    """Edge cases for session boundary detection."""

    def test_all_files_changed(self, monkeypatch):
        """When all files are modified, they are all returned."""
        responses = {
            "git diff --name-status dev": _cp(
                stdout="M\tpyproject.toml\nM\tsrc/main.py\nM\ttests/test_main.py"
            ),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert len(files) == 3

    def test_binary_files_handled(self, monkeypatch):
        """Binary files are included in the changed list."""
        responses = {
            "git diff --name-status dev": _cp(
                stdout="M\tassets/logo.png\nM\tsrc/main.py"
            ),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert {"status": "M", "file": "assets/logo.png"} in files
        assert {"status": "M", "file": "src/main.py"} in files

    def test_tricky_filenames_with_spaces(self, monkeypatch):
        """Filenames with spaces are handled correctly."""
        responses = {
            "git diff --name-status dev": _cp(
                stdout='M\tsrc/my file.py\nA\t"src/with spaces/file.py"'
            ),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")

        # At minimum both files should be detected
        assert len(files) >= 1
        file_paths = [f["file"] for f in files]
        assert any("my file.py" in p for p in file_paths)

    def test_default_parent_branch_is_dev(self, monkeypatch):
        """When no parent branch is specified, default to 'dev'."""
        responses = {
            "git diff --name-status dev": _cp(stdout="M\tsrc/foo.py"),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        # Call without specifying parent_branch
        files = get_changed_files()
        assert files == [{"status": "M", "file": "src/foo.py"}]

    def test_git_command_failure_returns_empty(self, monkeypatch):
        """When git command fails, return empty list rather than crash."""
        responses = {
            "git diff --name-status dev": _cp(
                returncode=128, stderr="fatal: not a git repository"
            ),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import get_changed_files

        files = get_changed_files(parent_branch="dev")
        assert files == []

    def test_non_zero_status_detection(self, monkeypatch):
        """Verify implement detects changes via exit code."""
        responses = {
            "git diff --exit-code dev": _cp(returncode=0),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses))

        from skill.refactor.session_boundary import has_changes

        assert has_changes(parent_branch="dev") is False

        responses_with_changes = {
            "git diff --exit-code dev": _cp(returncode=1),
        }
        monkeypatch.setattr(subprocess, "run", _make_fake_subprocess_run(responses_with_changes))

        assert has_changes(parent_branch="dev") is True
