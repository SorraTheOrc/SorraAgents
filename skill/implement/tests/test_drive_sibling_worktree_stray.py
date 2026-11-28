
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

import json
import logging
import os
import shutil
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
    f"Child session for {CHILD_ID} exited with code 1: "
    "ModuleNotFoundError: No module named 'nonexistent_module'"
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


def _establish_baseline(worktree: Path, child_id: str = CHILD_ID) -> dict:
    """Record the session baseline ``phase_start``/``phase_drive`` write.

    A *genuine* stray write happens **after** the baseline: establishing it
    here keeps the guard's post-baseline semantics under test instead of the
    ambient exemption a never-spawned child rightfully gets
    (SA-0MWL0XX88000E38K).
    """
    repo_root = implement._get_repo_root(str(worktree))
    assert repo_root, f"could not resolve the repo root for {worktree}"
    record = implement.record_session_baseline(
        repo_root, worktree, child_id, PARENT_BRANCH
    )
    assert record is not None
    return record


class TestDrivenChildStraysIntoSiblingWorktree:
    def test_sibling_write_is_detected_and_the_run_fails_closed(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """AC3: the driven child cd'd into the sibling and wrote there.

        The main checkout stays clean — the old guard's blind spot — and the
        child's own worktree holds nothing. ``phase_parent`` must fail closed
        naming both roots and the offending sibling paths.
        """
        _establish_baseline(child_worktree)
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
        _establish_baseline(child_worktree)
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

        report, _, invoked = _run_phase_parent(
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
# writing anything (in the original report, a provider error). The child was
# left ``in-progress`` with a clean worktree (no dirty files, no commits
# ahead), and the next ``drive``/``parent`` run then attributed *pre-existing*
# dirt in the main checkout and in three sibling worktrees to it — naming
# paths the child had never touched — and failed the chain closed. Recovering
# needed a manual ``implement.py abort``.
#
# NOTE: the transient provider-stream signature (``Provider finish_reason:
# error``) is now classified as retryable infrastructure and absorbed on the
# ``--infra-retry-limit`` budget (WL-0MWTZEDWG008YMGQ), so this module's
# failure reason is a *genuine* ``unknown`` defect — the fail-safe floor that
# still records evidence and hard-stops.
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
        _establish_baseline(child_worktree)
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
        _establish_baseline(child_worktree)
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
        assert "ModuleNotFoundError" in evidence["reason"]

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


# ---------------------------------------------------------------------------
# Stale debris is never evidence (WL-0MWQLHDOS006CAIH)
# ---------------------------------------------------------------------------
#
# Incident: the epic drive ``WL-0MW50EHZ6007QBPB`` refused a **clean** child's
# worktree and named four unrelated surfaces — three ``/tmp/wl-*`` scratch
# trees and the leftover worktree of the already-``deleted`` item
# ``WL-0MVOA0XVC0002PBG``. The chain could only continue after a manual
# ``git worktree remove``.
#
# Contract the debris filter adds to the sibling-surface scan:
#
# - A registered sibling **outside** the repository's worktree root
#   (``.worklog/worktrees/``) is scratch debris and is never evidence, dirty
#   or not — *when the child's own worktree lives inside that root*, which is
#   the only case where "outside the root" is meaningful. A child working
#   outside the canonical root (a custom ``--worktree-path``, an ad-hoc tree)
#   leaves the scope unknown, so the pre-fix fail-closed behaviour is kept.
# - A sibling whose **owning work item is terminal** cannot be raced by a live
#   session, so its dirt is never evidence either, wherever it lives.
# - Unresolved ownership (no work-item id in the directory name, or ``wl show``
#   unavailable/mismatched) stays **fail-closed**: the sibling still counts.
# - The child's own work still short-circuits the whole check, and the dirty
#   scan still runs before any worklog lookup — the fast paths gain no
#   ``wl`` subprocess.

DEBRIS_ITEM = "SA-DEBRISTERM0001"
LIVE_ITEM = "SA-LIVESIB0000001"


@pytest.fixture()
def rooted_repo(tmp_path: Path) -> Path:
    """A checkout whose linked worktrees live in the canonical root."""
    root = tmp_path / "rooted-repo"
    _init_repo(root)
    (root / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    gs.commit_all(root, "ignore .worklog")
    return root


def _canonical_worktree(rooted_repo: Path, item_id: str) -> Path:
    """Register a per-item worktree under ``<repo>/.worklog/worktrees/``."""
    name = f"wl-{item_id}-slug"
    path = rooted_repo / implement.DEFAULT_WORKTREE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return gs.add_worktree(rooted_repo, path, name, base=PARENT_BRANCH)


@pytest.fixture()
def canonical_child(rooted_repo: Path) -> Path:
    """The child's own clean worktree, inside the canonical worktree root."""
    return _canonical_worktree(rooted_repo, CHILD_ID)


def _patch_wl_show(items: dict[str, dict] | None = None):
    """Patch ``wl show``: known ids resolve, unknown ids are a test failure.

    ``None`` (or an empty map) patches with a lookup that raises, so a test
    asserting "no worklog lookup on this path" fails loudly when one happens.
    """
    resolved = items or {}

    def _fake(work_item_id: str) -> dict:
        if work_item_id not in resolved:
            raise AssertionError(f"unexpected wl show for {work_item_id}")
        return dict(resolved[work_item_id])

    return mock.patch.object(implement, "wl_show", side_effect=_fake)


class TestWorktreeItemOwnership:
    """The reverse helper that maps a worktree path to its owning item."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (f"wl-{CHILD_ID}-test-first-guard", CHILD_ID),
            (f"audit-exec-{CHILD_ID}", CHILD_ID),
            (f"wl-{DEBRIS_ITEM}", DEBRIS_ITEM),
            # Untagged trees carry no ownership: debris without an owner id.
            ("wl-test-ff", None),
            ("wl-base-check", None),
            ("audit-code-root", None),
            ("scratch", None),
        ],
    )
    def test_worktree_directory_name_maps_to_its_item(
        self, name: str, expected: str | None, tmp_path: Path
    ) -> None:
        assert implement._worktree_path_to_item_id(tmp_path / name) == expected

    def test_terminal_owner_resolves_through_wl_show(
        self, canonical_child: Path
    ) -> None:
        with _patch_wl_show({DEBRIS_ITEM: {"id": DEBRIS_ITEM, "status": "deleted"}}):
            assert implement._worktree_owner_is_terminal(
                Path(f"/tmp/wl-{DEBRIS_ITEM}-leftover")
            ) is True

    def test_live_owner_is_not_terminal(self, canonical_child: Path) -> None:
        with _patch_wl_show({LIVE_ITEM: {"id": LIVE_ITEM, "status": "in-progress"}}):
            assert implement._worktree_owner_is_terminal(
                Path(f"/tmp/wl-{LIVE_ITEM}-live")
            ) is False

    def test_untagged_tree_has_unresolved_ownership(self) -> None:
        with _patch_wl_show():
            assert implement._worktree_owner_is_terminal(Path("/tmp/wl-base-check")) is None

    def test_mismatched_lookup_is_unresolved_ownership(self) -> None:
        with _patch_wl_show({LIVE_ITEM: {"id": CHILD_ID, "status": "completed"}}):
            assert implement._worktree_owner_is_terminal(
                Path(f"/tmp/wl-{LIVE_ITEM}-live")
            ) is None

    def test_unavailable_worklog_is_unresolved_ownership(self) -> None:
        with mock.patch.object(
            implement, "wl_show", side_effect=FileNotFoundError("wl missing")
        ):
            assert implement._worktree_owner_is_terminal(
                Path(f"/tmp/wl-{LIVE_ITEM}-live")
            ) is None


class TestChildMainCheckoutViolationDebrisFilter:
    """``_child_main_checkout_violation`` never counts stale debris."""

    def _violation(
        self,
        rooted_repo: Path,
        child_worktree: Path,
        items: dict[str, dict] | None = None,
    ) -> str | None:
        with _patch_wl_show(items):
            return implement._child_main_checkout_violation(
                CHILD_ID,
                str(child_worktree),
                PARENT_BRANCH,
                repo_root=str(rooted_repo),
            )

    def test_outside_root_scratch_tree_is_never_a_foreign_surface(
        self, rooted_repo: Path, canonical_child: Path, tmp_path: Path
    ) -> None:
        scratch = gs.add_worktree(
            rooted_repo,
            tmp_path / "scratch" / f"wl-{CHILD_ID}-scratch",
            f"wl-{CHILD_ID}-scratch",
            base=PARENT_BRANCH,
        )
        (scratch / "scratch-marker.js").write_text("x = 1\n", encoding="utf-8")

        # No ``wl show`` at all: an outside-root tree is debris on path alone.
        assert self._violation(rooted_repo, canonical_child, None) is None

    def test_outside_root_sibling_is_ignored_even_with_commits_ahead(
        self, rooted_repo: Path, canonical_child: Path, tmp_path: Path
    ) -> None:
        scratch = gs.add_worktree(
            rooted_repo,
            tmp_path / "scratch2" / f"wl-{LIVE_ITEM}-scratch",
            f"wl-{LIVE_ITEM}-scratch",
            base=PARENT_BRANCH,
        )
        _commit(scratch, "scratch-work.py", branch_tag=LIVE_ITEM)

        assert self._violation(rooted_repo, canonical_child, None) is None

    def test_terminal_owner_leftover_tree_is_ignored(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        leftover = _canonical_worktree(rooted_repo, DEBRIS_ITEM)
        (leftover / "index.ts").write_text("deleted-item work\n", encoding="utf-8")

        assert (
            self._violation(
                rooted_repo,
                canonical_child,
                {DEBRIS_ITEM: {"id": DEBRIS_ITEM, "status": "completed"}},
            )
            is None
        )

    def test_deleted_owner_leftover_tree_is_ignored(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        leftover = _canonical_worktree(rooted_repo, DEBRIS_ITEM)
        (leftover / "index.ts").write_text("deleted-item work\n", encoding="utf-8")

        assert (
            self._violation(
                rooted_repo,
                canonical_child,
                {DEBRIS_ITEM: {"id": DEBRIS_ITEM, "status": "deleted"}},
            )
            is None
        )

    def test_dirty_live_sibling_still_fails_closed(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "stray.py").write_text("x = 1\n", encoding="utf-8")

        violation = self._violation(
            rooted_repo,
            canonical_child,
            {LIVE_ITEM: {"id": LIVE_ITEM, "status": "in-progress"}},
        )

        assert violation is not None
        assert str(live) in violation
        assert "stray.py" in violation
        assert str(canonical_child) in violation

    def test_untagged_inside_root_sibling_fails_closed(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        untagged = gs.add_worktree(
            rooted_repo,
            rooted_repo / implement.DEFAULT_WORKTREE_DIR / "wl-adhoc-check",
            "wl-adhoc-check",
            base=PARENT_BRANCH,
        )
        (untagged / "stray.py").write_text("x = 1\n", encoding="utf-8")

        violation = self._violation(rooted_repo, canonical_child, None)

        assert violation is not None
        assert str(untagged) in violation

    def test_unresolvable_owner_fails_closed(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "stray.py").write_text("x = 1\n", encoding="utf-8")

        with mock.patch.object(implement, "wl_show", return_value={}):
            violation = implement._child_main_checkout_violation(
                CHILD_ID,
                str(canonical_child),
                PARENT_BRANCH,
                repo_root=str(rooted_repo),
            )

        assert violation is not None
        assert str(live) in violation

    def test_child_holding_its_own_work_is_never_refused_and_skips_the_lookup(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "stray.py").write_text("x = 1\n", encoding="utf-8")
        _commit(canonical_child, "own-work.py", branch_tag=CHILD_ID)

        # The raising fake proves the fast path never reaches ``wl show``.
        assert self._violation(rooted_repo, canonical_child, None) is None

    def test_clean_inside_root_siblings_skip_the_lookup(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        _canonical_worktree(rooted_repo, LIVE_ITEM)

        assert self._violation(rooted_repo, canonical_child, None) is None

    def test_child_outside_the_canonical_root_keeps_the_pre_fix_behaviour(
        self, repo: Path, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """Scope unknown (ad-hoc worktrees) — the guard stays fail-closed.

        The scope of "outside the repository's worktree root" cannot be
        established when neither tree lives under it, so nothing is filtered.
        """
        (sibling_worktree / "stray.py").write_text("x = 1\n", encoding="utf-8")

        with _patch_wl_show():
            violation = implement._child_main_checkout_violation(
                CHILD_ID, str(child_worktree), PARENT_BRANCH, repo_root=str(repo)
            )

        assert violation is not None
        assert str(sibling_worktree) in violation


class TestPlacementViolationDebrisFilter:
    """The cwd-sensitive finish guard applies the same debris filter."""

    def _violation(
        self,
        rooted_repo: Path,
        child_worktree: Path,
        cwd: Path,
        items: dict[str, dict] | None = None,
    ) -> str | None:
        with _patch_wl_show(items):
            return implement._worktree_placement_violation(
                str(child_worktree),
                cwd=str(cwd),
                repo_root=str(rooted_repo),
                parent_branch=PARENT_BRANCH,
            )

    def test_cwd_in_a_terminal_owner_tree_is_not_a_violation(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        leftover = _canonical_worktree(rooted_repo, DEBRIS_ITEM)
        (leftover / "index.ts").write_text("deleted-item work\n", encoding="utf-8")

        assert (
            self._violation(
                rooted_repo,
                canonical_child,
                leftover,
                {DEBRIS_ITEM: {"id": DEBRIS_ITEM, "status": "completed"}},
            )
            is None
        )

    def test_cwd_in_an_outside_root_scratch_tree_is_not_a_violation(
        self, rooted_repo: Path, canonical_child: Path, tmp_path: Path
    ) -> None:
        scratch = gs.add_worktree(
            rooted_repo,
            tmp_path / "scratch" / f"wl-{LIVE_ITEM}-scratch",
            f"wl-{LIVE_ITEM}-scratch",
            base=PARENT_BRANCH,
        )
        (scratch / "scratch-marker.js").write_text("x = 1\n", encoding="utf-8")

        assert self._violation(rooted_repo, canonical_child, scratch, None) is None

    def test_cwd_in_a_dirty_live_sibling_still_fails_closed(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "stray.py").write_text("x = 1\n", encoding="utf-8")

        violation = self._violation(
            rooted_repo,
            canonical_child,
            live,
            {LIVE_ITEM: {"id": LIVE_ITEM, "status": "in-progress"}},
        )

        assert violation is not None
        assert str(live) in violation
        assert "stray.py" in violation

    def test_child_work_short_circuits_before_any_lookup(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "stray.py").write_text("x = 1\n", encoding="utf-8")
        _commit(canonical_child, "own-work.py", branch_tag=CHILD_ID)

        assert self._violation(rooted_repo, canonical_child, live, None) is None


class TestPhaseParentIgnoresStaleDebris:
    """End to end: a clean child is admitted despite dirty debris siblings."""

    def test_dirty_debris_siblings_do_not_block_a_clean_child(
        self, rooted_repo: Path, canonical_child: Path, tmp_path: Path
    ) -> None:
        scratch = gs.add_worktree(
            rooted_repo,
            tmp_path / "scratch" / f"wl-{CHILD_ID}-scratch",
            f"wl-{CHILD_ID}-scratch",
            base=PARENT_BRANCH,
        )
        (scratch / "scratch-marker.js").write_text("x = 1\n", encoding="utf-8")
        leftover = _canonical_worktree(rooted_repo, DEBRIS_ITEM)
        (leftover / "index.ts").write_text("deleted-item work\n", encoding="utf-8")

        def _wl_show(work_item_id: str) -> dict:
            if work_item_id == PARENT_ID:
                return {
                    "id": PARENT_ID,
                    "title": "Parent",
                    "status": "plan_complete",
                }
            if work_item_id == DEBRIS_ITEM:
                return {"id": DEBRIS_ITEM, "title": "Debris", "status": "completed"}
            raise AssertionError(f"unexpected wl show for {work_item_id}")

        def _invoke(action, work_item_id, **_kwargs):
            raise AssertionError(f"unexpected subprocess action {action}")

        with (
            mock.patch.object(implement, "is_code_freeze_active", return_value=False),
            mock.patch.object(implement, "wl_show", side_effect=_wl_show),
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
                implement, "_discover_worktree", return_value=str(canonical_child)
            ),
            mock.patch.object(implement, "_invoke_implement", side_effect=_invoke),
            mock.patch.object(implement, "wl_add_comment", return_value=True),
        ):
            report = implement.phase_parent(PARENT_ID, json_output=True)

        assert report["success"] is True
        assert report.get("placement_violation") is not True
        assert report["next_child"] == CHILD_ID


class TestDebrisSkipIsReported:
    """AC6: skipping stale debris is reported, never silent.

    An operator who saw a tree block a run yesterday needs one log line saying
    why it no longer blocks, or the guard looks like it simply stopped working
    (WL-0MWQLHDOS006CAIH).
    """

    LOGGER_NAME = "implement.scripts.implement"

    def _skip_messages(
        self,
        rooted_repo: Path,
        canonical_child: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> list[str]:
        with caplog.at_level(logging.INFO, logger=self.LOGGER_NAME):
            implement._child_main_checkout_violation(
                CHILD_ID,
                str(canonical_child),
                PARENT_BRANCH,
                repo_root=str(rooted_repo),
            )
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == self.LOGGER_NAME
        ]

    def test_outside_root_skip_is_reported(
        self,
        rooted_repo: Path,
        canonical_child: Path,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        scratch = gs.add_worktree(
            rooted_repo,
            tmp_path / "scratch" / f"wl-{LIVE_ITEM}-scratch",
            f"wl-{LIVE_ITEM}-scratch",
            base=PARENT_BRANCH,
        )
        (scratch / "scratch-marker.js").write_text("x = 1\n", encoding="utf-8")

        messages = self._skip_messages(rooted_repo, canonical_child, caplog)

        assert any(
            "outside the repository's worktree root" in message
            and str(scratch) in message
            for message in messages
        ), messages

    def test_terminal_owner_skip_is_reported(
        self,
        rooted_repo: Path,
        canonical_child: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        leftover = _canonical_worktree(rooted_repo, DEBRIS_ITEM)
        (leftover / "index.ts").write_text("deleted-item work\n", encoding="utf-8")

        with _patch_wl_show(
            {DEBRIS_ITEM: {"id": DEBRIS_ITEM, "status": "completed"}}
        ):
            messages = self._skip_messages(
                rooted_repo, canonical_child, caplog
            )

        assert any(
            "is terminal" in message and str(leftover) in message
            for message in messages
        ), messages

    def test_a_live_dirty_sibling_reports_no_skip(
        self,
        rooted_repo: Path,
        canonical_child: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "stray.py").write_text("x = 1\n", encoding="utf-8")

        with _patch_wl_show({LIVE_ITEM: {"id": LIVE_ITEM, "status": "in-progress"}}):
            messages = self._skip_messages(rooted_repo, canonical_child, caplog)

        assert not any("Ignoring registered worktree" in m for m in messages)


# ---------------------------------------------------------------------------
# Per-worktree expected-dirty resolution and submodule-aware surfaces
# (RISE-0MWOIAXAG008HMK3: drive's sibling-worktree guard fails closed on
# pre-existing dirt in unrelated worktrees)
# ---------------------------------------------------------------------------
#
# Two residual false-positive sources survive the surrounding debris and
# spawn-failure work:
#
# 1. **Per-worktree declarations were ignored.** The sibling scan classified
#    every sibling with the *root* repo's ``ignoreDirtyPaths`` extension, so a
#    project's own per-worktree declaration could never take effect.
# 2. **Submodule noise was an opaque surface.** Every tce-the-rising worktree
#    reports `` M core`` because the worktree bootstrap materialises
#    ``core/node_modules`` inside the ``core`` submodule; the guard saw the
#    bare ``core`` entry and no path-prefix declaration could ever match it.
#
# Contract pinned here (parent AC1/AC2):
#
# - Each sibling surface is classified with *that worktree's own*
#   ``ignoreDirtyPaths`` declaration.
# - A dirty submodule entry is expanded into its inner changes and those are
#   classified, so framework-materialised noise is not a surface while a
#   genuine inner edit still fails closed.
# - The genuine cross-worktree write detection is unchanged (a sibling
#   without the declaration is still reported).


def _write_implement_extension(worktree: Path, ignore_dirty: list[str]) -> None:
    """Write a project-local implement extension into *worktree*."""
    ext_dir = worktree / ".pi" / "skills_extensions" / "implement"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "extension.json").write_text(
        json.dumps({"ignoreDirtyPaths": ignore_dirty}), encoding="utf-8"
    )


def _init_declared_repo(root: Path) -> None:
    """A repo whose ``.pi/`` extension directory is git-ignored.

    Ignoring ``.pi/`` keeps the per-worktree declaration file itself out of
    the dirty surface, so a test is about *classification*, not about the
    declaration file leaking into ``git status``.
    """
    gs.init_repo(root, default_branch=PARENT_BRANCH)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    (root / ".gitignore").write_text(".pi/\n", encoding="utf-8")
    gs.commit_all(root, "base")


@pytest.fixture()
def declared_repo(tmp_path: Path) -> Path:
    root = tmp_path / "declared-repo"
    _init_declared_repo(root)
    return root


@pytest.fixture()
def declared_child(declared_repo: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "dwt" / f"wl-{CHILD_ID}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    return gs.add_worktree(declared_repo, wt, f"wl-{CHILD_ID}", base=PARENT_BRANCH)


@pytest.fixture()
def declared_sibling(declared_repo: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "dwt2" / f"wl-{SIBLING_ID}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    return gs.add_worktree(declared_repo, wt, f"wl-{SIBLING_ID}", base=PARENT_BRANCH)


def _dirty_noise(sibling: Path, name: str = "generated-bundle.js") -> Path:
    """Create an undeclared dirty file in a sibling's ``noise/`` directory."""
    noise = sibling / "noise"
    noise.mkdir(exist_ok=True)
    target = noise / name
    target.write_text("bundled\n", encoding="utf-8")
    return target


class TestPerWorktreeExpectedDirtyResolution:
    """AC1: a sibling surface is classified with *its own* declaration."""

    def _child_violation(self, repo: Path, child: Path) -> str | None:
        with _patch_wl_show():
            return implement._child_main_checkout_violation(
                CHILD_ID, str(child), PARENT_BRANCH, repo_root=str(repo)
            )

    def test_sibling_with_its_own_declaration_is_not_a_surface(
        self, declared_repo: Path, declared_child: Path, declared_sibling: Path
    ) -> None:
        _write_implement_extension(declared_sibling, ["noise/"])
        _dirty_noise(declared_sibling)

        assert self._child_violation(declared_repo, declared_child) is None

    def test_identical_dirt_without_a_declaration_is_still_a_surface(
        self, declared_repo: Path, declared_child: Path, declared_sibling: Path
    ) -> None:
        # The genuine-detection floor is unchanged: no declaration, no exemption.
        _dirty_noise(declared_sibling)

        violation = self._child_violation(declared_repo, declared_child)

        assert violation is not None
        assert str(declared_sibling) in violation
        assert "noise" in violation

    def test_unrelated_declaration_does_not_mask_other_dirt(
        self, declared_repo: Path, declared_child: Path, declared_sibling: Path
    ) -> None:
        # A declaration is additive, not a blanket exemption for the worktree.
        _write_implement_extension(declared_sibling, ["other/"])
        _dirty_noise(declared_sibling)

        violation = self._child_violation(declared_repo, declared_child)

        assert violation is not None
        assert str(declared_sibling) in violation

    def test_main_checkout_uses_its_own_declaration_not_a_siblings(
        self, declared_repo: Path, declared_child: Path, declared_sibling: Path
    ) -> None:
        # The sibling declares ``noise/`` but only the *main checkout* is
        # dirty; the main checkout has no declaration, so it is still reported.
        _write_implement_extension(declared_sibling, ["noise/"])
        _dirty_noise(declared_repo)

        violation = self._child_violation(declared_repo, declared_child)

        assert violation is not None
        assert str(declared_repo) in violation

    def test_placement_guard_resolves_the_siblings_own_declaration(
        self, declared_repo: Path, declared_child: Path, declared_sibling: Path
    ) -> None:
        _write_implement_extension(declared_sibling, ["noise/"])
        _dirty_noise(declared_sibling)

        with _patch_wl_show():
            violation = implement._worktree_placement_violation(
                str(declared_child),
                cwd=str(declared_sibling),
                repo_root=str(declared_repo),
                parent_branch=PARENT_BRANCH,
            )

        assert violation is None

    def test_placement_guard_reports_undeclared_sibling_dirt(
        self, declared_repo: Path, declared_child: Path, declared_sibling: Path
    ) -> None:
        _dirty_noise(declared_sibling)

        with _patch_wl_show():
            violation = implement._worktree_placement_violation(
                str(declared_child),
                cwd=str(declared_sibling),
                repo_root=str(declared_repo),
                parent_branch=PARENT_BRANCH,
            )

        assert violation is not None
        assert str(declared_sibling) in violation


def _init_repo_with_submodule_noise(outer: Path, inner: Path) -> str:
    """Outer repo whose ``core`` gitlink carries only untracked inner noise.

    Reproduces the tce-the-rising shape: an outer repository with a ``core``
    submodule whose only inner change is framework-materialised (the worktree
    bootstrap links ``core/node_modules``). Returns the pinned inner sha.
    """
    gs.init_repo(inner, default_branch="main")
    (inner / "engine.txt").write_text("engine\n", encoding="utf-8")
    inner_sha = gs.commit_all(inner, "inner base")

    gs.init_repo(outer, default_branch=PARENT_BRANCH)
    (outer / "README.md").write_text("base\n", encoding="utf-8")
    (outer / ".gitignore").write_text(".pi/\n", encoding="utf-8")
    gs.commit_all(outer, "base")
    gs.run_git(
        outer,
        ["update-index", "--add", "--cacheinfo", f"160000,{inner_sha},core"],
        check=True,
    )
    # Commit the staged gitlink directly: ``git add -A`` would drop it, because
    # the submodule has no working-tree checkout yet and looks deleted.
    gs.run_git(outer, ["commit", "-q", "-m", "track core gitlink"], check=True)
    # Materialise a clean ``core`` checkout in the main checkout as well: an
    # unpopulated gitlink reports as `` D core`` there, which would otherwise
    # make the main checkout itself the offending surface.
    shutil.rmtree(outer / "core", ignore_errors=True)
    gs.run_git(outer, ["clone", "-q", str(inner), "core"], check=True)
    gs.run_git(outer / "core", ["checkout", "-q", inner_sha], check=True)
    return inner_sha


def _materialise_submodule(
    worktree: Path,
    inner: Path,
    inner_sha: str,
    *,
    node_modules: bool = True,
) -> Path:
    """Populate *worktree*'s ``core`` gitlink with a checkout of *inner*.

    Mimics ``implement.py start``'s worktree bootstrap: the submodule is
    checked out at the pinned revision and ``core/node_modules`` is
    materialised as a symlink to a shared install.
    """
    core = worktree / "core"
    shutil.rmtree(core, ignore_errors=True)
    gs.run_git(worktree, ["clone", "-q", str(inner), "core"], check=True)
    gs.run_git(core, ["checkout", "-q", inner_sha], check=True)
    if node_modules:
        shared = worktree.parent / "shared-node-modules"
        shared.mkdir(parents=True, exist_ok=True)
        os.symlink(shared, core / "node_modules")
    return core


class TestSubmoduleNoiseIsNotASurface:
    """AC2: a dirty submodule is classified by what changed *inside* it."""

    def _sandbox(self, tmp_path: Path) -> tuple[Path, Path, str, Path, Path]:
        outer = tmp_path / "outer"
        inner = tmp_path / "inner"
        inner_sha = _init_repo_with_submodule_noise(outer, inner)
        child = gs.add_worktree(
            outer, tmp_path / "swt" / f"wl-{CHILD_ID}", f"wl-{CHILD_ID}",
            base=PARENT_BRANCH,
        )
        sibling = gs.add_worktree(
            outer, tmp_path / "swt2" / f"wl-{SIBLING_ID}", f"wl-{SIBLING_ID}",
            base=PARENT_BRANCH,
        )
        return outer, inner, inner_sha, child, sibling

    def _child_violation(self, outer: Path, child: Path) -> str | None:
        with _patch_wl_show():
            return implement._child_main_checkout_violation(
                CHILD_ID, str(child), PARENT_BRANCH, repo_root=str(outer)
            )

    def test_expected_dirty_submodule_noise_is_not_a_surface(
        self, tmp_path: Path
    ) -> None:
        outer, inner, inner_sha, child, sibling = self._sandbox(tmp_path)
        _write_implement_extension(sibling, ["core/node_modules"])
        _materialise_submodule(sibling, inner, inner_sha)

        # Sanity: the outer status really does report the bare submodule entry
        # (the shape no path-prefix declaration could previously match).
        status = gs.run_git(sibling, ["status", "--porcelain=v1"]).stdout
        assert "core" in status

        assert self._child_violation(outer, child) is None

    def test_genuine_inner_edit_still_fails_closed(self, tmp_path: Path) -> None:
        outer, inner, inner_sha, child, sibling = self._sandbox(tmp_path)
        _write_implement_extension(sibling, ["core/node_modules"])
        core = _materialise_submodule(sibling, inner, inner_sha)
        (core / "engine.txt").write_text("changed\n", encoding="utf-8")

        violation = self._child_violation(outer, child)

        assert violation is not None
        assert str(sibling) in violation
        assert "engine.txt" in violation

    def test_submodule_noise_without_a_declaration_is_reported(
        self, tmp_path: Path
    ) -> None:
        # Nothing is silently swallowed: without the declaration the noise is
        # still a genuine change surface.
        outer, inner, inner_sha, child, sibling = self._sandbox(tmp_path)
        _materialise_submodule(sibling, inner, inner_sha)

        violation = self._child_violation(outer, child)

        assert violation is not None
        assert str(sibling) in violation
        assert "core/node_modules" in violation

    def test_placement_guard_ignores_expected_dirty_submodule_noise(
        self, tmp_path: Path
    ) -> None:
        outer, inner, inner_sha, child, sibling = self._sandbox(tmp_path)
        _write_implement_extension(sibling, ["core/node_modules"])
        _materialise_submodule(sibling, inner, inner_sha)

        with _patch_wl_show():
            violation = implement._worktree_placement_violation(
                str(child),
                cwd=str(sibling),
                repo_root=str(outer),
                parent_branch=PARENT_BRANCH,
            )

        assert violation is None

    def test_placement_guard_reports_genuine_inner_edit(self, tmp_path: Path) -> None:
        outer, inner, inner_sha, child, sibling = self._sandbox(tmp_path)
        _write_implement_extension(sibling, ["core/node_modules"])
        core = _materialise_submodule(sibling, inner, inner_sha)
        (core / "engine.txt").write_text("changed\n", encoding="utf-8")

        with _patch_wl_show():
            violation = implement._worktree_placement_violation(
                str(child),
                cwd=str(sibling),
                repo_root=str(outer),
                parent_branch=PARENT_BRANCH,
            )

        assert violation is not None
        assert "engine.txt" in violation

    def test_moved_submodule_pointer_is_never_silently_clean(
        self, tmp_path: Path
    ) -> None:
        # A submodule whose pointer advanced (inner tree clean, inner HEAD moved)
        # is a genuine change surface — never silently clean.
        outer, inner, inner_sha, child, sibling = self._sandbox(tmp_path)
        _write_implement_extension(sibling, ["core/node_modules"])
        core = _materialise_submodule(sibling, inner, inner_sha)
        (core / "engine.txt").write_text("advanced\n", encoding="utf-8")
        gs.run_git(core, ["commit", "-q", "-am", "advance engine"], check=True)

        # The submodule pointer advanced: its checked-out HEAD no longer
        # matches the pinned gitlink, so it is a genuine change surface even
        # though the inner tree holds only the expected-dirty noise.
        assert "core" in gs.run_git(sibling, ["status", "--porcelain=v1"]).stdout

        assert self._child_violation(outer, child) is not None

    def test_unreadable_inner_repo_is_never_silently_clean(
        self, tmp_path: Path
    ) -> None:
        # The fail-closed branch of the classifier: if the inner repo cannot be
        # read, the submodule entry stays a surface (reported as the bare path).
        _outer, inner, inner_sha, _child, sibling = self._sandbox(tmp_path)
        _materialise_submodule(sibling, inner, inner_sha)

        with mock.patch.object(
            implement, "_submodule_inner_paths", return_value=None
        ):
            is_surface, reported = implement._dirty_entry_is_change_surface(
                str(sibling), "core", ("core/node_modules",)
            )

        assert is_surface is True
        assert reported == ["core"]


class TestDriveRetriesProviderStreamFailure:
    """``phase_drive`` absorbs a transient ``Provider finish_reason: error``.

    Recovery-cost harness for ``WL-0MWTZEDWG008YMGQ`` AC4. The real
    ``phase_drive`` retry accounting is exercised with an injected spawner, so
    the attempt count is measured deterministically — no real ``pi`` session,
    no sleep (``delay`` is a zero-cost recorder).

    The comparison is the before/after recovery cost:

    * a genuine ``unknown`` failure — what the provider signature *was* before
      the classifier learned it — costs one spawn and hard-stops the chain
      (the operator then re-runs ``implement.py drive`` by hand);
    * the provider signature now consumes one bounded ``--infra-retry-limit``
      slot per transient reply and re-spawns the **same** child in the
      **same** worktree, so the epic proceeds in the same invocation.

    The retry accounting is the subject here; the classification itself is
    pinned in ``test_spawn_failure_classification.py``. Release/reconcile side
    effects are stubbed so the harness never writes to a real worklog store.
    """

    PROVIDER_REASON = (
        f"Child session for {CHILD_ID} exited with code 1: Provider "
        "finish_reason: error"
    )
    GENUINE_REASON = (
        f"Child session for {CHILD_ID} exited with code 1: "
        "ModuleNotFoundError: No module named 'nonexistent_module'"
    )

    def _drive(
        self,
        child_worktree: Path,
        *,
        reason: str,
        infra_retry_limit: int,
        transient_failures: int | None = None,
    ) -> tuple[dict, int]:
        """Drive one child; return ``(report, spawn_count)``.

        ``transient_failures=None`` means "always fail"; an integer means
        "fail this many times, then succeed" so the chain can advance.
        """
        child_item = _child("in-progress")
        spawns: list[str] = []
        succeeded = {"value": False}

        def _wl_show(work_item_id: str) -> dict:
            if work_item_id == PARENT_ID:
                return {
                    "id": PARENT_ID,
                    "title": "Parent",
                    "status": "plan_complete",
                    "stage": "plan_complete",
                }
            return dict(child_item)

        def _spawner(child_id, worktree_path, timeout, env, verbose=False):
            spawns.append(child_id)
            assert child_id == CHILD_ID
            assert worktree_path == str(child_worktree)
            if transient_failures is not None and len(spawns) > transient_failures:
                succeeded["value"] = True
                return {"success": True, "returncode": 0, "reason": ""}
            return {"success": False, "returncode": 1, "reason": reason}

        def _parent(*_args, **_kwargs) -> dict:
            if succeeded["value"]:
                return {
                    "success": True,
                    "parent_advanced": True,
                    "message": "all children terminal",
                }
            return {
                "success": True,
                "next_child": CHILD_ID,
                "worktree_path": str(child_worktree),
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
            mock.patch.object(
                implement,
                "_release_provider_failed_child",
                return_value={"released": False, "reason": "test stub"},
            ),
            mock.patch.object(
                implement,
                "_release_orphaned_child_claim",
                return_value={"released": False, "reason": "test stub"},
            ),
            mock.patch.object(
                implement,
                "record_spawn_failure",
                return_value={"child_id": CHILD_ID, "reason": reason},
            ),
            mock.patch.object(
                implement,
                "_detect_stranded_child",
                return_value={"stranded": False},
            ),
            mock.patch.object(
                implement, "_maybe_remove_child_worktree", return_value=None
            ),
        ):
            report = implement.phase_drive(
                PARENT_ID,
                parent_runner=_parent,
                spawn_session=_spawner,
                infra_retry_limit=infra_retry_limit,
                delay=lambda _seconds: None,
                child_timeout=60,
            )
        return report, len(spawns)

    def test_a_transient_reply_is_retried_and_the_epic_proceeds(
        self, child_worktree: Path
    ) -> None:
        report, spawn_count = self._drive(
            child_worktree,
            reason=self.PROVIDER_REASON,
            infra_retry_limit=5,
            transient_failures=1,
        )

        # Before: one transient reply hard-stopped the chain (1 spawn, then a
        # manual re-run). After: one bounded re-spawn in the same worktree and
        # the drive completes.
        assert spawn_count == 2
        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["infra_retries"][CHILD_ID] == 1
        retries = [
            entry
            for entry in report["children_driven"]
            if entry.get("result") == "infra-retry"
            and entry.get("step") == "spawn"
        ]
        assert len(retries) == 1
        assert retries[0]["attempt"] == 1
        assert "Provider finish_reason: error" in retries[0]["reason"]

    def test_the_retry_budget_stays_bounded(self, child_worktree: Path) -> None:
        report, spawn_count = self._drive(
            child_worktree,
            reason=self.PROVIDER_REASON,
            infra_retry_limit=2,
        )

        # 1 initial attempt + 2 infra retries, then the bound stops the chain.
        # No new retry budget is introduced: this is the shared
        # --infra-retry-limit.
        assert spawn_count == 3
        assert report["failed_child"] == CHILD_ID
        assert report["infra_retries"][CHILD_ID] == 2

    def test_a_genuine_defect_is_never_retried(
        self, child_worktree: Path
    ) -> None:
        report, spawn_count = self._drive(
            child_worktree,
            reason=self.GENUINE_REASON,
            infra_retry_limit=5,
        )

        # Fail-safe floor: a real defect costs one spawn and hard-stops; the
        # infra budget is never touched.
        assert spawn_count == 1
        assert report["failed_child"] == CHILD_ID
        assert report["infra_retries"].get(CHILD_ID, 0) == 0
        assert report["children_driven"][0]["spawn_failure_kind"] == "unknown"


# ---------------------------------------------------------------------------
# Infra-retry with a live sibling worktree: the driver's classification — not
# another item's mid-flight tree — decides attribution
# (WL-0MWWBIXFQ004LZST, parent WL-0MWW96TNJ007EQ9F)
# ---------------------------------------------------------------------------
#
# Incident: ``implement.py drive`` classified a child's first session failure
# as ``infrastructure`` (``Provider finish_reason: error``) and re-ran the
# parent phase on the shared ``--infra-retry-limit`` budget — but recorded no
# spawn-failure evidence on that path (only the budget-exhausted path did).
# The re-run parent phase therefore saw a *clean* child worktree plus a live
# sibling worktree holding commits ahead of ``dev``, attributed the sibling's
# work to the failed child and stopped the whole drive chain with a false
# accusation against an unrelated item.
#
# Fix slice (this item): the two expectations that needed
# WL-0MWWBJJBD008TBPB now assert the fixed behaviour directly (the evidence is
# recorded before the retry ``continue``), so they are no longer ``xfail``.
# The companions — the guard still fails closed without evidence, and a child
# holding its own work never reaches the exemption — pass before **and** after
# the fix, so a failing test can only be failing on the behaviour under test.


class TestInfraRetryWithLiveSiblingWorktree:
    """A retried infrastructure failure is never a stray-write verdict.

    Real git repositories (the shared :mod:`shared.git_sandbox` factories) and
    an injected spawner keep the harness hermetic: no ``pi`` session is
    spawned, no wall-clock backoff is slept (``delay`` is a zero-cost
    recorder) and no real worklog store is written (``wl_add_comment`` and the
    release/reconcile helpers are stubbed). The re-run parent phase consults
    the **real** :func:`implement._child_main_checkout_violation`, exactly as
    ``phase_parent`` does for a non-terminal child with no work of its own.

    ``record_spawn_failure`` is deliberately **not** stubbed: whether the
    retry path writes that evidence is the behaviour under test.
    """

    PROVIDER_REASON = (
        f"Child session for {CHILD_ID} exited with code 1: Provider "
        "finish_reason: error"
    )

    def _drive(
        self,
        child_worktree: Path,
        *,
        infra_retry_limit: int = 5,
        transient_failures: int = 1,
    ) -> tuple[dict, int, list[str | None]]:
        """Drive one child while a live sibling worktree is ahead of ``dev``.

        ``transient_failures=1`` means "the first spawn fails with the
        retryable provider signature, the second succeeds". Returns
        ``(report, spawn_count, violations)``, where *violations* records what
        the real placement guard returned on every **re-run** parent phase
        (one entry per parent call after the first, which is the pre-spawn
        "start the child" decision).
        """
        child_item = _child("in-progress")
        spawns: list[str] = []
        violations: list[str | None] = []
        parent_calls = {"value": 0}
        succeeded = {"value": False}

        def _wl_show(work_item_id: str) -> dict:
            if work_item_id == PARENT_ID:
                return {
                    "id": PARENT_ID,
                    "title": "Parent",
                    "status": "plan_complete",
                    "stage": "plan_complete",
                }
            return dict(child_item)

        def _spawner(child_id, worktree_path, timeout, env, verbose=False):
            spawns.append(child_id)
            assert child_id == CHILD_ID
            assert worktree_path == str(child_worktree)
            if len(spawns) > transient_failures:
                succeeded["value"] = True
                return {"success": True, "returncode": 0, "reason": ""}
            return {
                "success": False,
                "returncode": 1,
                "reason": self.PROVIDER_REASON,
            }

        def _parent(*_args, **_kwargs) -> dict:
            parent_calls["value"] += 1
            if succeeded["value"]:
                return {
                    "success": True,
                    "parent_advanced": True,
                    "message": "all children terminal",
                }
            if parent_calls["value"] > 1:
                # The re-run parent phase, after the infra-retry ``continue``:
                # this is where ``phase_parent`` runs the placement guard.
                violation = implement._child_main_checkout_violation(
                    CHILD_ID, str(child_worktree), PARENT_BRANCH
                )
                violations.append(violation)
                if violation:
                    return {
                        "success": False,
                        "placement_violation": True,
                        "next_child": CHILD_ID,
                        "message": violation,
                    }
            return {
                "success": True,
                "next_child": CHILD_ID,
                "worktree_path": str(child_worktree),
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
            mock.patch.object(
                implement,
                "_release_provider_failed_child",
                return_value={"released": False, "reason": "test stub"},
            ),
            mock.patch.object(
                implement,
                "_release_orphaned_child_claim",
                return_value={"released": False, "reason": "test stub"},
            ),
            mock.patch.object(
                implement,
                "_detect_stranded_child",
                return_value={"stranded": False},
            ),
            mock.patch.object(
                implement, "_maybe_remove_child_worktree", return_value=None
            ),
        ):
            report = implement.phase_drive(
                PARENT_ID,
                parent_runner=_parent,
                spawn_session=_spawner,
                infra_retry_limit=infra_retry_limit,
                delay=lambda _seconds: None,
                child_timeout=60,
            )
        return report, len(spawns), violations

    def test_committed_live_sibling_still_fails_closed_without_evidence(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """Companion (unchanged): no failure evidence → the guard still fires.

        Pins the genuine cross-worktree-write detection
        (SA-0MW5FIJNQ002Z24I) on exactly the fixture the new exemption tests
        use, and pins that the *driver's* evidence — only ever written for a
        child that produced no work — is what turns the same surface into
        pre-existing dirt. Passing before and after the fix makes the
        ``xfail`` baselines unambiguous.
        """
        _commit(sibling_worktree, "sibling-work.py", branch_tag=SIBLING_ID)

        # Ownership stays unresolved (an empty lookup), so the sibling keeps
        # counting as a foreign surface without a real worklog subprocess.
        with mock.patch.object(implement, "wl_show", return_value={}):
            violation = implement._child_main_checkout_violation(
                CHILD_ID, str(child_worktree), PARENT_BRANCH
            )
            assert violation is not None
            assert str(sibling_worktree) in violation
            assert "commits ahead of dev" in violation

            implement.record_spawn_failure(
                str(child_worktree), CHILD_ID, self.PROVIDER_REASON
            )
            assert (
                implement._child_main_checkout_violation(
                    CHILD_ID, str(child_worktree), PARENT_BRANCH
                )
                is None
            )

    def test_the_retry_records_evidence_before_re_running_the_parent(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """AC1: the exemption exists from the moment of the classification."""
        _commit(sibling_worktree, "sibling-work.py", branch_tag=SIBLING_ID)

        report, spawn_count, violations = self._drive(child_worktree)

        # The retry ran on the infra budget: same child, same worktree.
        assert spawn_count == 2
        assert report["infra_retries"][CHILD_ID] == 1
        evidence = implement.read_spawn_failure(str(child_worktree), CHILD_ID)
        assert evidence is not None, (
            "the failed session left no spawn-failure evidence before the "
            "retry, so the re-run parent phase cannot tell this child from "
            "one that wrote into the sibling worktree"
        )
        assert evidence["child_id"] == CHILD_ID
        assert "Provider finish_reason: error" in evidence["reason"]
        # The evidence never counts as the child's own work.
        assert (
            implement._classify_child_worktree(
                CHILD_ID, str(child_worktree), PARENT_BRANCH
            )
            == implement.CHILD_CLEAN
        )
        assert violations == [None]

    def test_a_live_sibling_ahead_of_dev_never_stops_the_retry(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """AC2: the retried child is startable again, not accused."""
        _commit(sibling_worktree, "sibling-work.py", branch_tag=SIBLING_ID)
        # The sibling is a *live* surface holding work of its own: the exact
        # shape that used to be attributed to the failed child.
        assert (
            implement._git_path_has_changes(sibling_worktree, PARENT_BRANCH)
            is True
        )
        assert (
            implement._git_path_has_changes(child_worktree, PARENT_BRANCH)
            is False
        )

        report, spawn_count, violations = self._drive(child_worktree)

        assert spawn_count == 2
        # The re-run parent phase must not report a placement violation, so
        # the chain proceeds instead of stopping on a false accusation.
        assert violations == [None]
        assert report.get("placement_violation") is not True
        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report.get("failed_child") in (None, "")

    def test_a_child_holding_its_own_work_never_earns_the_exemption(
        self, child_worktree: Path, sibling_worktree: Path
    ) -> None:
        """AC4: ``CHILD_DIRTY``/``CHILD_AHEAD`` never reach the exemption."""
        _commit(sibling_worktree, "sibling-work.py", branch_tag=SIBLING_ID)
        _commit(child_worktree, "own-work.py", branch_tag=CHILD_ID)
        assert (
            implement._classify_child_worktree(
                CHILD_ID, str(child_worktree), PARENT_BRANCH
            )
            == implement.CHILD_WORK
        )

        report, spawn_count, violations = self._drive(child_worktree)

        # The retry still happens on the infra budget …
        assert spawn_count == 2
        assert report["infra_retries"][CHILD_ID] == 1
        # … but a child that produced its own work never earns the record the
        # exemption relies on (whether or not the fix is in place).
        assert implement.read_spawn_failure(str(child_worktree), CHILD_ID) is None
        # Its own work short-circuits the guard first (condition 3), so the
        # sibling surface is never even attributed to it.
        assert violations == [None]


# ---------------------------------------------------------------------------
# Ambient-dirt baselines: a never-spawned child is not blamed for pre-existing
# dirt (SA-0MWL0XX88000E38K)
# ---------------------------------------------------------------------------
#
# Incident: ``implement.py drive <epic>`` refused the next child **before**
# spawning any session because a dirty main checkout and several dirty sibling
# worktrees (one owned by a still-open item) were attributed to the child. Its
# own worktree was simply empty — its session had not started.
#
# Contract pinned here:
#
# - ``_snapshot_foreign_surfaces`` captures every foreign surface (the main
#   checkout plus each registered sibling worktree) deterministically.
# - ``_foreign_surface_offenders_since`` reports only paths/commits that are
#   new or changed since the baseline.
# - A never-spawned child (no baseline yet) has an ambient baseline recorded
#   by ``phase_parent`` and is reported startable (parent AC1).
# - ``phase_drive`` refreshes the baseline at the spawn boundary and names no
#   unrelated sibling as an offender (parent AC3).
# - A spawned session's post-baseline write still fails closed into the main
#   checkout or a sibling worktree, even with other pre-existing dirt present
#   (parent AC2).
# - A missing/corrupt/foreign baseline falls back to the legacy full guard.


class TestForeignSurfaceSnapshotPrimitive:
    """The snapshot/comparison primitive is deterministic and precise."""

    def test_snapshot_covers_main_and_registered_siblings(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (rooted_repo / "ambient-main.txt").write_text("x\n", encoding="utf-8")
        (live / "ambient-sibling.ts").write_text("x\n", encoding="utf-8")

        snapshot = implement._snapshot_foreign_surfaces(
            rooted_repo, canonical_child, PARENT_BRANCH
        )

        assert str(rooted_repo) in snapshot
        assert str(live) in snapshot
        # The child's own worktree is never a foreign surface.
        assert str(canonical_child) not in snapshot
        assert snapshot[str(rooted_repo)]["dirty"] == ["ambient-main.txt"]
        assert snapshot[str(live)]["dirty"] == ["ambient-sibling.ts"]
        assert snapshot[str(rooted_repo)]["head"]
        # Deterministic ordering keeps the persisted record byte-stable.
        assert list(snapshot) == sorted(snapshot)

    def test_only_new_dirt_is_reported_since_the_baseline(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (rooted_repo / "pre-existing-main.txt").write_text("x\n", encoding="utf-8")
        (live / "pre-existing-sibling.ts").write_text("x\n", encoding="utf-8")
        baseline = implement._snapshot_foreign_surfaces(
            rooted_repo, canonical_child, PARENT_BRANCH
        )

        (rooted_repo / "new-main.txt").write_text("x\n", encoding="utf-8")
        current = implement._snapshot_foreign_surfaces(
            rooted_repo, canonical_child, PARENT_BRANCH
        )
        offenders = implement._foreign_surface_offenders_since(baseline, current)

        by_surface = {o["surface"]: o for o in offenders}
        assert set(by_surface) == {str(rooted_repo)}
        assert by_surface[str(rooted_repo)]["paths"] == ["new-main.txt"]

    def test_a_surface_that_gained_commits_is_an_offender(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        baseline = implement._snapshot_foreign_surfaces(
            rooted_repo, canonical_child, PARENT_BRANCH
        )

        _commit(live, "sibling-commit.ts", branch_tag=LIVE_ITEM)
        current = implement._snapshot_foreign_surfaces(
            rooted_repo, canonical_child, PARENT_BRANCH
        )
        offenders = implement._foreign_surface_offenders_since(baseline, current)

        by_surface = {o["surface"]: o for o in offenders}
        assert str(live) in by_surface
        assert by_surface[str(live)]["ahead_added"] is True


class TestSessionBaselineRecord:
    """The persisted record never counts as work and is never oversized."""

    def test_baseline_record_never_counts_as_work(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        implement.record_session_baseline(
            rooted_repo, canonical_child, CHILD_ID, PARENT_BRANCH
        )

        assert implement.session_baseline_exists(canonical_child)
        # The record lives under ``.worklog/`` and must not turn the clean
        # child into a dirty/ahead surface.
        assert (
            implement._git_path_has_changes(canonical_child, PARENT_BRANCH)
            is False
        )
        assert (
            implement._classify_child_worktree(
                CHILD_ID, str(canonical_child), PARENT_BRANCH
            )
            == implement.CHILD_CLEAN
        )
        snapshot = implement._snapshot_foreign_surfaces(
            rooted_repo, canonical_child, PARENT_BRANCH
        )
        assert all(
            not p.startswith(".worklog/")
            for entry in snapshot.values()
            for p in entry["dirty"]
        )

    def test_a_foreign_child_record_is_never_trusted(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        implement.record_session_baseline(
            rooted_repo, canonical_child, CHILD_ID, PARENT_BRANCH
        )

        assert implement.read_session_baseline(canonical_child, CHILD_ID) is not None
        assert (
            implement.read_session_baseline(
                canonical_child, "SA-OTHERCHILD00001"
            )
            is None
        )


class TestNeverSpawnedChildIgnoresAmbientDirt:
    """AC1: ambient dirt is never attributed to a child with no session."""

    def test_dirty_main_and_live_sibling_do_not_block_the_next_child(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        (rooted_repo / "ambient-main.txt").write_text(
            "in flight\n", encoding="utf-8"
        )
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "ambient-sibling.ts").write_text("in flight\n", encoding="utf-8")
        assert not implement.session_baseline_exists(canonical_child)

        report, status_calls, invoked = _run_phase_parent(canonical_child)

        assert report["success"] is True
        assert report.get("placement_violation") is not True
        assert report["next_child"] == CHILD_ID
        # No unrelated sibling is named as an offender.
        assert str(live) not in report.get("message", "")
        # An ambient baseline is now recorded so a later pass never
        # re-attributes the same pre-existing dirt.
        assert implement.session_baseline_exists(canonical_child)
        assert invoked == []
        assert status_calls == []

    def test_phase_parent_records_an_ambient_baseline_when_none_exists(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        (rooted_repo / "ambient-main.txt").write_text("x\n", encoding="utf-8")
        assert not implement.session_baseline_exists(canonical_child)

        report, _status_calls, _invoked = _run_phase_parent(canonical_child)

        assert report["success"] is True
        assert report.get("placement_violation") is not True
        assert implement.session_baseline_exists(canonical_child)


class TestDriveSpawnsDespiteAmbientDirt:
    """AC3: ``drive`` spawns the next child; no sibling is blamed."""

    def _drive(
        self, rooted_repo: Path, canonical_child: Path, live: Path
    ) -> tuple[dict, list[str], dict]:
        child_item = _child("in-progress")
        spawns: list[str] = []
        at_spawn: dict = {}

        def _wl_show(work_item_id: str) -> dict:
            if work_item_id == PARENT_ID:
                return {
                    "id": PARENT_ID,
                    "title": "Parent",
                    "status": "plan_complete",
                    "stage": "plan_complete",
                }
            return dict(child_item)

        def _spawner(child_id, worktree_path, timeout, env, verbose=False):
            spawns.append(child_id)
            # The baseline must already be refreshed when the session starts.
            at_spawn["baseline"] = implement.read_session_baseline(
                str(canonical_child), CHILD_ID
            )
            return {
                "success": False,
                "returncode": 1,
                "reason": SPAWN_FAILURE_REASON,
            }

        parent_report = {
            "success": True,
            "next_child": CHILD_ID,
            "worktree_path": str(canonical_child),
        }
        with (
            mock.patch.object(implement, "wl_show", side_effect=_wl_show),
            mock.patch.object(
                implement, "wl_show_children", return_value=[child_item]
            ),
            mock.patch.object(
                implement, "_discover_worktree", return_value=str(canonical_child)
            ),
            mock.patch.object(implement, "wl_add_comment", return_value=True),
            mock.patch.object(
                implement,
                "_release_provider_failed_child",
                return_value={"released": False, "reason": "test stub"},
            ),
            mock.patch.object(
                implement,
                "_release_orphaned_child_claim",
                return_value={"released": False, "reason": "test stub"},
            ),
            mock.patch.object(
                implement, "_detect_stranded_child", return_value={"stranded": False}
            ),
            mock.patch.object(
                implement, "_maybe_remove_child_worktree", return_value=None
            ),
        ):
            report = implement.phase_drive(
                PARENT_ID,
                parent_runner=lambda *a, **k: dict(parent_report),
                spawn_session=_spawner,
                child_timeout=60,
                delay=lambda _seconds: None,
            )
        return report, spawns, at_spawn

    def test_drive_spawns_and_names_no_sibling_offender(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        (rooted_repo / "ambient-main.txt").write_text(
            "in flight\n", encoding="utf-8"
        )
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "ambient-sibling.ts").write_text("in flight\n", encoding="utf-8")

        report, spawns, at_spawn = self._drive(
            rooted_repo, canonical_child, live
        )

        assert spawns == [CHILD_ID]
        assert at_spawn["baseline"] is not None
        assert report.get("placement_violation") is not True
        assert str(live) not in json.dumps(report)
        assert str(rooted_repo) not in json.dumps(
            [e.get("reason", "") for e in report.get("children_driven", [])]
        )


class TestBaselineAwareGenuineStray:
    """AC2: a spawned session's post-baseline write still fails closed."""

    def _violation(self, rooted_repo: Path, canonical_child: Path, baseline):
        with _patch_wl_show({LIVE_ITEM: {"id": LIVE_ITEM, "status": "in-progress"}}):
            return implement._child_main_checkout_violation(
                CHILD_ID,
                str(canonical_child),
                PARENT_BRANCH,
                repo_root=str(rooted_repo),
                baseline=baseline,
            )

    def test_post_baseline_main_checkout_write_still_fails_closed(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        (rooted_repo / "preexisting-main.txt").write_text("x\n", encoding="utf-8")
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "preexisting-sibling.ts").write_text("x\n", encoding="utf-8")
        baseline = _establish_baseline(canonical_child)

        (rooted_repo / "stray-main.txt").write_text("x\n", encoding="utf-8")
        violation = self._violation(rooted_repo, canonical_child, baseline)

        assert violation is not None
        assert "stray-main.txt" in violation
        assert "preexisting-main.txt" not in violation
        assert str(canonical_child) in violation

    def test_post_baseline_sibling_write_still_fails_closed(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        (rooted_repo / "preexisting-main.txt").write_text("x\n", encoding="utf-8")
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "preexisting-sibling.ts").write_text("x\n", encoding="utf-8")
        baseline = _establish_baseline(canonical_child)

        (live / "stray-sibling.ts").write_text("x\n", encoding="utf-8")
        violation = self._violation(rooted_repo, canonical_child, baseline)

        assert violation is not None
        assert "stray-sibling.ts" in violation
        assert str(live) in violation
        assert "preexisting-sibling.ts" not in violation

    def test_preexisting_dirt_only_is_no_violation(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        (rooted_repo / "preexisting-main.txt").write_text("x\n", encoding="utf-8")
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "preexisting-sibling.ts").write_text("x\n", encoding="utf-8")
        baseline = _establish_baseline(canonical_child)

        assert self._violation(rooted_repo, canonical_child, baseline) is None

    def test_sibling_commits_after_the_baseline_still_fail_closed(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        baseline = _establish_baseline(canonical_child)

        _commit(live, "sibling-commit.ts", branch_tag=LIVE_ITEM)
        violation = self._violation(rooted_repo, canonical_child, baseline)

        assert violation is not None
        assert str(live) in violation
        assert "commits ahead of dev" in violation

    def test_corrupt_baseline_falls_back_to_the_legacy_guard(
        self, rooted_repo: Path, canonical_child: Path
    ) -> None:
        live = _canonical_worktree(rooted_repo, LIVE_ITEM)
        (live / "stray.ts").write_text("x\n", encoding="utf-8")
        # A corrupt record exists: it must never silently exempt the child.
        path = implement._session_baseline_path(canonical_child)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")

        report, _status_calls, _invoked = _run_phase_parent(canonical_child)

        assert report["success"] is False
        assert report["placement_violation"] is True
        assert str(live) in report["message"]
