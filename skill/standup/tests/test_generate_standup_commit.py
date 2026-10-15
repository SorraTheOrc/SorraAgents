#!/usr/bin/env python3
"""Tests: generate_standup.py commits and pushes the report file it writes.

Covers SA-0MUDYRHVZ005PQP4:

1. A written report is committed (only that file) and pushed to the current
   branch's upstream — verified with a hermetic real-git sandbox
   (:mod:`skill.shared.git_sandbox`) under ``tmp_path``.
2. On ``main`` the report is committed locally but never pushed (warning).
3. ``--no-commit`` disables all git operations; ``--json`` to stdout writes no
   file and so performs no commit; an unchanged file is an idempotent no-op.
4. Every commit/push failure is non-fatal (warning only, exit code unchanged):
   non-git directory, unset author identity, missing upstream, offline push.

The tests never touch the live checkout: every repository is a sandbox under
``tmp_path`` and every git invocation goes through the hermetic runner.
"""  # noqa: EXE001
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from shared.git_sandbox import (
    add_remote,
    commit_all,
    init_bare_remote,
    init_repo,
    run_git,
    sanitized_git_env,
)

_MODULE_NAME = "generate_standup_commit"


# ── helpers ────────────────────────────────────────────────────────────


def _reload_standup():
    """Import a fresh generate_standup module from the repo skill tree."""
    os.environ.pop("WL_WORKLOG_DIR", None)
    spec_path = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git_runner(repo):
    """A hermetic git runner bound to *repo* (matches commit_report_file's API)."""

    def _run(args):
        proc = run_git(repo, args)
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()

    return _run


def _make_repo(tmp_path, branch="dev", with_remote=True):
    """Create a hermetic repo (optionally with an ``origin`` bare remote)."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    repo = init_repo(sandbox / "repo", default_branch=branch, sandbox_root=sandbox)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    commit_all(repo, "seed")
    if not with_remote:
        return repo, None
    remote = init_bare_remote(sandbox / "remote.git", sandbox_root=sandbox)
    add_remote(repo, "origin", str(remote))
    run_git(repo, ["push", "-u", "origin", branch], check=True)
    # Point the bare remote's HEAD at the pushed branch so a plain
    # ``show HEAD`` resolves (``git init --bare`` defaults to master).
    run_git(remote, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
    return repo, remote


def _write_report(repo, name="2026_10_14.md"):
    report = repo / "standups" / name
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Standup report\n", encoding="utf-8")
    return report


def _head_files(repo, ref="HEAD"):
    out = run_git(repo, ["show", "--name-only", "--pretty=format:", ref]).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _make_main_context(monkeypatch, repo, extra_argv=None):
    """Patch main()'s worklog fetch seams so main() runs hermetically."""
    mod = _reload_standup()
    worklog = repo / ".worklog"
    worklog.mkdir(exist_ok=True)
    monkeypatch.setattr(mod, "sync_worklog", lambda **_k: {"status": "skipped"})
    monkeypatch.setattr(mod, "generate_report", lambda **_k: {})
    monkeypatch.setattr(mod, "format_report", lambda _data, _count=None: "report body")
    argv = ["generate_standup.py", "--worklog-dir", str(worklog)]
    if extra_argv:
        argv[1:1] = extra_argv
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr("builtins.print", lambda *_a, **_k: None)
    return mod


# ── AC1: commit + push only the written file ───────────────────────────


