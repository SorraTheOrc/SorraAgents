"""Regression tests for worktree "has changes" detection (SA-0MVRN5Q67005H3M5).

Contract under test:

- ``_git_path_has_changes()`` reports changes only when the worktree holds dirty
  files or **commits ahead of** the parent branch. A worktree that is merely
  *behind* the parent branch contains no work, so ``phase_parent`` must return
  that child as the next child to implement instead of running ``finish`` on it
  (AC1-AC3).
- The placement guards keep the documented contract
  (``docs/dev/worktree-isolation.md`` §"The placement guard"): a non-terminal
  child whose worktree holds no work while the main checkout holds uncommitted
  changes fails closed, and a worktree that holds the work never triggers the
  guard (AC4).

Every repository used here is a throwaway one created under ``tmp_path`` —
never the live checkout. All fixture git commands run through
:mod:`shared.git_sandbox`, which supplies a hermetic environment (its own
``GIT_INDEX_FILE``, repository overrides scrubbed, pinned identity): the finish
gate runs this suite against a temporary index, and a fixture that staged into
an inherited index would invalidate the gate's would-be-commit tree evidence.
The package ``conftest.py`` removes an ambient ``GIT_INDEX_FILE`` for the same
reason, so the production helpers under test (``git_status`` / ``git rev-list``)
read the fixture repositories' own index.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = REPO_ROOT / "skill"
for _path in (str(REPO_ROOT), str(_SKILLS_ROOT)):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

from shared import git_sandbox as gs

from skill.implement.scripts import implement

PARENT_BRANCH = "dev"
CHILD_ID = "SA-CHILD0000000001"
PARENT_ID = "SA-PARENT00000001"


# ---------------------------------------------------------------------------
# Git fixture helpers (hermetic — see the module docstring)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return gs.run_git(cwd, args, check=True).stdout


def _init_repo(root: Path) -> None:
    gs.init_repo(root, default_branch=PARENT_BRANCH)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    gs.commit_all(root, "base")


def _commit(root: Path, name: str, text: str = "change\n") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    gs.commit_all(root, f"add {name}")


def _add_worktree(root: Path, worktree: Path) -> Path:
    """Create a linked worktree on its own branch forked from the parent."""
    worktree.parent.mkdir(parents=True, exist_ok=True)
    return gs.add_worktree(root, worktree, "wl-child", base=PARENT_BRANCH)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway main checkout on ``dev`` with one commit."""
    root = tmp_path / "repo"
    _init_repo(root)
    return root


@pytest.fixture()
def worktree(repo: Path, tmp_path: Path) -> Path:
    """A clean child worktree forked from ``dev`` at the fork base."""
    return _add_worktree(repo, tmp_path / "wt" / "wl-child")


# ---------------------------------------------------------------------------
# AC1/AC3: the "has changes" decision itself
# ---------------------------------------------------------------------------


