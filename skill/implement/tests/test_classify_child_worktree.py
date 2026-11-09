"""Regression tests for the child work-state classifier (SA-0MW74IKJE008JXDQ).

Contract under test (parent SA-0MVYYL7X9008MGA1 AC4/AC5):

- ``_classify_child_worktree()`` distinguishes the change surfaces that
  ``_has_worktree_changes()`` collapses into one boolean:

  * ``"work"`` — uncommitted non-state files, or commits ahead of the parent
    branch whose messages reference the child id;
  * ``"verification-only"`` — no real work but the state file's
    ``verification_only`` opt-in is set;
  * ``"no-work"`` — a misleading surface only (the state file is the sole
    dirty path, or the commits ahead are not attributable to the item);
  * ``"clean"`` — nothing at all.

- ``phase_parent`` never presents ``no-work`` as finishable: the child is
  reported as ``not-finalisable`` in ``children_processed``, ``finish`` is
  never invoked for it, and the parent is **not** advanced to
  ``completed``/``in_review`` (AC4). A child with a genuine attributable
  commit ahead or real uncommitted work is still finalised exactly as today
  (AC5 non-vacuity).

Every repository here is a throwaway one created under ``tmp_path`` and built
through :mod:`shared.git_sandbox` (hermetic environment, pinned identity,
fixture-local index) — never the live checkout. The fixture ``.gitignore``
ignores ``.implement_state.json`` exactly as the real repository does, so the
"accidentally tracked" shape is reproduced by force-adding the file.
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
    # Mirror the real repository: the state file is gitignored, so the
    # "accidentally tracked" shape is an explicit ``git add -f`` in a test.
    (root / ".gitignore").write_text(".implement_state.json\n", encoding="utf-8")
    gs.commit_all(root, "base")


def _commit(root: Path, name: str, text: str = "change\n", *, message: str | None = None) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    gs.commit_all(root, message or f"add {name}")


def _state_file(worktree: Path, verification_only: bool = False) -> Path:
    """Write a child state file into *worktree* and return its path."""
    import json

    state = {
        "work_item_id": CHILD_ID,
        "worktree_path": str(worktree),
        "repo_root": str(worktree.parent.parent),
        "parent_branch": PARENT_BRANCH,
        "commit_msg": "",
        "started_at": "",
        "verification_only": verification_only,
    }
    path = worktree / implement.STATE_FILE_NAME
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway main checkout on ``dev`` with one commit."""
    root = tmp_path / "repo"
    _init_repo(root)
    return root


@pytest.fixture()
def worktree(repo: Path, tmp_path: Path) -> Path:
    """A clean child worktree forked from ``dev`` at the fork base."""
    target = tmp_path / "wt" / "wl-child"
    target.parent.mkdir(parents=True, exist_ok=True)
    return gs.add_worktree(repo, target, "wl-child", base=PARENT_BRANCH)


# ---------------------------------------------------------------------------
# AC5 (iv): the classifier verdict itself
# ---------------------------------------------------------------------------


