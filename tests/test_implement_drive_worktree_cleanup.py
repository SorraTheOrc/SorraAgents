"""Driver-side worktree removal for driven children (SA-0MVTE8XAJ002IUDR).

A driven child's ``finish`` defers worktree removal: the worktree **is** the
child session's cwd, so deleting it inside ``finish`` leaves that session
unable to run any command (Pi's bash tool refuses a missing cwd). The driver
therefore owns removal and performs it once the child's session has exited —
the contract pinned here:

- **AC5 — driver removes after session exit.** For a terminal child with a
  clean worktree the driver runs ``cleanup_worktree_processes`` +
  ``_remove_worktree`` exactly once and records ``worktree_removed: true`` on
  that child's report entry; a non-terminal child gets no removal attempt and
  its retained path is recorded; a terminal child whose worktree is dirty is
  retained, never removed.
- **AC5 (production path).** The removal hook also runs on the
  ``completed-late`` branch — the exact path the recorded incident took (the
  session was killed after its push).
- **AC6 — the driver is robust.** An already-absent worktree is a reported
  no-op, and a failing removal is recorded without turning a terminal child
  into a drive failure (the chain continues).
- **AC7 — no leak on interruption.** A drive sweeps leftover worktrees of
  *terminal* children that already exist when it starts, and every retained
  tree is named in the report with a copy-pasteable remedy.

The parent runner and the session spawner are injected and every external
boundary (worklog, git, process cleanup, removal) is patched, so the
orchestration loop is exercised deterministically — no real session, no real
removal.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

PARENT = "SA-PARENT"
TERMINAL = {"status": "completed", "stage": "in_review"}
NON_TERMINAL = {"status": "in_progress", "stage": "in_progress"}


@pytest.fixture(autouse=True)
def _clean_drive_env(monkeypatch):
    """Keep the tests hermetic against a driven host session."""
    monkeypatch.delenv(implement.DRIVE_RECURSION_ENV, raising=False)
    monkeypatch.delenv(implement.KEEP_WORKTREE_ENV, raising=False)


class _ScriptedParent:
    """Return a scripted sequence of ``parent`` reports, one per call."""

    def __init__(self, reports: list[dict]):
        self.reports = list(reports)
        self.calls: list[tuple] = []

    def __call__(self, action, work_item_id, no_refactor=False, verbose=False):
        self.calls.append((action, work_item_id))
        if not self.reports:
            raise AssertionError("parent called more times than scripted")
        return self.reports.pop(0)


class _RecordingSpawner:
    """Record spawn calls and return scripted (or success) results."""

    def __init__(self, results: list[dict] | None = None):
        self.results = list(results or [])
        self.calls: list[dict] = []

    def __call__(self, child_id, worktree_path, timeout, env, verbose=False):
        self.calls.append({"id": child_id, "worktree_path": worktree_path})
        if self.results:
            return self.results.pop(0)
        return {"success": True, "returncode": 0, "reason": ""}


def _child_report(cid: str, wt: str) -> dict:
    return {"success": True, "next_child": cid, "worktree_path": wt}


def _worktree(tmp_path: Path, name: str) -> str:
    """Create a real directory so the existence gate passes."""
    path = tmp_path / name
    path.mkdir(parents=True)
    return str(path)


def _run_drive(
    tmp_path: Path,
    *,
    reports: list[dict],
    children: list[dict],
    child_states: dict[str, dict],
    spawner: _RecordingSpawner | None = None,
    discovered: dict[str, str] | None = None,
    dirty: set[str] | None = None,
    removal_side_effect=None,
    **kwargs,
):
    """Run ``phase_drive`` with every external boundary patched.

    Args:
        tmp_path: Fixture directory.
        reports: Scripted ``parent`` reports.
        children: The parent's children (``wl show --children`` shape).
        child_states: Per-child work-item dict returned by ``wl_show``.
        spawner: Injectable spawner (defaults to an always-successful one).
        discovered: ``_discover_worktree`` answers, per work item id.
        dirty: Worktree paths reported as having uncommitted changes.
        removal_side_effect: Optional side effect for ``_remove_worktree``.

    Returns:
        ``(report, mocks)`` where *mocks* exposes the recorded calls.
    """
    parent_runner = _ScriptedParent(reports)
    spawner = spawner or _RecordingSpawner()
    discovered = discovered or {}
    dirty = set(dirty or ())

    def _show(work_item_id):
        if work_item_id == PARENT:
            return {"id": PARENT}
        return child_states.get(work_item_id, {})

    removal_kwargs: dict = {"return_value": True}
    if removal_side_effect is not None:
        removal_kwargs = {"side_effect": removal_side_effect}

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(implement, "wl_show", side_effect=_show))
        stack.enter_context(
            mock.patch.object(implement, "wl_show_children", return_value=children)
        )
        stack.enter_context(
            mock.patch.object(
                implement,
                "_discover_worktree",
                side_effect=lambda wid: discovered.get(wid),
            )
        )
        stack.enter_context(mock.patch.object(implement, "_get_repo_root", return_value="/repo"))
        stack.enter_context(
            mock.patch.object(
                implement,
                "_worktree_has_uncommitted_changes",
                side_effect=lambda path: path in dirty,
            )
        )
        cleanup = stack.enter_context(
            mock.patch.object(implement, "cleanup_worktree_processes")
        )
        removal = stack.enter_context(
            mock.patch.object(implement, "_remove_worktree", **removal_kwargs)
        )
        comments: list[tuple[str, str]] = []
        stack.enter_context(
            mock.patch.object(
                implement,
                "wl_add_comment",
                side_effect=lambda wid, text: comments.append((wid, text)) or True,
            )
        )
        report = implement.phase_drive(
            PARENT,
            parent_runner=parent_runner,
            spawn_session=spawner,
            **kwargs,
        )
    report["_comments"] = comments
    return report, {"cleanup": cleanup, "removal": removal, "spawner": spawner}


def _processed(cid: str, status: str = "completed", action: str = "skipped-terminal") -> dict:
    """A ``children_processed`` entry as ``phase_parent`` reports it."""
    return {"id": cid, "title": cid, "action": action, "status": status}


def _advanced(processed: list[dict] | None = None) -> dict:
    """A parent report that advanced; it names every child's final status."""
    return {
        "success": True,
        "parent_advanced": True,
        "children_processed": list(processed or []),
        "message": "advanced",
    }


