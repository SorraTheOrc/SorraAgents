
# <!-- REFACTOR-SA-0MWFSGW52003YZA3
# smell: ruff_specific
# severity: medium
# description: Unpacked variable `status_calls` is never used
# -->
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


SPAWN_FAILURE_REASON = (
    f"Child session for {CHILD_ID} exited with code 1: Provider "
    "finish_reason: error"
)


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


def _run_phase_parent(
    worktree_path: Path, invoke_impl=None, status: str = "open"
):
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


# ---------------------------------------------------------------------------
# Spawn-failure evidence: a child that wrote nothing is startable again
# (SA-0MWGZ8NWP009QD8G / AH-0MWFHLDAS008YG0P)
# ---------------------------------------------------------------------------
#
# Incident: ``implement.py drive`` spawned a child session that died before
# writing anything (a transient provider error, ``Provider finish_reason:
# error``). The child was left ``in-progress`` with a clean worktree (no dirty
# files, no commits ahead), and the next ``drive``/``parent`` run then
# attributed *pre-existing* dirt in the main checkout and in three sibling
# worktrees to it — naming paths the child had never touched — and failed the
# chain closed. Recovering needed a manual ``implement.py abort``.
#
# Contract (parent ACs):
#
# - The driver records durable evidence when a child's session fails and the
#   child produced no work (``record_spawn_failure``). The record lives under
#   the worktree's ``.worklog/`` — a prefix the dirty-path helpers always
#   exclude — so it can never be mistaken for work, and it is cleared when the
#   next session starts.
# - ``_child_main_checkout_violation`` treats recorded spawn-failure evidence
#   as proof the child wrote nothing: unrelated dirty surfaces are then
#   pre-existing dirt, so the child is reported *startable again* (the next
#   ``drive`` re-spawns it) instead of failing the chain.
# - Evidence-free children keep the full guard (SA-0MW5FIJNQ002Z24I): a child
#   that genuinely wrote into a sibling worktree still fails closed, at both
#   ``phase_parent`` call sites.