class TestClassifyChildWorktree:
    def test_clean_worktree_is_clean(self, worktree: Path) -> None:
        assert (
            implement._classify_child_worktree(CHILD_ID, str(worktree))
            == implement.CHILD_CLEAN
        )

    def test_real_uncommitted_work_is_work(self, worktree: Path) -> None:
        (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")

        assert (
            implement._classify_child_worktree(CHILD_ID, str(worktree))
            == implement.CHILD_WORK
        )

    def test_state_file_only_dirty_is_no_work(self, worktree: Path) -> None:
        # The bug shape: an accidentally tracked ``.implement_state.json`` is
        # the *only* dirty path — ``_has_worktree_changes`` reports True.
        state = _state_file(worktree)
        _git(worktree, "add", "-f", state.name)

        assert (
            implement._classify_child_worktree(CHILD_ID, str(worktree))
            == implement.CHILD_NO_WORK
        )

    def test_attributable_commit_ahead_is_work(self, worktree: Path) -> None:
        _commit(
            worktree,
            "child-work.txt",
            message=f"{CHILD_ID}: implement the child",
        )

        assert (
            implement._classify_child_worktree(CHILD_ID, str(worktree))
            == implement.CHILD_WORK
        )

    def test_non_attributable_commit_ahead_is_no_work(self, worktree: Path) -> None:
        # A commit ahead that belongs to nothing but the ambient parent branch
        # must never be presented as the child's work.
        _commit(worktree, "ambient.txt", message="unrelated ambient change")

        assert (
            implement._classify_child_worktree(CHILD_ID, str(worktree))
            == implement.CHILD_NO_WORK
        )

    def test_verification_only_clean_is_verification_only(
        self, worktree: Path
    ) -> None:
        _state_file(worktree, verification_only=True)

        assert (
            implement._classify_child_worktree(CHILD_ID, str(worktree))
            == implement.CHILD_VERIFICATION_ONLY
        )

    def test_verification_only_with_real_work_is_still_work(
        self, worktree: Path
    ) -> None:
        _state_file(worktree, verification_only=True)
        (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")

        assert (
            implement._classify_child_worktree(CHILD_ID, str(worktree))
            == implement.CHILD_WORK
        )

    def test_missing_worktree_is_no_work(self, tmp_path: Path) -> None:
        assert (
            implement._classify_child_worktree(
                CHILD_ID, str(tmp_path / "does-not-exist")
            )
            == implement.CHILD_NO_WORK
        )


# ---------------------------------------------------------------------------
# AC4/AC5 (iv): phase_parent never presents no-work as finishable
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

    def _fake_update_status(work_item_id, status_, stage=None, assignee=None, **_kw):
        status_calls.append((work_item_id, status_, stage, assignee))
        return {"success": True, "workItem": {"id": work_item_id, "status": status_}}

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
        ),
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
    return report


def _entries(report: dict) -> dict[str, dict]:
    return {
        str(item.get("id")): item
        for item in report.get("children_processed") or []
    }


def _parent_advanced(status_calls: list[tuple]) -> bool:
    return any(
        call[1] == "completed" and call[2] == "in_review"
        for call in status_calls
    )


class TestPhaseParentNoWork:
    def test_state_file_only_child_is_not_finalisable(
        self, worktree: Path
    ) -> None:
        state = _state_file(worktree)
        _git(worktree, "add", "-f", state.name)

        report = _run_phase_parent(worktree)

        # finish is never invoked for a zero-work child.
        assert report["_invoked"] == []
        entry = _entries(report)[CHILD_ID]
        assert entry["action"] == "not-finalisable"
        assert entry["worktree_path"] == str(worktree)
        assert report["not_finalisable_children"] == [CHILD_ID]
        # The parent is never advanced to in_review.
        assert not report.get("parent_advanced")
        assert not _parent_advanced(report["_status_calls"])

    def test_non_attributable_commit_child_is_not_finalisable(
        self, worktree: Path
    ) -> None:
        _commit(worktree, "ambient.txt", message="unrelated ambient change")

        report = _run_phase_parent(worktree)

        assert report["_invoked"] == []
        assert _entries(report)[CHILD_ID]["action"] == "not-finalisable"
        assert report["not_finalisable_children"] == [CHILD_ID]
        assert not report.get("parent_advanced")
        assert not _parent_advanced(report["_status_calls"])

    def test_in_progress_state_file_only_child_is_not_finalisable(
        self, worktree: Path
    ) -> None:
        # The in-progress branch is the second ``has_changes`` call site.
        state = _state_file(worktree)
        _git(worktree, "add", "-f", state.name)

        report = _run_phase_parent(worktree, status="in-progress")

        assert report["_invoked"] == []
        assert _entries(report)[CHILD_ID]["action"] == "not-finalisable"
        assert not report.get("parent_advanced")
        assert not _parent_advanced(report["_status_calls"])

    def test_genuine_commit_ahead_is_still_finished(self, worktree: Path) -> None:
        # Non-vacuity: the same harness finalises a child that did real work.
        _commit(
            worktree,
            "child-work.txt",
            message=f"{CHILD_ID}: implement the child",
        )

        report = _run_phase_parent(worktree)

        assert report["_invoked"] == [("finish", CHILD_ID)]
        assert _entries(report)[CHILD_ID]["action"] == "finished"

    def test_real_uncommitted_work_is_still_finished(self, worktree: Path) -> None:
        (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")

        report = _run_phase_parent(worktree)

        assert report["_invoked"] == [("finish", CHILD_ID)]
        assert _entries(report)[CHILD_ID]["action"] == "finished"

    def test_verification_only_child_is_still_finished(
        self, worktree: Path
    ) -> None:
        # A deliberate no-commit finalisation carries the state-file opt-in and
        # must still flow through to finish (which honours it) — verification-
        # only outranks the not-finalisable verdict.
        state = _state_file(worktree, verification_only=True)
        _git(worktree, "add", "-f", state.name)

        report = _run_phase_parent(worktree)

        assert report["_invoked"] == [("finish", CHILD_ID)]
        assert _entries(report)[CHILD_ID]["action"] == "finished"
        assert "not_finalisable_children" not in report