# ---------------------------------------------------------------------------
# AC5 — the driver removes a terminal child's clean worktree, exactly once
# ---------------------------------------------------------------------------


def test_driver_removes_terminal_child_worktree(tmp_path):
    """A finished, clean child's tree is removed once, after the session exits."""
    wt = _worktree(tmp_path, "wl-child-1")

    report, mocks = _run_drive(
        tmp_path,
        reports=[_child_report("SA-C1", wt), _advanced([_processed("SA-C1")])],
        children=[{"id": "SA-C1", "status": "completed"}],
        child_states={"SA-C1": TERMINAL},
    )

    assert report["success"] is True
    assert report["parent_advanced"] is True
    assert mocks["cleanup"].call_count == 1
    assert mocks["removal"].call_count == 1
    assert mocks["removal"].call_args.args[0] == wt

    entry = report["children_driven"][0]
    assert entry["worktree_removed"] is True
    assert entry["worktree_cleanup"]["removed"] is True
    assert "retained_worktrees" not in report


def test_driver_keeps_non_terminal_child_worktree(tmp_path):
    """A non-terminal child keeps its tree as evidence (no removal attempt)."""
    wt = _worktree(tmp_path, "wl-child-1")

    report, mocks = _run_drive(
        tmp_path,
        reports=[
            _child_report("SA-C1", wt),
            _advanced([_processed("SA-C1", status="in_progress", action="")]),
        ],
        children=[{"id": "SA-C1", "status": "open"}],
        child_states={"SA-C1": NON_TERMINAL},
    )

    assert mocks["removal"].call_count == 0
    entry = report["children_driven"][0]
    assert entry["worktree_removed"] is False
    assert entry["worktree_retained"] == wt
    assert [r["id"] for r in report["retained_worktrees"]] == ["SA-C1"]


def test_driver_never_removes_a_dirty_worktree(tmp_path):
    """Uncommitted work is never discarded — the tree is retained and reported."""
    wt = _worktree(tmp_path, "wl-child-1")

    report, mocks = _run_drive(
        tmp_path,
        reports=[_child_report("SA-C1", wt), _advanced([_processed("SA-C1")])],
        children=[{"id": "SA-C1", "status": "completed"}],
        child_states={"SA-C1": TERMINAL},
        dirty={wt},
    )

    assert mocks["cleanup"].call_count == 0
    assert mocks["removal"].call_count == 0
    entry = report["children_driven"][0]
    assert entry["worktree_removed"] is False
    assert entry["worktree_cleanup"]["retained_dirty"] is True
    retained = report["retained_worktrees"]
    assert retained[0]["path"] == wt
    assert "git worktree remove" in retained[0]["remedy"]
    assert "git worktree prune" in retained[0]["remedy"]


