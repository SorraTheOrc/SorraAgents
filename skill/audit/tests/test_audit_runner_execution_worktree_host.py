"""SA-0MWO59J9T002IQ63: the F3 execution worktree follows the code root.

For a cross-repo audit (``issue <id> --code-root <CODE_REPO>``) the execution
worktree is a worktree of the **code** repository, so it must be hosted under
that repository's ``.worklog/worktrees`` — hosting it under the worklog owner
puts it inside the *other* project's checkout, where any test that resolves
the repository root or the worklog by walking up from its cwd resolves the
wrong project and fails.

Observed incident (2026-11-19, ``CUR-0MWIBGLIL00774EU``): the suite run from
``games_curator/.worklog/worktrees/audit-exec-CUR-…`` failed
``skill/shared/tests/test_resolve_repo_root.py::…test_framework_root_resolves_to_main_checkout_here``
and ``tests/test_timing_integration.py::…test_render_report_emits_timing_on_stderr``
(``render_report.py`` resolved ``games_curator``'s worklog and reported
``Work item not found``). Both pass in a ``SorraAgents``-nested worktree.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner

# Capture the real helpers before the package-wide autouse fixture neutralises
# them (``skill/audit/tests/conftest.py::_neutralise_execution_worktree_git``),
# so this module can exercise the real worktree-creation logic.
_REAL_CREATE = audit_runner._create_execution_worktree
_REAL_REMOVE = audit_runner._remove_execution_worktree


@pytest.fixture(autouse=True)
def _use_real_worktree_helpers():
    """Re-enable the real execution-worktree helpers for this module."""
    with (
        mock.patch.object(
            audit_runner, "_create_execution_worktree", _REAL_CREATE,
        ),
        mock.patch.object(
            audit_runner, "_remove_execution_worktree", _REAL_REMOVE,
        ),
    ):
        yield

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _fake_git_runner(calls: list[list[str]]):
    """A runner that simulates ``git worktree add`` (creates the directory).

    ``origin/dev`` resolves to a real 40-char sha so the creation guard is
    satisfied, and ``git worktree add --detach <path>`` materialises the path
    with a ``.git`` file — exactly what the caller checks before accepting the
    worktree as the execution base.
    """

    def runner(cmd):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "fetch", "origin"]:
            return _ok("")
        if cmd[:2] == ["git", "rev-parse"] and "--verify" in cmd:
            return _ok("a" * 40 + "\n")
        if cmd[:4] == ["git", "worktree", "add", "--detach"]:
            target = Path(cmd[4])
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").write_text("gitdir: /nowhere\n")
            return _ok("")
        return _ok("")

    return runner


# ---------------------------------------------------------------------------
# 1. Host-root resolution (AC1 / AC3)
# ---------------------------------------------------------------------------

class TestExecutionWorktreeHost:
    def test_code_root_hosts_the_execution_worktree(self):
        """AC1: a supplied code root is the host root."""
        assert audit_runner._execution_worktree_host(
            "/projects/code-repo", Path("/projects/worklog-owner"),
        ) == Path("/projects/code-repo")

    def test_owning_root_hosts_the_execution_worktree_by_default(self):
        """AC3: with no code root the owning root is the host root — the
        same-repo behaviour is unchanged."""
        owning = Path("/projects/worklog-owner")
        assert audit_runner._execution_worktree_host(None, owning) == owning
        assert audit_runner._execution_worktree_host("", owning) == owning


# ---------------------------------------------------------------------------
# 2. Worktree creation follows the host root (AC1 / AC2)
# ---------------------------------------------------------------------------

class TestCreateExecutionWorktree:
    def test_cross_repo_worktree_is_created_under_the_code_root(self, tmp_path):
        """AC1/AC2: with a code root the worktree path is
        ``<code-root>/.worklog/worktrees/audit-exec-<id>`` — never under the
        worklog owner — so repo-root/worklog resolution inside the suite
        reaches the code repository."""
        owning_root = tmp_path / "worklog-owner"
        code_root = tmp_path / "code-repo"
        owning_root.mkdir()
        code_root.mkdir()
        calls: list[list[str]] = []

        with mock.patch.object(
            audit_runner, "_ensure_worktree_submodules",
            return_value=True,
        ):
            path, evidence = audit_runner._create_execution_worktree(
                owning_root, _fake_git_runner(calls), "OSL-1",
                code_root=code_root,
            )

        assert path is not None, evidence
        expected = code_root / ".worklog" / "worktrees" / "audit-exec-OSL-1"
        assert path == expected
        assert not str(path).startswith(str(owning_root)), (
            "the execution worktree must not be hosted inside the worklog "
            f"owner (got {path})"
        )
        # The worktree is registered with git at that path.
        add_calls = [c for c in calls if c[:4] ==
                     ["git", "worktree", "add", "--detach"]]
        assert add_calls and add_calls[0][4] == str(expected)

    def test_same_repo_worktree_path_is_unchanged(self, tmp_path):
        """AC3: without a code root the worktree stays under the owning root
        (byte-identical to the pre-change behaviour)."""
        owning_root = tmp_path / "worklog-owner"
        owning_root.mkdir()
        calls: list[list[str]] = []

        with mock.patch.object(
            audit_runner, "_ensure_worktree_submodules",
            return_value=True,
        ):
            path, evidence = audit_runner._create_execution_worktree(
                owning_root, _fake_git_runner(calls), "OSL-2",
            )

        assert path == (
            owning_root / ".worklog" / "worktrees" / "audit-exec-OSL-2"
        ), evidence

    def test_node_modules_symlink_comes_from_the_host_root(self, tmp_path):
        """AC1: the shared ``node_modules`` symlink is taken from the host
        root (the code repo for a cross-repo audit), never the other
        project's install."""
        owning_root = tmp_path / "worklog-owner"
        code_root = tmp_path / "code-repo"
        owning_root.mkdir()
        (code_root / "node_modules").mkdir(parents=True)
        calls: list[list[str]] = []

        with mock.patch.object(
            audit_runner, "_ensure_worktree_submodules",
            return_value=True,
        ):
            path, evidence = audit_runner._create_execution_worktree(
                owning_root, _fake_git_runner(calls), "OSL-3",
                code_root=code_root,
            )

        assert path is not None, evidence
        link = path / "node_modules"
        assert link.is_symlink(), evidence
        assert Path(link).resolve() == (code_root / "node_modules").resolve()


