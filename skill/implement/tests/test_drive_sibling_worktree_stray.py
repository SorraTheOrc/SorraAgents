"""Regression tests: a driven child that strays into a sibling worktree.

Incident (SA-0MW5FIJNQ002Z24I): a driven child whose first ``finish`` gate
was red for a pre-existing environment-only failure triaged the failure into
a new work item — and then, following the AGENTS.md blocker rule verbatim,
**started implementing that item itself**. ``implement.py start <new-id>``
created the new item's worktree as a *sibling* of the child's own worktree,
and the child wrote there. The existing placement guard
(``_child_main_checkout_violation``) only scanned the main checkout, so the
sibling work was invisible to ``phase_parent``/``phase_drive``: the child
could race the session that owns that worktree, silently widen the epic, and
consume the host's test slots.

Contract (AC3):

- A simulated driven child that ``cd``'s into a registered sibling worktree
  and writes there is detected by ``_child_main_checkout_violation`` (the
  parent/drive placement guard) even when the main checkout is clean.
- ``phase_parent`` fails closed via ``_fail_placement`` (``success: False``
  + ``placement_violation: True``), naming the sibling root and its offending
  paths.
- The fail-closed refusal mutates **neither** item's status: no
  ``update_status`` call is recorded for the child, and the sibling item is
  never even looked up (the guard only reads the filesystem).

All fixtures are real git repositories via :mod:`shared.git_sandbox` — the
guards are never re-implemented.
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
PARENT_ID = "SA-PARENT00000001"
CHILD_ID = "SA-CHILD0000000001"
SIBLING_ID = "SA-SIBLING0000002"


def _git(cwd: Path, *args: str) -> str:
    return gs.run_git(cwd, args, check=True).stdout


def _init_repo(root: Path) -> None:
    gs.init_repo(root, default_branch=PARENT_BRANCH)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    gs.commit_all(root, "base")


def _commit(root: Path, name: str, *, branch_tag: str) -> None:
    path = root / name
    path.write_text(f"{name}\n", encoding="utf-8")
    gs.commit_all(root, f"{branch_tag}: {name}")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway main checkout on ``dev`` with one commit."""
    root = tmp_path / "repo"
    _init_repo(root)
    return root


@pytest.fixture()
def child_worktree(repo: Path, tmp_path: Path) -> Path:
    """The driven child's own clean worktree (its IMPLEMENT_WORKTREE_PATH)."""
    wt = tmp_path / "wt" / f"wl-{CHILD_ID}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    return gs.add_worktree(repo, wt, f"wl-{CHILD_ID}", base=PARENT_BRANCH)


@pytest.fixture()
def sibling_worktree(repo: Path, tmp_path: Path) -> Path:
    """Another item's linked worktree — the surface the child strayed into."""
    wt = tmp_path / "wt2" / f"wl-{SIBLING_ID}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    return gs.add_worktree(repo, wt, f"wl-{SIBLING_ID}", base=PARENT_BRANCH)


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


def _run_phase_parent(worktree_path: Path, invoke_impl=None):
    """Drive ``phase_parent`` against the child, recording wl writes.

    Returns the report plus the recorded ``update_status`` calls (empty = no
    status mutation) and the invoked subprocesses. *invoke_impl* overrides
    the subprocess boundary (default: any subprocess call is an error — the
    guard paths never spawn one).
    """
    invoked: list[tuple[str, str]] = []
    status_calls: list[tuple] = []

    def _invoke(action, work_item_id, **_kwargs):
        invoked.append((action, work_item_id))
        if invoke_impl is not None:
            return invoke_impl(action, work_item_id, **_kwargs)
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
            implement, "wl_show_children", return_value=[_child()]
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
        mock.patch.object(implement, "_invoke_implement", side_effect=_invoke),
        mock.patch.object(
            implement.StatusLifecycle,
            "update_status",
            side_effect=_fake_update_status,
        ),
        mock.patch.object(implement, "wl_add_comment", return_value=True),
    ):
        report = implement.phase_parent(PARENT_ID, json_output=True)

    return report, status_calls, invoked


class TestDrivenChildStraysIntoSiblingWorktree:
    def test_sibling_write_is_detected_and_the_run_fails_closed(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """AC3: the driven child cd'd into the sibling and wrote there.

        The main checkout stays clean — the old guard's blind spot — and the
        child's own worktree holds nothing. ``phase_parent`` must fail closed
        naming both roots and the offending sibling paths.
        """
        (sibling_worktree / "stray.py").write_text(
            "x = 1\n", encoding="utf-8"
        )

        report, status_calls, invoked = _run_phase_parent(child_worktree)

        assert report["success"] is False
        assert report["placement_violation"] is True
        assert report["next_child"] == CHILD_ID
        assert str(sibling_worktree) in report["message"]
        assert str(child_worktree) in report["message"]
        assert "stray.py" in report["message"]
        # A fail-closed placement refusal never mutates either item's status:
        # ``_fail_placement`` only reports and comments.
        assert invoked == []
        assert status_calls == []

    def test_committed_sibling_still_fails_closed_naming_the_sibling(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        # The stray child committed into the sibling worktree: no dirty files
        # exist to list, but the sibling is ahead of the parent branch — the
        # run still fails closed naming the sibling (no status mutation).
        _commit(sibling_worktree, "stray-committed.py", branch_tag=SIBLING_ID)

        report, status_calls, invoked = _run_phase_parent(child_worktree)

        assert report["success"] is False
        assert report["placement_violation"] is True
        assert str(sibling_worktree) in report["message"]
        assert "commits ahead of dev" in report["message"]
        assert invoked == []
        assert status_calls == []

    def test_clean_sibling_and_clean_own_worktree_returns_implement_next(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """No foreign surface → the phase reports the child for implementation."""
        report, status_calls, invoked = _run_phase_parent(child_worktree)

        assert report["success"] is True
        assert report.get("placement_violation") is not True
        assert report["next_child"] == CHILD_ID
        assert invoked == []
        assert status_calls == []

    def test_own_worktree_holding_the_work_never_triggers_the_guard(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        _commit(sibling_worktree, "stray.py", branch_tag=SIBLING_ID)
        _commit(child_worktree, "own-work.py", branch_tag=CHILD_ID)

        report, status_calls, invoked = _run_phase_parent(
            child_worktree,
            invoke_impl=lambda action, work_item_id, **_kw: {
                "success": True, "work_item_id": work_item_id,
            },
        )

        assert report.get("placement_violation") is not True
        # The child has work, so the phase handed it to finish (stubbed).
        assert invoked == [("finish", CHILD_ID)]