def test_driver_removes_worktree_on_the_completed_late_path(tmp_path):
    """The recorded incident path: a timed-out session whose child is terminal.

    The ``completed-late`` ``continue`` branch bypasses the spawn-success
    path, so the removal hook must run there too — otherwise the deferred
    worktree of every killed-but-finished child leaks.
    """
    wt = _worktree(tmp_path, "wl-child-1")
    spawner = _RecordingSpawner([
        {"success": False, "returncode": 143, "reason": "killed"},
    ])

    report, mocks = _run_drive(
        tmp_path,
        reports=[_child_report("SA-C1", wt), _advanced()],
        children=[{"id": "SA-C1", "status": "completed"}],
        child_states={"SA-C1": TERMINAL},
        spawner=spawner,
    )

    assert report["children_driven"][0]["result"] == "completed-late"
    assert report["completed_late"] == 1
    assert mocks["cleanup"].call_count == 1
    assert mocks["removal"].call_count == 1
    assert report["children_driven"][0]["worktree_removed"] is True


# ---------------------------------------------------------------------------
# AC6 — already absent, and failing removals, never break the chain
# ---------------------------------------------------------------------------


def test_already_absent_worktree_is_a_reported_no_op(tmp_path):
    """A tree that is already gone is reported, not treated as a failure."""
    missing = str(tmp_path / "wl-child-1-gone")

    report, mocks = _run_drive(
        tmp_path,
        reports=[_child_report("SA-C1", missing), _advanced([_processed("SA-C1")])],
        children=[{"id": "SA-C1", "status": "completed"}],
        child_states={"SA-C1": TERMINAL},
    )

    assert mocks["removal"].call_count == 0
    entry = report["children_driven"][0]
    assert entry["worktree_cleanup"]["already_absent"] is True
    assert entry["worktree_removed"] is False
    assert report["success"] is True


def test_failing_removal_does_not_fail_the_drive(tmp_path):
    """A cleanup problem is recorded; a terminal child is still a success."""
    wt = _worktree(tmp_path, "wl-child-1")

    report, mocks = _run_drive(
        tmp_path,
        reports=[_child_report("SA-C1", wt), _advanced([_processed("SA-C1")])],
        children=[{"id": "SA-C1", "status": "completed"}],
        child_states={"SA-C1": TERMINAL},
        removal_side_effect=RuntimeError("REFUSE to remove the repository root"),
    )

    assert report["success"] is True
    assert report["parent_advanced"] is True
    assert "failed_child" not in report
    entry = report["children_driven"][0]
    assert entry["result"] == "finished"
    assert entry["worktree_removed"] is False
    assert entry["worktree_cleanup"]["refused"] is True
    assert report["retained_worktrees"][0]["id"] == "SA-C1"


# ---------------------------------------------------------------------------
# AC7 — start-of-run sweep of terminal children's leftover worktrees
# ---------------------------------------------------------------------------


def test_drive_sweeps_terminal_childrens_leftover_worktrees(tmp_path):
    """A stranded tree from an interrupted drive is removed at startup."""
    terminal_wt = _worktree(tmp_path, "wl-terminal")
    live_wt = _worktree(tmp_path, "wl-live")

    report, mocks = _run_drive(
        tmp_path,
        reports=[_advanced()],
        children=[
            {"id": "SA-DONE", "status": "completed"},
            {"id": "SA-LIVE", "status": "in_progress"},
        ],
        child_states={},
        discovered={"SA-DONE": terminal_wt, "SA-LIVE": live_wt},
    )

    swept = report["worktree_sweep"]
    assert [entry["id"] for entry in swept] == ["SA-DONE"]
    assert swept[0]["removed"] is True
    assert mocks["removal"].call_args.args[0] == terminal_wt
    # A live child's worktree is never swept.
    assert mocks["removal"].call_count == 1


def test_sweep_retains_a_dirty_terminal_worktree(tmp_path):
    """The sweep obeys the same dirty gate as every other removal path."""
    dirty_wt = _worktree(tmp_path, "wl-terminal")

    report, mocks = _run_drive(
        tmp_path,
        reports=[_advanced()],
        children=[{"id": "SA-DONE", "status": "completed"}],
        child_states={},
        discovered={"SA-DONE": dirty_wt},
        dirty={dirty_wt},
    )

    assert mocks["removal"].call_count == 0
    assert report["worktree_sweep"][0]["retained_dirty"] is True
    assert report["retained_worktrees"][0]["path"] == dirty_wt


def test_sweep_is_best_effort(tmp_path):
    """A discovery/sweep error never stops the drive."""
    report, _mocks = _run_drive(
        tmp_path,
        reports=[_advanced()],
        children=[{"id": "SA-DONE", "status": "completed"}],
        child_states={},
        discovered={},
    )

    assert report["success"] is True
    assert "worktree_sweep" not in report