class TestCommitAndPush:
    """AC1: commit exactly the report file and push the current branch."""

    def test_commits_and_pushes_only_the_report_file(self, tmp_path):
        repo, remote = _make_repo(tmp_path)
        # An unrelated staged change must NOT be swept into the commit.
        (repo / "unrelated.txt").write_text("wip\n", encoding="utf-8")
        run_git(repo, ["add", "--", "unrelated.txt"], check=True)

        report = _write_report(repo)
        status = _reload_standup().commit_report_file(
            str(report), str(repo), runner=_git_runner(repo)
        )

        assert status["status"] == "committed"
        assert status["pushed"] is True

        committed = _head_files(repo)
        assert "standups/2026_10_14.md" in committed
        assert "unrelated.txt" not in committed
        # The unrelated change is still staged and uncommitted.
        staged = run_git(repo, ["diff", "--cached", "--name-only"]).stdout
        assert "unrelated.txt" in staged
        # The push reached the remote.
        assert "standups/2026_10_14.md" in _head_files(remote)

    def test_explicit_output_path_is_committed(self, tmp_path, monkeypatch):
        repo, _remote = _make_repo(tmp_path)
        explicit = repo / "custom_report.md"
        mod = _make_main_context(
            monkeypatch, repo, ["--output-path", str(explicit)]
        )

        rc = mod.main()

        assert rc == 0
        assert explicit.is_file()
        assert "custom_report.md" in _head_files(repo)

    def test_relative_output_path_is_committed_from_cwd(self, tmp_path, monkeypatch):
        """A cwd-relative --output-path commits the exact file that was written."""
        repo, _remote = _make_repo(tmp_path)
        monkeypatch.chdir(repo)
        mod = _make_main_context(monkeypatch, repo, ["--output-path", "my_report.md"])

        rc = mod.main()

        assert rc == 0
        assert (repo / "my_report.md").is_file()
        assert "my_report.md" in _head_files(repo)


# ── AC2: main guard ────────────────────────────────────────────────────


class TestMainGuard:
    """AC2: on main, commit locally but never push."""

    def test_main_branch_commits_locally_but_does_not_push(self, tmp_path, capsys):
        repo, remote = _make_repo(tmp_path, branch="main")
        seed = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        report = _write_report(repo)

        status = _reload_standup().commit_report_file(
            str(report), str(repo), runner=_git_runner(repo)
        )

        assert status["status"] == "committed"
        assert status["pushed"] is False
        assert status["reason"] == "protected_branch"
        assert run_git(repo, ["rev-parse", "HEAD"]).stdout.strip() != seed
        # Remote main is unchanged.
        assert run_git(remote, ["rev-parse", "main"]).stdout.strip() == seed
        assert "protected branch" in capsys.readouterr().err


# ── AC3: escape hatches + idempotence ──────────────────────────────────


class TestNoCommitAndIdempotence:
    """AC3: --no-commit, --json stdout, and unchanged-file no-op."""

    def test_no_commit_flag_skips_all_git_operations(self, tmp_path, monkeypatch):
        repo, _remote = _make_repo(tmp_path)
        seed = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        mod = _make_main_context(monkeypatch, repo, ["--no-commit"])

        rc = mod.main()

        assert rc == 0
        today = datetime.now(tz=timezone.utc).astimezone().replace(tzinfo=None).strftime("%Y_%m_%d")
        assert (repo / "standups" / f"{today}.md").is_file()
        assert run_git(repo, ["rev-parse", "HEAD"]).stdout.strip() == seed

    def test_json_to_stdout_performs_no_commit(self, tmp_path, monkeypatch):
        repo, _remote = _make_repo(tmp_path)
        seed = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        mod = _make_main_context(monkeypatch, repo, ["--json"])

        rc = mod.main()

        assert rc == 0
        assert not (repo / "standups").exists()
        assert run_git(repo, ["rev-parse", "HEAD"]).stdout.strip() == seed

    def test_unchanged_report_is_idempotent_no_op(self, tmp_path):
        repo, _remote = _make_repo(tmp_path)
        report = _write_report(repo)
        mod = _reload_standup()

        first = mod.commit_report_file(str(report), str(repo), runner=_git_runner(repo))
        head_after_first = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        second = mod.commit_report_file(str(report), str(repo), runner=_git_runner(repo))

        assert first["status"] == "committed"
        assert second["status"] == "no_changes"
        assert run_git(repo, ["rev-parse", "HEAD"]).stdout.strip() == head_after_first