class TestGitPathHasChanges:
    def test_clean_worktree_at_fork_base_has_no_changes(self, worktree: Path) -> None:
        assert implement._git_path_has_changes(worktree, PARENT_BRANCH) is False

    def test_clean_worktree_behind_parent_branch_has_no_changes(
        self, repo: Path, worktree: Path
    ) -> None:
        # Unrelated work lands on ``dev`` while the child session does nothing:
        # HEAD differs from ``dev``, but the worktree holds no work (AC1).
        _commit(repo, "unrelated-on-dev.txt")

        assert implement._git_path_has_changes(worktree, PARENT_BRANCH) is False

    def test_dirty_worktree_has_changes(self, worktree: Path) -> None:
        (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")

        assert implement._git_path_has_changes(worktree, PARENT_BRANCH) is True

    def test_worktree_with_commits_ahead_has_changes(self, worktree: Path) -> None:
        _commit(worktree, "child-work.txt")

        assert implement._git_path_has_changes(worktree, PARENT_BRANCH) is True

    def test_worktree_ahead_then_rewound_is_clean(self, worktree: Path) -> None:
        # A committed-then-reset worktree is back at its fork base: no work.
        base = _git(worktree, "rev-parse", "HEAD").strip()
        _commit(worktree, "child-work.txt")
        _git(worktree, "reset", "-q", "--hard", base)

        assert implement._git_path_has_changes(worktree, PARENT_BRANCH) is False

    def test_worklog_only_dirt_has_no_changes(self, worktree: Path) -> None:
        worklog_file = worktree / ".worklog" / "worklog.db"
        worklog_file.parent.mkdir(parents=True, exist_ok=True)
        worklog_file.write_text("binary-ish\n", encoding="utf-8")

        assert implement._git_path_has_changes(worktree, PARENT_BRANCH) is False

    def test_unresolvable_parent_branch_fails_open(self, worktree: Path) -> None:
        # No work at all, but the ahead-count cannot be computed: fail open so
        # a real change is never silently discarded.
        assert implement._git_path_has_changes(worktree, "no-such-branch") is True


# ---------------------------------------------------------------------------
# AC2: phase_parent must not finish a behind-only child
# ---------------------------------------------------------------------------


def _child(status: str = "open") -> dict:
    return {
        "id": CHILD_ID,
        "title": "Child work item",
        "status": status,
        "stage": "intake_complete",
        "assignee": "",
        "priority": "high",
        "sortIndex": 1000,
    }


def _run_phase_parent(worktree_path: Path, *, status: str = "open"):
    """Drive ``phase_parent`` against *worktree_path*, recording subprocesses."""
    invoked: list[tuple[str, str]] = []
    status_calls: list[tuple] = []

    def _fake_invoke(action, work_item_id, **_kwargs):
        invoked.append((action, work_item_id))
        if action == "finish":
            return {"success": True, "work_item_id": work_item_id}
        raise AssertionError(f"unexpected subprocess action {action}")

    def _fake_update_status(work_item_id, status, stage=None, assignee=None, **_kw):
        status_calls.append((work_item_id, status, stage, assignee))
        return {"success": True, "workItem": {"id": work_item_id, "status": status}}

    with (
        mock.patch.object(implement, "is_code_freeze_active", return_value=False),
        mock.patch.object(
            implement,
            "wl_show",
            return_value={"id": PARENT_ID, "title": "Parent", "status": "plan_complete"},
        ),
        mock.patch.object(
            implement, "wl_show_children", return_value=[_child(status)]
        ),
        mock.patch.object(implement, "wl_dep_blockers", return_value=[]),
        mock.patch.object(
            implement,
            "_outbound_gate",
            return_value={
                "blocked": False,
                "blockers": [],
                "coordination_blockers": [],
            },
        ),
        mock.patch.object(
            implement, "_discover_worktree", return_value=str(worktree_path)
        ),
        mock.patch.object(
            implement, "_invoke_implement", side_effect=_fake_invoke
        ) as invoke_mock,
        mock.patch.object(
            implement.StatusLifecycle,
            "update_status",
            side_effect=_fake_update_status,
        ),
        mock.patch.object(implement, "wl_add_comment", return_value=True),
    ):
        report = implement.phase_parent(PARENT_ID, json_output=True)

    report["_invoked"] = invoked
    report["_status_calls"] = status_calls
    report["_invoke_mock"] = invoke_mock.call_args_list
    return report


class TestPhaseParentBehindOnlyChild:
    def test_behind_only_child_is_next_child_not_finished(
        self, repo: Path, worktree: Path
    ) -> None:
        # Unrelated commits land on ``dev`` while the child session did nothing
        # (the 2026-10-19 shape): the worktree is behind, not dirty, not ahead.
        _commit(repo, "unrelated-on-dev.txt")

        report = _run_phase_parent(worktree)

        assert report["_invoked"] == []
        assert report["next_child"] == CHILD_ID
        assert report["worktree_path"] == str(worktree)
        assert "finish" not in [action for action, _ in report["_invoked"]]

    def test_child_with_commits_ahead_is_still_finished(
        self, worktree: Path
    ) -> None:
        # Non-vacuity: the same harness still finishes a child that did work.
        _commit(worktree, "child-work.txt")

        report = _run_phase_parent(worktree)

        assert report["_invoked"] == [("finish", CHILD_ID)]
        assert report.get("next_child") is None


# ---------------------------------------------------------------------------
# AC4: the documented placement-guard contract is preserved
# ---------------------------------------------------------------------------


class TestPlacementGuardContract:
    def test_behind_only_child_with_dirty_main_checkout_fails_closed(
        self, repo: Path, worktree: Path
    ) -> None:
        # The work definitely did not land in the child's worktree: no dirty
        # files, no commits ahead — while the main checkout is dirty.
        _commit(repo, "unrelated-on-dev.txt")
        (repo / "leaked.txt").write_text("written by the child\n", encoding="utf-8")

        violation = implement._child_main_checkout_violation(
            CHILD_ID, str(worktree), PARENT_BRANCH
        )

        assert violation is not None
        assert CHILD_ID in violation
        assert "leaked.txt" in violation

    def test_worktree_holding_the_work_never_triggers_the_guard(
        self, repo: Path, worktree: Path
    ) -> None:
        # Unrelated main-checkout dirt plus real work in the worktree: the work
        # lives where it belongs, so the guard stays silent.
        (repo / "leaked.txt").write_text("pre-existing dirt\n", encoding="utf-8")
        _commit(worktree, "child-work.txt")

        assert (
            implement._child_main_checkout_violation(
                CHILD_ID, str(worktree), PARENT_BRANCH
            )
            is None
        )

    def test_phase_finish_guard_flags_behind_only_worktree(
        self, repo: Path, worktree: Path
    ) -> None:
        _commit(repo, "unrelated-on-dev.txt")
        (repo / "leaked.txt").write_text("written by the child\n", encoding="utf-8")

        violation = implement._worktree_placement_violation(
            str(worktree), cwd=str(repo), repo_root=str(repo)
        )

        assert violation is not None
        assert str(repo) in violation

    def test_phase_finish_guard_ignores_worktree_holding_the_work(
        self, repo: Path, worktree: Path
    ) -> None:
        (repo / "leaked.txt").write_text("pre-existing dirt\n", encoding="utf-8")
        _commit(worktree, "child-work.txt")

        assert (
            implement._worktree_placement_violation(
                str(worktree), cwd=str(repo), repo_root=str(repo)
            )
            is None
        )

    def test_clean_main_checkout_never_triggers_the_guard(
        self, worktree: Path
    ) -> None:
        assert (
            implement._child_main_checkout_violation(
                CHILD_ID, str(worktree), PARENT_BRANCH
            )
            is None
        )
