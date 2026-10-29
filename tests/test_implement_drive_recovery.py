
# <!-- REFACTOR-SA-0MVTPH99O008YX29
# smell: unused_import
# severity: critical
# description: Redefinition of unused `implement_mod` from line 57: `implement_mod` redefined here
# -->
"""Drive-recovery test harness + terminal-child regression
(SA-0MVLJXKB60088SND, parent SA-0MVLIZ33M009C5QY).

Scope of this module
--------------------

This is the *test harness* slice of the drive bounded-recovery work. It adds
no recovery behaviour itself; it establishes the fixtures the sibling feature
items build on, plus the regression guard that must stay green through every
one of them:

- **Harness (AC1/AC2).** :class:`DriveHarness` drives ``phase_drive`` with an
  injectable ``parent_runner`` / ``spawn_session``, patched worklog plumbing,
  and recorders for every mutation a recovery path may perform (lifecycle
  status updates, worklog comments, the worktree reset helper). The
  :func:`finishing_failed_report` helper scripts the exact
  ``phase_parent`` report shape emitted by ``_run_finish``
  (``success: False``, ``finishing_failed: True``, ``next_child``,
  ``message``) — the livelock entry point.
- **Terminal-child regression (AC3/parent AC5).** A child already terminal
  (``completed``/``done``/``in_review`` status, an ``in_review`` stage, or
  soft-deleted via ``deletedBy``) is never finished, reset or re-spawned by
  ``phase_drive``, and the ``skip-terminal`` precedence in
  :func:`_classify_child` is preserved.

The fixtures intentionally mirror — and reuse — the ``ScriptedParent`` /
``RecordingSpawner`` pattern and the ``implement_mod`` fixture from
``tests/test_implement_drive.py`` rather than inventing a second style.

Future sibling work in this file: bounded finish attempts, finish-failure
terminal reconciliation, and non-terminal reset. The harness exposes
:meth:`DriveHarness.patch` and :meth:`DriveHarness.record_reset_helper` so
those tests can inject not-yet-existing recovery helpers (``create=True``)
without touching production code from this item.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Reuse the existing drive-loop harness pattern verbatim: the scripted parent
# runner, the recording session spawner, the child-report helper and the
# module-under-test fixture all come from ``tests/test_implement_drive.py``.
from tests.test_implement_drive import (
    PARENT,
    RecordingSpawner,
    ScriptedParent,
    implement_mod,  # noqa: F401 — re-exported as a pytest fixture
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _neutralise_ambient_drive_guard(monkeypatch):
    """Make this module hermetic when pytest runs inside a driven session.

    ``implement.py finish`` runs the changed-scope suite with
    ``IMPLEMENT_DRIVE_ACTIVE=1`` exported by the driving parent. Every
    ``phase_drive`` test here would otherwise hit the recursion guard and
    fail the run (same hazard as SA-0MV8HEQ4O009Y3UB, pinned in
    ``tests/test_implement_drive.py``).
    """
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)


def _child(cid: str, **overrides) -> dict:
    """Build a child work-item dict with the fields the loop inspects."""
    child = {
        "id": cid,
        "title": f"Child {cid}",
        "status": "open",
        "stage": "intake_complete",
        "deletedBy": "",
        "assignee": "",
    }
    child.update(overrides)
    return child


def finishing_failed_report(child_id: str, message: str | None = None) -> dict:
    """Script the exact ``phase_parent`` report shape ``_run_finish`` emits.

    When ``implement.py finish <child>`` fails inside ``phase_parent``,
    ``_run_finish`` sets ``success: False``, ``finishing_failed: True``,
    ``next_child`` (the child whose finish failed) and ``message``. That is
    the livelock entry point: today ``phase_drive`` breaks the chain on any
    ``success: False`` report without accounting for or recovering from it.

    Args:
        child_id: The child whose finish supposedly failed.
        message: Optional failure message; defaults to the production text.

    Returns:
        A report dict accepted by :class:`ScriptedParent`.
    """
    return {
        "phase": "parent",
        "work_item_id": PARENT,
        "success": False,
        "message": message or f"Failed to finish child {child_id}",
        "next_child": child_id,
        "finishing_failed": True,
        "children_processed": [],
    }


class RecordingParent(ScriptedParent):
    """A :class:`ScriptedParent` that also records every report it returns.

    ``phase_drive`` observes ``phase_parent`` behaviour only through its
    reports — finish attempts inside the real parent happen in a subprocess.
    Recording the returned reports lets a test count the ``finishing_failed``
    reports drive consumed for a given child (the per-child finish-attempt
    observability the bounded-recovery features build on).
    """

    def __init__(self, reports: list[dict]):
        super().__init__(reports)
        self.returned: list[dict] = []

    def __call__(self, action, work_item_id, no_refactor=False, verbose=False):
        report = super().__call__(action, work_item_id, no_refactor, verbose)
        self.returned.append(report)
        return report


class DriveHarness:
    """Deterministic ``phase_drive`` harness with injectable collaborators.

    Records everything a recovery path may mutate so a test can assert both
    what happened (spawns, finish invocations, resets) and what must *never*
    happen to a terminal child.
    """

    def __init__(
        self,
        mod,
        *,
        children: list[dict],
        reports: list[dict] | None = None,
        runner: ScriptedParent | None = None,
        spawn_results: list[dict] | None = None,
        child_states: dict[str, dict] | None = None,
    ):
        self.mod = mod
        self.children = list(children)
        self.runner = runner or RecordingParent(list(reports or []))
        self.spawner = RecordingSpawner(spawn_results)
        self.child_states = dict(child_states or {})
        self.show_calls: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.status_updates: list[dict] = []
        self.extra_patches: list = []

    # -- injectable collaborators ------------------------------------------

    def _show(self, work_item_id: str) -> dict:
        """Serve the parent plus scripted per-child state.

        ``phase_drive`` fetches the parent, then (on the failure/recovery
        path) re-fetches individual children; an unmapped child resolves to
        ``{}`` — non-terminal — so a wrong fetch can never fake success.
        """
        self.show_calls.append(work_item_id)
        if work_item_id == PARENT:
            return {"id": PARENT}
        return self.child_states.get(work_item_id, {})

    def _update_status(self, work_item_id, status, *args, **kwargs):
        self.status_updates.append(
            {"id": work_item_id, "status": status, **kwargs}
        )
        return {"success": True}

    def _comment(self, work_item_id, text):
        self.comments.append((work_item_id, text))
        return True

    def record_reset_helper(self, name: str = "_reset_child_worktree") -> list:
        """Patch the worktree-reset helper with a recorder and return the list.

        The helper is introduced by a sibling feature, so the patch is
        created with ``create=True``: the terminal-child guard can assert
        "never reset" before the helper exists, and keeps asserting it
        afterwards.
        """
        calls: list = []

        def _record(*args, **kwargs):
            calls.append((args, kwargs))
            return {"success": True}

        self.extra_patches.append(
            mock.patch.object(self.mod, name, side_effect=_record, create=True)
        )
        return calls

    def patch(self, name: str, **kwargs):
        """Register an extra ``mock.patch.object`` on the module under test."""
        patcher = mock.patch.object(self.mod, name, create=True, **kwargs)
        self.extra_patches.append(patcher)
        return patcher

    # -- recording helpers -------------------------------------------------

    @property
    def spawn_calls(self) -> list[str]:
        return [call["id"] for call in self.spawner.calls]

    @property
    def returned_reports(self) -> list[dict]:
        """Every ``parent`` report drive consumed, in order."""
        return list(self.runner.returned)

    def finishing_failed_reports(
        self, child_id: str | None = None,
    ) -> list[dict]:
        """The ``finishing_failed`` reports drive consumed.

        Optionally filtered to one child.
        """
        return [
            report
            for report in self.runner.returned
            if report.get("finishing_failed")
            and (child_id is None or report.get("next_child") == child_id)
        ]

    def mutated_ids(self) -> set[str]:
        return {update["id"] for update in self.status_updates}

    # -- execution ---------------------------------------------------------

    def run(self, **kwargs) -> dict:
        """Patch worklog plumbing and run one ``phase_drive`` invocation."""
        patches = [
            mock.patch.object(self.mod, "wl_show", side_effect=self._show),
            mock.patch.object(
                self.mod, "wl_show_children", return_value=self.children,
            ),
            mock.patch.object(self.mod, "_discover_worktree", return_value=""),
            mock.patch.object(
                self.mod, "wl_add_comment", side_effect=self._comment,
            ),
            mock.patch.object(
                self.mod.StatusLifecycle, "update_status", self._update_status,
            ),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches + self.extra_patches:
                stack.enter_context(patcher)
            report = self.mod.phase_drive(
                PARENT,
                parent_runner=self.runner,
                spawn_session=self.spawner,
                **kwargs,
            )
        report["_show_calls"] = list(self.show_calls)
        return report


@pytest.fixture
def drive_harness(implement_mod):
    """Factory fixture building a :class:`DriveHarness` bound to the module."""
    def _make(**kwargs) -> DriveHarness:
        return DriveHarness(implement_mod, **kwargs)

    return _make


@pytest.fixture
def finishing_failed_parent_runner():
    """Factory fixture scripting a ``parent_runner`` that reports finish
    failure.

    The returned runner always reports ``finishing_failed`` for the given
    child, ``times`` times (default 10 — enough for a bounded-retry recovery
    loop to exhaust its per-child bound without exhausting the script first,
    which would raise from :class:`ScriptedParent`).
    """
    def _make(child_id: str, *, times: int = 10, message=None):
        return RecordingParent([
            finishing_failed_report(child_id, message=message)
            for _ in range(times)
        ])

    return _make


# ---------------------------------------------------------------------------
# AC2: the finishing_failed report is the livelock entry point
# ---------------------------------------------------------------------------


class TestFinishingFailedReports:
    """``phase_drive`` consumes the exact ``finishing_failed`` report shape."""

    def test_finishing_failed_stops_without_spawning_a_session(
        self, drive_harness, finishing_failed_parent_runner,
    ):
        """No session is spawned for a child whose finish failed.

        The finish failure is a *parent* failure, not a spawn failure: drive
        must never invent a session for it, must not claim success and must
        not advance the parent while the child is non-terminal.
        """
        harness = drive_harness(
            children=[_child("SA-C1")],
            runner=finishing_failed_parent_runner("SA-C1"),
        )

        report = harness.run()

        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["sessions_spawned"] == 0
        assert harness.spawner.calls == []
        # The only parent report drive consumed was the failed finish for
        # SA-C1 — the fixture scripted exactly the `_run_finish` shape.
        assert harness.finishing_failed_reports("SA-C1")

    def test_finishing_failed_is_not_confused_with_parent_completion(
        self, drive_harness,
    ):
        """A finish failure never reads as "epic complete"."""
        harness = drive_harness(
            children=[_child("SA-C1")],
            reports=[finishing_failed_report("SA-C1")],
        )

        report = harness.run()

        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert report["failed_child"] == "SA-C1"
        assert "Failed to finish child SA-C1" in report["message"]
        assert all(
            r.get("finishing_failed") and r.get("next_child") == "SA-C1"
            for r in harness.returned_reports
        )


# ---------------------------------------------------------------------------
# AC3 (parent AC5): terminal children are never touched
# ---------------------------------------------------------------------------


class TestTerminalChildRegression:
    """A terminal child is never finished, reset or re-spawned by drive."""

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param(
                {"status": "completed", "stage": "in_review"},
                id="completed-status",
            ),
            pytest.param(
                {"status": "done", "stage": "in_review"},
                id="done-status",
            ),
            pytest.param(
                {"status": "in_review", "stage": "in_review"},
                id="in-review-status",
            ),
            pytest.param(
                {"status": "open", "stage": "in_review"},
                id="in-review-stage",
            ),
            pytest.param(
                {"status": "open", "deletedBy": "operator"},
                id="soft-deleted",
            ),
        ],
    )
    def test_terminal_child_is_never_driven(self, drive_harness, state):
        """An all-terminal epic spawns, finishes and resets nothing."""
        child = _child("SA-T", **state)
        harness = drive_harness(
            children=[child],
            reports=[{
                "success": True,
                "parent_advanced": True,
                "message": "all children terminal",
            }],
            child_states={"SA-T": child},
        )
        reset_calls = harness.record_reset_helper()

        report = harness.run()

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["children_driven"] == []
        assert harness.spawner.calls == []
        assert harness.finishing_failed_reports() == []
        assert harness.status_updates == []
        assert reset_calls == []

    def test_mixed_epic_drives_only_the_non_terminal_child(
        self, drive_harness,
    ):
        """A terminal sibling is skipped while the open child is driven."""
        terminal = _child("SA-T", status="completed", stage="in_review")
        open_child = _child("SA-O")
        harness = drive_harness(
            children=[terminal, open_child],
            reports=[
                {
                    "success": True,
                    "next_child": "SA-O",
                    "worktree_path": "/wt/SA-O",
                    "children_processed": [{
                        "id": "SA-T",
                        "action": "skipped-terminal",
                        "status": "completed",
                    }],
                },
                {"success": True, "parent_advanced": True},
            ],
            child_states={"SA-T": terminal, "SA-O": open_child},
        )
        reset_calls = harness.record_reset_helper()

        report = harness.run()

        assert report["success"] is True
        assert report["parent_advanced"] is True
        # Only the open child got a session; the terminal sibling never did.
        assert harness.spawn_calls == ["SA-O"]
        assert [e["id"] for e in report["children_driven"]] == ["SA-O"]
        # The terminal child was never finished, reset or reconciled.
        assert not harness.finishing_failed_reports("SA-T")
        assert "SA-T" not in harness.mutated_ids()
        assert reset_calls == []

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"status": "done"}, id="done-status"),
            pytest.param({"status": "in_review"}, id="in-review-status"),
            pytest.param(
                {"status": "in_progress", "deletedBy": "operator"},
                id="soft-deleted-wins-over-in-progress",
            ),
        ],
    )
    def test_skip_terminal_precedence_is_preserved(
        self, implement_mod, overrides,
    ):
        """``skip-terminal`` outranks ``skip-in-progress`` in
        classification."""
        assert implement_mod._classify_child(_child("SA-T", **overrides)) == (
            "skip-terminal"
        )