# ── AC4: non-fatal failures ────────────────────────────────────────────


def _no_identity_runner(repo):
    """A real git runner with global/system config and HOME neutralised."""

    def _run(args):
        env = sanitized_git_env()
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["HOME"] = str(repo / ".empty-home")
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()

    return _run


class TestNonFatalFailures:
    """AC4: every git failure is a warning, never an error."""

    def test_non_git_directory_is_non_fatal(self, tmp_path, capsys):
        plain = tmp_path / "plain"
        plain.mkdir()
        report = plain / "report.md"
        report.write_text("x\n", encoding="utf-8")

        status = _reload_standup().commit_report_file(
            str(report), str(plain), runner=_git_runner(plain)
        )

        assert status["status"] == "failed"
        assert report.is_file()
        assert "Warning" in capsys.readouterr().err

    def test_unset_author_identity_is_non_fatal(self, tmp_path, capsys):
        repo, _remote = _make_repo(tmp_path)
        run_git(repo, ["config", "--unset", "user.name"])
        run_git(repo, ["config", "--unset", "user.email"])
        report = _write_report(repo)

        status = _reload_standup().commit_report_file(
            str(report), str(repo), runner=_no_identity_runner(repo)
        )

        assert status["status"] == "failed"
        assert status["step"] == "commit"
        assert report.is_file()
        assert "Warning" in capsys.readouterr().err

    def test_missing_upstream_is_non_fatal(self, tmp_path, capsys):
        repo, _remote = _make_repo(tmp_path, with_remote=False)
        report = _write_report(repo)

        status = _reload_standup().commit_report_file(
            str(report), str(repo), runner=_git_runner(repo)
        )

        assert status["status"] == "committed"
        assert status["pushed"] is False
        assert status["reason"] == "push_failed"
        # The local decision/commit is still recorded.
        assert "standups/2026_10_14.md" in _head_files(repo)
        assert "Warning" in capsys.readouterr().err

    def test_unborn_empty_repo_is_non_fatal(self, tmp_path, capsys):
        """An unborn (no-commits) repo is born by the commit; push stays non-fatal."""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        repo = init_repo(sandbox / "fresh", default_branch="dev", sandbox_root=sandbox)
        report = _write_report(repo)

        status = _reload_standup().commit_report_file(
            str(report), str(repo), runner=_git_runner(repo)
        )

        assert status["status"] == "committed"
        assert status["pushed"] is False
        assert status["reason"] == "push_failed"
        assert "standups/2026_10_14.md" in _head_files(repo)
        assert "Warning" in capsys.readouterr().err

    def test_offline_push_is_non_fatal(self, tmp_path, capsys):
        repo, _remote = _make_repo(tmp_path)
        run_git(repo, ["remote", "set-url", "origin", str(tmp_path / "gone.git")], check=True)
        report = _write_report(repo)

        status = _reload_standup().commit_report_file(
            str(report), str(repo), runner=_git_runner(repo)
        )

        assert status["status"] == "committed"
        assert status["pushed"] is False
        assert status["reason"] == "push_failed"
        assert report.is_file()
        assert "Warning" in capsys.readouterr().err

    def test_main_default_path_is_committed_and_pushed(self, tmp_path, monkeypatch):
        """End-to-end: main() writes the dated file, commits it and pushes."""
        repo, remote = _make_repo(tmp_path)
        mod = _make_main_context(monkeypatch, repo)

        rc = mod.main()

        assert rc == 0
        today = datetime.now(tz=timezone.utc).astimezone().replace(tzinfo=None).strftime("%Y_%m_%d")
        rel = f"standups/{today}.md"
        assert (repo / rel).is_file()
        assert rel in _head_files(repo)
        assert rel in _head_files(remote)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