# ---------------------------------------------------------------------------
# 3. Cleanup still removes the worktree (AC4)
# ---------------------------------------------------------------------------

class TestRemoveExecutionWorktree:
    def test_remove_issues_git_worktree_remove(self, tmp_path):
        """AC4: the execution worktree is removed after the run regardless of
        which root hosted it."""
        calls: list[list[str]] = []
        worktree = tmp_path / "code-repo" / ".worklog" / "worktrees" / "audit-exec-OSL-4"

        def runner(cmd):
            calls.append(list(cmd))
            return _ok("")

        audit_runner._remove_execution_worktree(
            tmp_path / "code-repo", runner, str(worktree),
        )

        assert ["git", "worktree", "remove", "--force", str(worktree)] in calls

    def test_remove_is_a_noop_without_a_worktree(self, tmp_path):
        calls: list[list[str]] = []
        audit_runner._remove_execution_worktree(
            tmp_path, lambda cmd: calls.append(list(cmd)), "",
        )
        assert calls == []

    def test_remove_failure_is_logged_and_fail_open(self, tmp_path, capsys):
        """S110 (SA-0MWQKFSUR0041FXV): a failing cleanup is reported on stderr
        rather than silently swallowed, and never aborts the audit."""
        worktree = tmp_path / ".worklog" / "worktrees" / "audit-exec-OSL-5"

        def runner(cmd):
            raise RuntimeError("git worktree remove exploded")

        audit_runner._remove_execution_worktree(tmp_path, runner, str(worktree))

        err = capsys.readouterr().err
        assert "Execution worktree cleanup failed" in err
        assert str(worktree) in err
        assert "git worktree remove exploded" in err