class TestSpawnFailureEvidence:
    """The durable spawn-failure record itself."""

    def test_recorded_evidence_round_trips_and_is_never_work(
        self, child_worktree: Path
    ) -> None:
        evidence = implement.record_spawn_failure(
            str(child_worktree), CHILD_ID, SPAWN_FAILURE_REASON
        )

        assert evidence is not None
        assert evidence["child_id"] == CHILD_ID
        assert evidence["reason"] == SPAWN_FAILURE_REASON
        assert evidence["recorded_at"]
        assert (
            implement.read_spawn_failure(str(child_worktree), CHILD_ID)
            == evidence
        )
        # AC1: recording the failure must not turn the child into "has work".
        # The record sits under ``.worklog/``, which every dirty-path helper
        # excludes, so the child stays re-spawnable (never finishable).
        assert implement._has_worktree_changes(str(child_worktree)) is False
        assert (
            implement._classify_child_worktree(
                CHILD_ID, str(child_worktree), PARENT_BRANCH
            )
            == implement.CHILD_CLEAN
        )

    def test_clear_removes_the_record_and_is_idempotent(
        self, child_worktree: Path
    ) -> None:
        implement.record_spawn_failure(
            str(child_worktree), CHILD_ID, SPAWN_FAILURE_REASON
        )

        assert implement.clear_spawn_failure(str(child_worktree)) is True
        assert implement.read_spawn_failure(str(child_worktree), CHILD_ID) is None
        assert implement.clear_spawn_failure(str(child_worktree)) is False

    def test_evidence_naming_another_child_is_never_attributed(
        self, child_worktree: Path
    ) -> None:
        implement.record_spawn_failure(
            str(child_worktree), "SA-OTHERCHILD00001", SPAWN_FAILURE_REASON
        )

        assert implement.read_spawn_failure(str(child_worktree), CHILD_ID) is None
        # Reading without a child id still yields the raw record (diagnostics).
        assert implement.read_spawn_failure(str(child_worktree)) is not None

    def test_unreadable_evidence_fails_open_to_the_guard(
        self, child_worktree: Path
    ) -> None:
        marker = child_worktree / implement.SPAWN_FAILURE_FILE
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{not json", encoding="utf-8")

        assert implement.read_spawn_failure(str(child_worktree), CHILD_ID) is None

    def test_a_missing_worktree_is_never_recreated(
        self, tmp_path: Path
    ) -> None:
        gone = tmp_path / "removed-worktree"

        assert implement.record_spawn_failure(str(gone), CHILD_ID, "boom") is None
        assert not gone.exists()


class TestSpawnFailedChildIsStartableAgain:
    """A spawn-failed child is re-spawnable, not a placement violation."""

    def test_dirty_main_and_sibling_are_not_attributed_to_it(
        self, repo: Path, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """The incident: unrelated dirt in both foreign surfaces."""
        (repo / "dirty-main.txt").write_text("uncommitted\n", encoding="utf-8")
        (sibling_worktree / "stray.py").write_text("x = 1\n", encoding="utf-8")
        implement.record_spawn_failure(
            str(child_worktree), CHILD_ID, SPAWN_FAILURE_REASON
        )

        report, status_calls, invoked = _run_phase_parent(child_worktree)

        assert report["success"] is True
        assert report.get("placement_violation") is not True
        assert report["next_child"] == CHILD_ID
        assert report["spawn_failure"]["child_id"] == CHILD_ID
        assert "startable again" in report["message"]
        # No status mutation: the child is reported, never reset behind the
        # workflow's back, and no finish is attempted.
        assert invoked == []
        assert status_calls == []

    def test_in_progress_child_with_evidence_is_startable_again(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """The ``skip-in-progress`` call site (the incident's shape)."""
        (sibling_worktree / "stray.py").write_text("x = 1\n", encoding="utf-8")
        implement.record_spawn_failure(
            str(child_worktree), CHILD_ID, SPAWN_FAILURE_REASON
        )

        report, status_calls, invoked = _run_phase_parent(
            child_worktree, status="in-progress"
        )

        assert report["success"] is True
        assert report.get("placement_violation") is not True
        assert report["next_child"] == CHILD_ID
        assert "startable again" in report["message"]
        assert invoked == []
        assert status_calls == []

    def test_clean_surfaces_with_evidence_still_report_implement_next(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """The happy path is unchanged: nothing dirty, nothing to attribute."""
        implement.record_spawn_failure(
            str(child_worktree), CHILD_ID, SPAWN_FAILURE_REASON
        )

        report, status_calls, invoked = _run_phase_parent(child_worktree)

        assert report["success"] is True
        assert report.get("placement_violation") is not True
        assert report["next_child"] == CHILD_ID
        assert invoked == []
        assert status_calls == []

    def test_genuine_stray_without_evidence_still_fails_closed(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """No weakening: the same dirt *without* evidence is a violation."""
        (sibling_worktree / "stray.py").write_text("x = 1\n", encoding="utf-8")

        report, status_calls, invoked = _run_phase_parent(
            child_worktree, status="in-progress"
        )

        assert report["success"] is False
        assert report["placement_violation"] is True
        assert str(sibling_worktree) in report["message"]
        assert "stray.py" in report["message"]
        assert invoked == []
        assert status_calls == []

    def test_evidence_naming_another_child_keeps_the_guard(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        (sibling_worktree / "stray.py").write_text("x = 1\n", encoding="utf-8")
        implement.record_spawn_failure(
            str(child_worktree), "SA-OTHERCHILD00001", SPAWN_FAILURE_REASON
        )

        report, _status_calls, _invoked = _run_phase_parent(child_worktree)

        assert report["success"] is False
        assert report["placement_violation"] is True


class TestDriveRecordsSpawnFailureEvidence:
    """``phase_drive`` records the evidence the guard later reads."""

    def _drive(self, child_worktree: Path, child_status: str = "in-progress"):
        parent_report = {
            "success": True,
            "next_child": CHILD_ID,
            "worktree_path": str(child_worktree),
        }
        child_item = _child(child_status)

        def _wl_show(work_item_id: str):
            if work_item_id == PARENT_ID:
                return {
                    "id": PARENT_ID,
                    "title": "Parent",
                    "status": "plan_complete",
                    "stage": "plan_complete",
                }
            return dict(child_item)

        def _spawner(child_id, worktree_path, timeout, env, verbose=False):
            assert child_id == CHILD_ID
            assert worktree_path == str(child_worktree)
            return {
                "success": False,
                "returncode": 1,
                "reason": SPAWN_FAILURE_REASON,
            }

        with (
            mock.patch.object(implement, "wl_show", side_effect=_wl_show),
            mock.patch.object(
                implement, "wl_show_children", return_value=[child_item]
            ),
            mock.patch.object(
                implement, "_discover_worktree", return_value=str(child_worktree)
            ),
            mock.patch.object(implement, "wl_add_comment", return_value=True),
        ):
            return implement.phase_drive(
                PARENT_ID,
                parent_runner=lambda *a, **k: dict(parent_report),
                spawn_session=_spawner,
            )

    def test_failed_session_on_a_child_with_no_work_records_evidence(
        self, child_worktree: Path
    ) -> None:
        report = self._drive(child_worktree)

        assert report["failed_child"] == CHILD_ID
        entry = report["children_driven"][0]
        assert entry["result"] == "failed"
        assert entry["spawn_failure_recorded"] is True

        evidence = implement.read_spawn_failure(str(child_worktree), CHILD_ID)
        assert evidence is not None
        assert "Provider finish_reason: error" in evidence["reason"]

    def test_recorded_evidence_makes_the_next_parent_run_startable_again(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """End to end: drive's record defuses the next run's guard."""
        self._drive(child_worktree)
        (sibling_worktree / "stray.py").write_text("x = 1\n", encoding="utf-8")

        report, _status_calls, _invoked = _run_phase_parent(
            child_worktree, status="in-progress"
        )

        assert report["success"] is True
        assert report.get("placement_violation") is not True
        assert report["next_child"] == CHILD_ID

    def test_a_child_that_already_holds_work_records_no_evidence(
        self, child_worktree: Path
    ) -> None:
        """Evidence is only ever recorded for a child that produced nothing."""
        _commit(child_worktree, "child-work.py", branch_tag=CHILD_ID)

        self._drive(child_worktree)

        assert implement.read_spawn_failure(str(child_worktree), CHILD_ID) is None
