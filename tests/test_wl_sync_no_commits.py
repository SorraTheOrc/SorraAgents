#!/usr/bin/env python3
"""Integration regression tests for `wl sync` on a repo with no commits.

Covers SA-0MSG57UNY009DE51 / SA-0MSGH57JL001OZER:
  - ``wl sync`` on a git repo with no commits fails with a message naming
    the cause (no commits yet) and the remedy (initial commit or
    ``--no-push``).
  - The ``--no-push`` remedy succeeds.
  - Normal sync behavior on repos with commits is unchanged (proceeds past
    worktree creation; a missing remote then fails at the git push stage —
    not with the no-commits message).
"""  # noqa: EXE001
from __future__ import annotations

import subprocess


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=120,
    )


def _init_no_commit_repo(repo) -> None:
    """Create a git repo with NO commits and an initialized worklog store."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=repo, check=True, capture_output=True)

    init = _run([
        "wl", "init",
        "--project-name", "No Commit Test",
        "--prefix", "NCT",
        "--auto-export", "no",
        "--auto-sync", "no",
        "--agents-template", "skip",
        "--json",
    ], cwd=str(repo))
    assert init.returncode == 0, f"wl init failed: {init.stdout} {init.stderr}"

    create = _run([
        "wl", "create",
        "--title", "Temp sync item",
        "--description", "Auto-created by test_wl_sync_no_commits.py",
        "--priority", "low",
        "--issue-type", "task",
        "--json",
    ], cwd=str(repo))
    assert create.returncode == 0, f"wl create failed: {create.stdout} {create.stderr}"


def test_wl_sync_no_commits_fails_with_actionable_message(tmp_path):
    """AC1: wl sync on a no-commit repo fails naming the cause and remedy."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_no_commit_repo(repo)

    sync = _run(["wl", "sync"], cwd=str(repo))
    assert sync.returncode != 0, "wl sync should fail on a repo with no commits"
    combined = sync.stdout + sync.stderr
    assert "no commits yet" in combined, f"cause not named: {combined}"
    assert "git commit --allow-empty" in combined, f"remedy missing: {combined}"
    assert "--no-push" in combined, f"--no-push remedy missing: {combined}"


def test_wl_sync_no_push_remedy_succeeds(tmp_path):
    """AC1: the --no-push remedy keeps worklog data local and succeeds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_no_commit_repo(repo)

    no_push = _run(["wl", "sync", "--no-push"], cwd=str(repo))
    assert no_push.returncode == 0, (
        f"wl sync --no-push should succeed: {no_push.stdout} {no_push.stderr}"
    )


def test_wl_sync_after_initial_commit_is_unchanged(tmp_path):
    """AC2: with an initial commit, the no-commits guard never fires — sync
    proceeds and any failure is the pre-existing downstream behavior (e.g. a
    missing origin remote fails at the git push stage), never the
    no-commits message.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_no_commit_repo(repo)

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "commit", "-q", "-m", "chore: initial"],
        cwd=repo, check=True, capture_output=True,
    )
    assert commit.returncode == 0

    sync = _run(["wl", "sync"], cwd=str(repo))
    combined = sync.stdout + sync.stderr
    assert "no commits yet" not in combined, (
        f"no-commits message must not appear after an initial commit: {combined}"
    )
    # The temp repo has no origin remote, so push cannot succeed; the exact
    # downstream failure (git push vs environment-specific worktree behavior)
    # is not what this test guards — only that the no-commits guard is inert.
    assert sync.returncode != 0
