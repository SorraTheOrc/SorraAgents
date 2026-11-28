"""Tests for ``phase_drive`` — one fresh session per epic child.

Contract (SA-0MUWNHKQH0034DW9):

- AC-1/AC-8: drive walks the parent chain to completion and reports each
  child.
- AC-2: every child is implemented by its own fresh session (a separate
  spawner call with a clean environment).
- AC-4/AC-5: the chain stops on the first failure, reports the failing
  child, and never advances the parent.
- AC-6: a driven session cannot recursively re-enter drive.
- AC-7: a missing session spawner stops with actionable instructions.

Timeout reconciliation (SA-0MVLJWL60001H6QK, parent SA-0MVHXWLPG007OQVN):
a per-child session timeout is a bound, not a verdict.
``TestTimeoutReconciliation`` pins the post-timeout classification — a child
that already reached a terminal state is recorded as completed-late and the
chain continues; a non-terminal child still fails hard; the report carries the
evidence that distinguishes the two outcomes.

Stranded children (SA-0MVLJWQ820060RFF / SA-0MVLJWRVO000QQNP): a child whose
session dies after pushing to ``dev`` but before its terminal update is
reported as stranded, with its pushed-commit evidence, without mutating its
lifecycle state (finalisation is a separate, explicit opt-in).

The parent state machine and the session spawner are both injected so the
orchestration loop is exercised deterministically (no worklog, no subprocess).
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
PARENT = "SA-0MUWNHKQH0034DW9"


@pytest.fixture(scope="module")
def implement_mod():
    """Load the module-under-test (skill/implement/scripts/implement.py)."""
    sys.path.insert(0, str(_REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "implement_under_test_drive", _IMPLEMENT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["implement_under_test_drive"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _neutralise_ambient_drive_guard(monkeypatch):
    """Neutralise an ambient ``IMPLEMENT_DRIVE_ACTIVE`` for this module.

    A driven child session exports ``IMPLEMENT_DRIVE_ACTIVE=1`` (so its own
    agent cannot recursively re-enter ``drive``). When the full suite runs
    inside that driven session (as ``implement.py finish`` / the pre-push hook
    do), pytest inherits the variable and every ``phase_drive`` unit test
    here that does not itself set the variable hits the recursion guard,
    failing the run with "Refusing to drive from within a driven child
    session" (SA-0MV8HEQ4O009Y3UB). Deleting the variable for the duration
    of each test makes the module hermetic against the host environment; the
    dedicated recursion-guard test re-sets it explicitly and is unaffected.
    """
    monkeypatch.delenv(
        "IMPLEMENT_DRIVE_ACTIVE", raising=False,
    )


class ScriptedParent:
    """Return a scripted sequence of ``phase_parent`` reports, one per call."""

    def __init__(self, reports: list[dict]):
        self.reports = list(reports)
        self.calls: list[tuple] = []

    def __call__(self, action, work_item_id, no_refactor=False, verbose=False):
        self.calls.append((action, work_item_id))
        if not self.reports:
            raise AssertionError("phase_parent called more times than scripted")
        return self.reports.pop(0)


class RecordingSpawner:
    """Record spawn calls and return scripted (or success) results."""

    def __init__(self, results: list[dict] | None = None):
        self.results = list(results or [])
        self.calls: list[dict] = []

    def __call__(self, child_id, worktree_path, timeout, env, verbose=False):
        self.calls.append({
            "id": child_id,
            "worktree_path": worktree_path,
            "timeout": timeout,
            "env": env,
        })
        if self.results:
            return self.results.pop(0)
        return {"success": True, "returncode": 0, "reason": ""}


def _child_report(cid: str, wt: str | None = None) -> dict:
    return {
        "success": True,
        "next_child": cid,
        "worktree_path": wt or f"/wt/{cid}",
    }


def _patch_parent(m, children):
    return (
        mock.patch.object(m, "wl_show", return_value={"id": PARENT}),
        mock.patch.object(m, "wl_show_children", return_value=children),
        mock.patch.object(m, "_discover_worktree", return_value=""),
    )


def _patch_child_state(m, states: dict[str, dict]):
    """Route ``wl_show`` per child id while still serving the parent fetch.

    ``phase_drive`` fetches the parent once, re-validates the reported
    ``next_child`` immediately before spawning it (a second ``wl show``), and —
    after a failed spawn — re-fetches the affected child to classify it.
    ``states`` maps child id to the work-item dict ``wl_show`` should return
    for it; a **list** is consumed successively (the pre-spawn re-validation
    reads the first entry, a later re-fetch the next, and the last entry
    repeats), so a test can script a child that is startable at the spawn
    decision and terminal at reconciliation. The parent fetch (``PARENT``) and
    any unmapped id get a minimal placeholder, so a fetch for the wrong id
    shows up as a non-terminal child instead of passing silently.

    Returns the patch plus the list of ids fetched, so a test can assert that
    reconciliation only happens on the failure path.
    """
    calls: list[str] = []

    def _show(work_item_id):
        calls.append(work_item_id)
        if work_item_id == PARENT:
            return {"id": PARENT}
        state = states.get(work_item_id, {})
        if isinstance(state, list):
            return state.pop(0) if len(state) > 1 else (state[0] if state else {})
        return state

    return mock.patch.object(m, "wl_show", side_effect=_show), calls


def _run(
    m, runner, spawner, children, child_states=None, blockers=None, **kwargs,
):
    patches = list(_patch_parent(m, children))
    calls: list[str] = []
    comments: list[tuple[str, str]] = []
    if child_states is not None:
        state_patch, calls = _patch_child_state(m, child_states)
        patches.append(state_patch)
    if blockers is not None:
        # The pre-spawn re-validation re-reads a ``blocked`` child's outbound
        # edges; ``blockers`` maps child id → edge dicts.
        patches.append(mock.patch.object(
            m, "wl_dep_blockers",
            side_effect=lambda cid: blockers.get(cid, []),
        ))
    # ``phase_drive`` may comment on the parent (stranded-child diagnostic).
    # Every drive test patches it so the orchestration loop stays hermetic and
    # never touches the real worklog.
    patches.append(mock.patch.object(
        m, "wl_add_comment",
        side_effect=lambda wid, text: comments.append((wid, text)) or True,
    ))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        report = m.phase_drive(
            PARENT, parent_runner=runner, spawn_session=spawner, **kwargs
        )
    report["_wl_show_calls"] = calls
    report["_comments"] = comments
    return report


def _timeout_result(cid: str, seconds: int = 3600) -> dict:
    """Scripted spawner result for a timed-out child session.

    Mirrors what ``_default_session_spawner`` returns on
    ``subprocess.TimeoutExpired`` so the drive loop is exercised with the
    exact failure shape produced in production.
    """
    return {
        "success": False,
        "returncode": None,
        "reason": f"Child session for {cid} timed out after {seconds}s.",
    }


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-8: full epic in fresh sessions
# ---------------------------------------------------------------------------


class TestDriveCompletion:
    def test_completes_all_children_in_fresh_sessions(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            _child_report("SA-C2"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(m, runner, spawner, [{"id": "SA-C1"}, {"id": "SA-C2"}])

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["sessions_spawned"] == 2
        assert [c["id"] for c in spawner.calls] == ["SA-C1", "SA-C2"]
        assert [c["worktree_path"] for c in spawner.calls] == [
            "/wt/SA-C1", "/wt/SA-C2",
        ]
        # Parent was re-run after each child and once more to advance.
        assert len(runner.calls) == 3
        assert [e["result"] for e in report["children_driven"]] == [
            "finished", "finished",
        ]

    def test_each_spawn_carries_a_clean_guarded_environment(
        self, implement_mod,
    ):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner()

        with mock.patch.dict(
            os.environ, {"PI_SESSION_ID": "parent", "PI_SESSION_FILE": "/p"},
        ):
            _run(m, runner, spawner, [{"id": "SA-C1"}])

        env = spawner.calls[0]["env"]
        assert env[m.DRIVE_RECURSION_ENV] == "1"
        assert "PI_SESSION_ID" not in env
        assert "PI_SESSION_FILE" not in env


# ---------------------------------------------------------------------------
# AC-4 / AC-5: stop on failure, report, never advance
# ---------------------------------------------------------------------------


class TestDriveFailure:
    def test_stops_when_child_session_fails(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([
            {"success": False, "returncode": 1, "reason": "boom"},
        ])

        report = _run(m, runner, spawner, [{"id": "SA-C1"}])

        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["sessions_spawned"] == 1
        assert report["children_driven"][0]["result"] == "failed"
        assert "boom" in report["message"]
        # The chain stopped immediately after the failure.
        assert len(runner.calls) == 1

    def test_stops_when_parent_reports_failure(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([
            {"success": False, "message": "finish failed", "next_child": "SA-C1"},
        ])
        spawner = RecordingSpawner()

        report = _run(m, runner, spawner, [{"id": "SA-C1"}])

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert spawner.calls == []

    def test_stops_when_no_startable_child(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([
            {
                "success": True,
                "message": "blocked",
                "blocked_children": ["SA-C2"],
            },
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}, {"id": "SA-C2"}],
        )

        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert report["blocked_children"] == ["SA-C2"]
        assert spawner.calls == []

    def test_bounds_sessions_per_child(self, implement_mod):
        m = implement_mod
        # The parent never makes progress: the same child is returned forever.
        runner = ScriptedParent([_child_report("SA-C1") for _ in range(10)])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}], max_child_sessions=2,
        )

        assert report["success"] is False
        assert report["sessions_spawned"] == 2
        assert report["failed_child"] == "SA-C1"
        assert "after 2 session" in report["message"]


# ---------------------------------------------------------------------------
# SA-0MVLJWL60001H6QK — timeout reconciliation (parent SA-0MVHXWLPG007OQVN)
#
# A driven child's session can be killed at the per-child timeout after the
# child has already committed, pushed and reached a terminal work-item state.
# Treating every timeout as a hard child failure produced a false-negative
# drive report and stopped the epic chain. The cases below pin the required
# behaviour: reconcile the child's actual state after a failed spawn.
# ---------------------------------------------------------------------------


class TestTimeoutReconciliation:
    """A timeout is a bound, not a verdict (SA-0MVHXWLPG007OQVN)."""

    def test_terminal_child_timed_out_continues_and_advances_parent(
        self, implement_mod,
    ):
        """A child already completed/in_review is completed-late, not failed."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            _child_report("SA-C2"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 3600)])
        # Startable at the spawn decision, terminal when the killed session is
        # reconciled (the exact production shape).
        states = {
            "SA-C1": [
                {"id": "SA-C1", "status": "in_progress",
                 "stage": "in_progress"},
                {"id": "SA-C1", "status": "completed",
                 "stage": "in_review"},
            ],
        }

        report = _run(
            m, runner, spawner,
            [{"id": "SA-C1"}, {"id": "SA-C2"}],
            child_states=states, child_timeout=3600,
        )

        # The chain did not stop and the parent was advanced.
        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report.get("failed_child") is None
        # The terminal child was not re-spawned; the loop moved on to C2.
        assert [c["id"] for c in spawner.calls] == ["SA-C1", "SA-C2"]
        assert report["sessions_spawned"] == 2
        first, second = report["children_driven"]
        assert first["result"] != "failed"
        assert first["result"] in {"finished", "completed-late"}
        assert second["result"] == "finished"
        assert len(runner.calls) == 3

    def test_non_terminal_child_timed_out_still_fails_hard(
        self, implement_mod,
    ):
        """Regression guard: a non-terminal child keeps the hard failure."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            _child_report("SA-C2"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 3600)])
        states = {
            "SA-C1": {
                "id": "SA-C1", "status": "in_progress",
                "stage": "in_progress",
            },
        }

        report = _run(
            m, runner, spawner,
            [{"id": "SA-C1"}, {"id": "SA-C2"}],
            child_states=states, child_timeout=3600,
        )

        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["children_driven"][0]["result"] == "failed"
        assert "timed out" in report["children_driven"][0]["reason"]
        # The chain stopped: C2 was never spawned and the parent never re-ran.
        assert [c["id"] for c in spawner.calls] == ["SA-C1"]
        assert len(runner.calls) == 1

    def test_report_evidence_distinguishes_terminal_from_incomplete(
        self, implement_mod,
    ):
        """Each timeout outcome is truthfully distinguishable in the report."""
        m = implement_mod
        children = [{"id": "SA-C1"}, {"id": "SA-C2"}]

        terminal_runner = ScriptedParent([
            _child_report("SA-C1"),
            _child_report("SA-C2"),
            {"success": True, "parent_advanced": True},
        ])
        completed_late = _run(
            m, terminal_runner,
            RecordingSpawner([_timeout_result("SA-C1", 3600)]),
            children,
            child_states={"SA-C1": [
                {"id": "SA-C1", "status": "in_progress",
                 "stage": "in_progress"},
                {"id": "SA-C1", "status": "completed",
                 "stage": "in_review"},
            ]},
            child_timeout=3600,
        )["children_driven"][0]

        assert completed_late["result"] == "completed-late"
        assert completed_late["timed_out"] is True
        assert completed_late["timeout_seconds"] == 3600
        assert completed_late["terminal"] is True
        assert completed_late["terminal_status"] == "completed"
        assert completed_late["terminal_stage"] == "in_review"
        assert "timed out" in completed_late["reason"]
        assert "completed" in completed_late["reason"]

        incomplete_runner = ScriptedParent([_child_report("SA-C1")])
        incomplete = _run(
            m, incomplete_runner,
            RecordingSpawner([_timeout_result("SA-C1", 3600)]),
            children,
            child_states={"SA-C1": {
                "id": "SA-C1", "status": "in_progress",
                "stage": "in_progress",
            }},
            child_timeout=3600,
        )["children_driven"][0]

        assert incomplete["result"] == "failed"
        assert incomplete["timed_out"] is True
        assert incomplete["timeout_seconds"] == 3600
        assert incomplete["terminal"] is False
        assert "timed out" in incomplete["reason"]


class TestSpawnFailureReconciliation:
    """Reconciliation is read-only, failure-scoped and fail-closed.

    SA-0MVLJWMQ8007EDW0: after a failed spawn ``phase_drive`` re-fetches the
    child and classifies it with the existing terminal-state helper. A
    successful spawn must not pay that extra fetch (risk R1), and an
    unreadable fetch must keep the pre-existing hard failure (no crash, no
    silent success).
    """

    def test_reconciles_terminal_child_after_failed_spawn(self, implement_mod):
        """The child is re-fetched and its terminal state is recorded."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 1800)])

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states={"SA-C1": [
                {"id": "SA-C1", "status": "open", "stage": "intake_complete"},
                {"id": "SA-C1", "status": "done", "stage": "in_review"},
            ]},
        )

        assert "SA-C1" in report["_wl_show_calls"]
        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["children_driven"][0]["result"] == "completed-late"

    def test_successful_spawn_fetches_the_child_once(self, implement_mod):
        """Reconciliation is failure-scoped (risk R1).

        Since SA-0MWFJSCPT005BO2R a successful spawn pays exactly **one**
        child fetch: the pre-spawn re-validation. The failure-only
        reconciliation fetch is still never taken on the success path — the
        child appears once, not twice.
        """
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner()  # succeeds

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states={"SA-C1": {
                "id": "SA-C1", "status": "in_progress", "stage": "",
            }},
        )

        assert report["success"] is True
        child_fetches = [
            cid for cid in report["_wl_show_calls"] if cid != PARENT
        ]
        assert child_fetches == ["SA-C1"]

    def test_unreadable_child_state_keeps_hard_failure(self, implement_mod):
        """A failed fetch must fail closed — no crash, no silent success."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 600)])

        # child_states maps nothing for SA-C1 → wl_show returns {}.
        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}], child_states={},
        )

        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert report["failed_child"] == "SA-C1"
        assert report["children_driven"][0]["result"] == "failed"
        assert "timed out" in report["children_driven"][0]["reason"]

    def test_terminal_child_is_not_re_spawned(self, implement_mod):
        """A reconciled terminal child never gets another session."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            _child_report("SA-C2"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 60)])

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}, {"id": "SA-C2"}],
            child_states={"SA-C1": [
                {"id": "SA-C1", "status": "in_progress",
                 "stage": "in_progress"},
                {"id": "SA-C1", "status": "completed", "stage": "in_review"},
            ]},
        )

        assert [c["id"] for c in spawner.calls] == ["SA-C1", "SA-C2"]
        assert report["children_driven"][0]["result"] == "completed-late"

    def test_nonzero_exit_with_terminal_child_is_reconciled(
        self, implement_mod,
    ):
        """Non-zero exits reconcile too — the timeout is not special-cased."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([{
            "success": False, "returncode": 3,
            "reason": "Child session for SA-C1 exited with code 3: boom",
        }])

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states={"SA-C1": [
                {"id": "SA-C1", "status": "in_progress",
                 "stage": "in_progress"},
                {"id": "SA-C1", "status": "completed", "stage": "in_review"},
            ]},
        )

        assert report["success"] is True
        assert report["children_driven"][0]["result"] == "completed-late"
        assert report["children_driven"][0]["timed_out"] is False


# ---------------------------------------------------------------------------
# SA-0MVLJWOJ8004ABBA — truthful timeout outcome evidence in the drive report
#
# A producer or auditor reading `drive --json` (or the human log) must be able
# to tell "timed out but the child completed" apart from "timed out, child
# incomplete", including the terminal evidence, so a false-negative failure
# can never be mistaken for real work remaining.
# ---------------------------------------------------------------------------


class TestTimeoutOutcomeEvidence:
    """Distinct verdicts, truthful reasons, human rendering, summary."""

    def test_report_counts_completed_late_children(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            _child_report("SA-C2"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([
            _timeout_result("SA-C1", 3600),
            _timeout_result("SA-C2", 1800),
        ])
        states = {
            "SA-C1": [
                {"id": "SA-C1", "status": "in_progress",
                 "stage": "in_progress"},
                {"id": "SA-C1", "status": "completed",
                 "stage": "in_review"},
            ],
            "SA-C2": [
                {"id": "SA-C2", "status": "in_progress",
                 "stage": "in_progress"},
                {"id": "SA-C2", "status": "in_review",
                 "stage": "in_review"},
            ],
        }

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}, {"id": "SA-C2"}],
            child_states=states,
        )

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["completed_late"] == 2
        assert "2" in report["message"]
        assert "completed late" in report["message"].lower()

    def test_successful_drive_reports_zero_completed_late(
        self, implement_mod,
    ):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])

        report = _run(
            m, runner, RecordingSpawner(), [{"id": "SA-C1"}],
        )

        assert report["completed_late"] == 0
        assert "completed late" not in report["message"].lower()

    def test_backward_compatible_report_keys(self, implement_mod):
        """Existing keys keep their meaning and types (additive only)."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])

        report = _run(
            m, runner, RecordingSpawner([_timeout_result("SA-C1", 60)]),
            [{"id": "SA-C1"}],
            # Startable at the spawn decision (the pre-spawn re-validation
            # must allow the session), terminal by reconciliation.
            child_states={"SA-C1": [
                {"id": "SA-C1", "status": "open", "stage": "intake_complete"},
                {"id": "SA-C1", "status": "completed", "stage": "in_review"},
            ]},
        )

        assert isinstance(report["success"], bool)
        assert isinstance(report["parent_advanced"], bool)
        assert isinstance(report["sessions_spawned"], int)
        assert isinstance(report["children_driven"], list)
        assert isinstance(report["message"], str)
        assert set(report) >= {
            "success", "parent_advanced", "sessions_spawned",
            "children_driven", "message",
        }

    def test_human_report_renders_completed_late_distinctly(
        self, implement_mod, capsys,
    ):
        """The log must not show a completed-late child as a plain success."""
        m = implement_mod
        report = {
            "phase": "drive",
            "work_item_id": PARENT,
            "success": True,
            "parent_advanced": True,
            "sessions_spawned": 1,
            "completed_late": 1,
            "children_driven": [{
                "id": "SA-C1",
                "attempt": 1,
                "result": "completed-late",
                "timed_out": True,
                "timeout_seconds": 3600,
                "terminal": True,
                "terminal_status": "completed",
                "terminal_stage": "in_review",
                "reason": (
                    "Child session for SA-C1 timed out after 3600s. Child "
                    "reached a terminal state anyway (status=completed, "
                    "stage=in_review)."
                ),
            }],
            "message": "All children terminal; 1 completed late.",
        }

        m._emit_phase_report(report, json_output=False)
        out = capsys.readouterr().out

        assert "completed-late" in out
        assert "status=completed" in out
        assert "stage=in_review" in out
        # A completed-late child gets its own icon, never the plain success one.
        assert "🕓 SA-C1" in out
        assert "✅ SA-C1" not in out


# ---------------------------------------------------------------------------
# SA-0MVLJWQ820060RFF — stranded in-progress child detection (tests)
#
# The production case (WL-0MVGUA9EY003XFAD): a child session is killed between
# pushing its work to `dev` and running the final StatusLifecycle terminal
# update, leaving the item stranded `in-progress` and blocking `wl next`.
# Detection must name the stranded child together with its pushed-commit
# evidence, without mutating anything (finalisation is a separate, explicit
# opt-in — SA-0MVLJWTGP005XJPP).
# ---------------------------------------------------------------------------


class _RecordingMutations:
    """Record lifecycle mutations so a test can assert read-only behaviour."""

    def __init__(self):
        self.status_updates: list[tuple] = []

    def update_status(self, *args, **kwargs):
        self.status_updates.append((args, kwargs))
        return {"success": True}


def _patch_mutations(m):
    """Patch ``StatusLifecycle.update_status`` with a recorder."""
    recorder = _RecordingMutations()
    return (
        mock.patch.object(m.StatusLifecycle, "update_status", recorder.update_status),
        recorder,
    )


class TestStrandedChildren:
    """Detect a child whose work is on dev but whose item is still in-progress."""

    def test_stranded_child_is_reported_with_commit_evidence(
        self, implement_mod,
    ):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 3600)])
        states = {"SA-C1": {
            "id": "SA-C1", "status": "in-progress", "stage": "in_progress",
        }}

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states=states,
            commit_evidence=lambda child_id: "abc1234",
        )

        # The chain still stops — the child is not terminal.
        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        # …but the report names the stranded child and its pushed commit.
        stranded = report["stranded_children"]
        assert len(stranded) == 1
        assert stranded[0]["id"] == "SA-C1"
        assert stranded[0]["status"] == "in-progress"
        assert stranded[0]["commit"] == "abc1234"
        assert "SA-C1" in report["message"]
        assert "stranded" in report["message"].lower()

    def test_in_progress_child_without_pushed_commit_is_not_stranded(
        self, implement_mod,
    ):
        """No false positives: no commit on dev → plain hard failure."""
        m = implement_mod
        runner = ScriptedParent([_child_report("SA-C1")])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 3600)])
        states = {"SA-C1": {
            "id": "SA-C1", "status": "in-progress", "stage": "in_progress",
        }}

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states=states,
            commit_evidence=lambda child_id: None,
        )

        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert report.get("stranded_children") in (None, [])
        assert "stranded" not in report["message"].lower()

    def test_terminal_child_is_never_reported_stranded(self, implement_mod):
        """A completed-late child is terminal, so never "stranded"."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 3600)])
        states = {"SA-C1": {
            "id": "SA-C1", "status": "completed", "stage": "in_review",
        }}

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states=states,
            commit_evidence=lambda child_id: "abc1234",
        )

        assert report["success"] is True
        assert report.get("stranded_children") in (None, [])

    def test_detection_does_not_mutate_child_state(self, implement_mod):
        """Read-only by default: no finalisation unless explicitly requested."""
        m = implement_mod
        runner = ScriptedParent([_child_report("SA-C1")])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 3600)])
        states = {"SA-C1": {
            "id": "SA-C1", "status": "in-progress", "stage": "in_progress",
        }}
        patch, recorder = _patch_mutations(m)

        with patch:
            report = _run(
                m, runner, spawner, [{"id": "SA-C1"}],
                child_states=states,
                commit_evidence=lambda child_id: "abc1234",
            )

        assert report["success"] is False
        assert recorder.status_updates == []


# ---------------------------------------------------------------------------
# SA-0MVLJWTGP005XJPP — safe, evidence-gated finalisation of stranded children
#
# Finalisation is opt-in (``--finalise-stranded``) and only runs when the
# pushed commit is verified, the item is still in-progress, no other agent
# holds it, and its worktree holds no uncommitted work. Otherwise the
# read-only diagnostic stands.
# ---------------------------------------------------------------------------


class TestFinaliseStrandedChildren:
    """Finalise only when safe; fall back to the diagnostic otherwise."""

    def _stranded_setup(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner([_timeout_result("SA-C1", 3600)])
        states = {"SA-C1": {
            "id": "SA-C1", "status": "in-progress", "stage": "in_progress",
            "assignee": "",
        }}
        return m, runner, spawner, states

    def test_finalises_stranded_child_when_safe(self, implement_mod):
        m, runner, spawner, states = self._stranded_setup(implement_mod)
        finalised_calls: list = []

        def fake_finalise(child_id, evidence):
            finalised_calls.append((child_id, evidence["commit"]))
            return {
                "finalised": True,
                "commit": evidence["commit"],
                "reason": "finalised for test",
            }

        with mock.patch.object(
            m, "_worktree_has_uncommitted_changes", return_value=False,
        ):
            report = _run(
                m, runner, spawner, [{"id": "SA-C1"}],
                child_states=states,
                commit_evidence=lambda child_id: "abc1234",
                finalise_stranded=True,
                finalise_child=fake_finalise,
            )

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert finalised_calls == [("SA-C1", "abc1234")]
        assert report["finalised_children"][0]["id"] == "SA-C1"
        assert report["finalised_children"][0]["commit"] == "abc1234"
        assert report["children_driven"][0]["result"] == "finalised"
        assert report.get("failed_child") is None
        # The parent worklog comment records the automatic finalisation.
        assert any(
            "finalised" in text.lower() for _wid, text in report["_comments"]
        )

    def test_default_is_read_only(self, implement_mod):
        """Without --finalise-stranded nothing is mutated."""
        m, runner, spawner, states = self._stranded_setup(implement_mod)
        patch, recorder = _patch_mutations(m)

        with patch, mock.patch.object(
            m, "_worktree_has_uncommitted_changes",
        ) as dirty_check:
            report = _run(
                m, runner, spawner, [{"id": "SA-C1"}],
                child_states=states,
                commit_evidence=lambda child_id: "abc1234",
            )

        assert report["success"] is False
        assert recorder.status_updates == []
        # The safety check is part of the finalisation path only.
        assert dirty_check.called is False
        assert report["stranded_children"][0]["id"] == "SA-C1"

    def test_dirty_worktree_blocks_finalisation(self, implement_mod):
        m, runner, spawner, states = self._stranded_setup(implement_mod)
        patch, recorder = _patch_mutations(m)

        with patch, mock.patch.object(
            m, "_worktree_has_uncommitted_changes", return_value=True,
        ):
            report = _run(
                m, runner, spawner, [{"id": "SA-C1"}],
                child_states=states,
                commit_evidence=lambda child_id: "abc1234",
                finalise_stranded=True,
            )

        assert report["success"] is False
        assert recorder.status_updates == []
        assert report["stranded_children"][0]["id"] == "SA-C1"
        assert "stranded" in report["message"].lower()

    def test_claimed_by_another_agent_blocks_finalisation(
        self, implement_mod,
    ):
        """A child held by another live agent is never finalised."""
        m, runner, spawner, states = self._stranded_setup(implement_mod)
        states["SA-C1"]["assignee"] = "another-agent"

        with mock.patch.object(
            m, "_worktree_has_uncommitted_changes", return_value=False,
        ):
            report = _run(
                m, runner, spawner, [{"id": "SA-C1"}],
                child_states=states,
                commit_evidence=lambda child_id: "abc1234",
                finalise_stranded=True,
            )

        assert report["success"] is False
        assert report.get("finalised_children") in (None, [])
        assert "stranded" in report["message"].lower()

    def test_missing_evidence_never_finalises(self, implement_mod):
        m, runner, spawner, states = self._stranded_setup(implement_mod)

        with mock.patch.object(
            m, "_worktree_has_uncommitted_changes", return_value=False,
        ):
            report = _run(
                m, runner, spawner, [{"id": "SA-C1"}],
                child_states=states,
                commit_evidence=lambda child_id: None,
                finalise_stranded=True,
            )

        assert report["success"] is False
        assert report.get("finalised_children") in (None, [])
        assert report.get("stranded_children") in (None, [])

    def test_finalise_helper_refuses_non_in_progress_child(
        self, implement_mod,
    ):
        """The helper itself never touches a terminal or claimed child."""
        m = implement_mod
        evidence = {
            "stranded": True, "commit": "abc1234",
            "evidence": "commit abc1234 on dev",
        }
        with mock.patch.object(
            m, "wl_show", return_value={
                "id": "SA-C1", "status": "open", "assignee": "",
            },
        ), mock.patch.object(m.StatusLifecycle, "update_status") as upd:
            out = m._finalise_stranded_child("SA-C1", evidence)

        assert out["finalised"] is False
        assert "not in-progress" in out["reason"]
        assert upd.called is False


# ---------------------------------------------------------------------------
# AC-6 / AC-7: recursion guard + missing spawner
# ---------------------------------------------------------------------------


class TestDriveGuards:
    def test_recursion_guard_refuses_from_driven_session(self, implement_mod):
        m = implement_mod
        spawner = RecordingSpawner()
        runner = ScriptedParent([])

        with mock.patch.dict(os.environ, {m.DRIVE_RECURSION_ENV: "1"}):
            report = _run(m, runner, spawner, [{"id": "SA-C1"}])

        assert report["success"] is False
        assert report["recursion_blocked"] is True
        assert spawner.calls == []
        assert runner.calls == []

    def test_leaf_item_is_not_driven(self, implement_mod):
        m = implement_mod
        spawner = RecordingSpawner()
        runner = ScriptedParent([])

        report = _run(m, runner, spawner, [])

        assert report.get("leaf") is True
        assert report["success"] is False
        assert report["sessions_spawned"] == 0
        assert spawner.calls == []

    def test_missing_spawner_is_actionable(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([_child_report("SA-C1")])

        p1, p2, p3 = _patch_parent(m, [{"id": "SA-C1"}])
        with p1, p2, p3, mock.patch.object(
            m, "_resolve_pi_bin", return_value=None,
        ):
            report = m.phase_drive(PARENT, parent_runner=runner)

        assert report["success"] is False
        assert "No Pi executable" in report["message"]
        assert report["sessions_spawned"] == 1


# ---------------------------------------------------------------------------
# Env helper + default spawner
# ---------------------------------------------------------------------------


class TestChildSessionEnv:
    def test_sets_guard_and_scrubs_parent_identity(self, implement_mod):
        m = implement_mod
        base = {
            "PATH": "/bin",
            "PI_SESSION_ID": "parent-id",
            "PI_SESSION_FILE": "/tmp/parent.jsonl",
        }
        env = m._child_session_env(base)

        assert env[m.DRIVE_RECURSION_ENV] == "1"
        assert "PI_SESSION_ID" not in env
        assert "PI_SESSION_FILE" not in env
        assert env["PATH"] == "/bin"
        # Base mapping is not mutated.
        assert base["PI_SESSION_ID"] == "parent-id"


class TestDefaultSessionSpawner:
    def test_invokes_pi_print_with_skill_command(
        self, implement_mod, tmp_path,
    ):
        m = implement_mod
        captured: dict = {}

        def fake_run(cmd, cwd=None, env=None, capture_output=None, text=None,
                     timeout=None, check=None):
            captured.update(
                cmd=cmd, cwd=cwd, env=env, timeout=timeout,
            )

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        env = {"PATH": "/bin", m.DRIVE_RECURSION_ENV: "1"}
        with mock.patch.object(m, "_resolve_pi_bin", return_value="/usr/bin/pi"), \
                mock.patch.object(m.subprocess, "run", side_effect=fake_run):
            result = m._default_session_spawner("SA-C1", str(tmp_path), 120, env)

        assert result["success"] is True
        assert captured["cmd"][:3] == [
            "/usr/bin/pi", "-p", "/skill:implement SA-C1",
        ]
        assert captured["cmd"][3] == "--approve"
        assert captured["cwd"] == str(tmp_path)
        assert captured["env"][m.DRIVE_RECURSION_ENV] == "1"
        assert captured["timeout"] == 120

    def test_reports_timeout(self, implement_mod, tmp_path):
        m = implement_mod

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="pi", timeout=5)

        with mock.patch.object(m, "_resolve_pi_bin", return_value="/usr/bin/pi"), \
                mock.patch.object(m.subprocess, "run", side_effect=boom):
            result = m._default_session_spawner("SA-C1", str(tmp_path), 5, {})

        assert result["success"] is False
        assert "timed out" in result["reason"]

    def test_reports_nonzero_exit(self, implement_mod, tmp_path):
        m = implement_mod

        def fake_run(*a, **k):
            class _R:
                returncode = 3
                stdout = ""
                stderr = "fatal: something failed"

            return _R()

        with mock.patch.object(m, "_resolve_pi_bin", return_value="/usr/bin/pi"), \
                mock.patch.object(m.subprocess, "run", side_effect=fake_run):
            result = m._default_session_spawner("SA-C1", str(tmp_path), 60, {})

        assert result["success"] is False
        assert result["returncode"] == 3
        assert "exited with code 3" in result["reason"]


# ---------------------------------------------------------------------------
# SA-0MV8HEQ4O009Y3UB — hermetic against the driven-session ambient guard
# ---------------------------------------------------------------------------


class TestHermeticAgainstDrivenAmbientGuard:
    """The full suite must pass when run *inside* a driven child session.

    ``implement.py finish`` in a driven child runs pytest with
    ``IMPLEMENT_DRIVE_ACTIVE=1`` exported (the recursion guard that stops the
    child's own agent re-entering ``drive``). Every ``phase_drive`` unit test
    in this module inherited that ambient variable and hit the recursion
    guard, failing the whole suite with "Refusing to drive from within a
    driven child session" (SA-0MV8HEQ4O009Y3UB). The autouse fixture
    neutralises the ambient variable; this proof runs the module under a
    nested pytest with the variable exported to confirm the exact
    driven-session scenario passes.
    """

    def test_module_passes_when_ambient_guard_var_is_exported(self, tmp_path):
        """Run this module in a subprocess with ``IMPLEMENT_DRIVE_ACTIVE=1``
        set in the environment, as ``implement.py finish`` does inside a
        driven child, and assert the whole module exits green."""
        env = dict(os.environ)
        env["IMPLEMENT_DRIVE_ACTIVE"] = "1"
        env.pop("PI_SESSION_ID", None)
        env.pop("PI_SESSION_FILE", None)
        proc = subprocess.run(
            [
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "-p", "no:randomly",
                "-k", "not module_passes_when_ambient_guard_var_is_exported",
                str(_REPO_ROOT / "tests" / "test_implement_drive.py"),
            ],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 0, (
            f"module must pass under IMPLEMENT_DRIVE_ACTIVE=1\n{combined}"
        )
        assert "Refusing to drive from within a driven child" not in combined


# ---------------------------------------------------------------------------
# LP-0MVWEP292000ODBS: --infra-retry-limit CLI flag and loop bound
# ---------------------------------------------------------------------------


def _infra_refusal_report(child_id: str) -> dict:
    """A ``finishing_failed`` parent report classified as infrastructure.

    The canonical, classifier-checked scripted helper lives in
    ``tests/test_implement_drive_recovery.py`` (:func:`infra_refusal_report`);
    this module only needs the drive-visible shape (``kind``/``step``/
    ``reason``) for loop-bound and CLI accounting.
    """
    return {
        "phase": "parent",
        "work_item_id": PARENT,
        "success": False,
        "message": f"Failed to finish child {child_id}",
        "next_child": child_id,
        "finishing_failed": True,
        "children_processed": [],
        "finish_failure": {
            "kind": "infrastructure",
            "step": "local_dev_fast_forward",
            "reason": "local_dev_diverged",
        },
    }


class TestInfraRetryLimitFlag:
    """AC1: ``--infra-retry-limit`` exists, defaults to 5 and is clamped."""

    def test_usage_block_lists_the_flag(self, implement_mod):
        assert "--infra-retry-limit" in implement_mod.__doc__

    def test_default_is_five(self, implement_mod):
        args = implement_mod.parse_args(["drive", "SA-1"])
        assert args.infra_retry_limit == 5

    def test_flag_parses(self, implement_mod):
        args = implement_mod.parse_args(
            ["drive", "SA-1", "--infra-retry-limit", "3"],
        )
        assert args.infra_retry_limit == 3

    def test_help_lists_the_flag(self, implement_mod, capsys):
        with pytest.raises(SystemExit) as excinfo:
            implement_mod.parse_args(["drive", "SA-1", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--infra-retry-limit" in out
        assert "default: 5" in out

    def test_non_positive_limit_is_clamped_to_one(self, implement_mod):
        """A non-positive ``--infra-retry-limit`` is clamped to 1 so the
        loop still terminates (never an unbounded retry loop)."""
        for bad in (0, -3):
            report = _run(
                implement_mod,
                ScriptedParent([
                    _infra_refusal_report("SA-C1"),
                    _infra_refusal_report("SA-C1"),
                ]),
                RecordingSpawner(),
                [{"id": "SA-C1"}],
                infra_retry_limit=bad,
            )
            assert report["infra_retry_limit"] == 1
            assert report["infra_retries"] == {"SA-C1": 1}


class TestInfraRetryLoopBound:
    """AC7: ``max_iterations`` accounts for the infrastructure budget, so a
    chain that needs several infra retries then its session then the parent
    advance completes instead of tripping the iteration bound."""

    def test_loop_bound_includes_the_infra_budget(self, implement_mod):
        """Five infra retries + one session + the parent advance consume 7
        parent invocations — beyond the bound that would hold without the
        infra budget in the formula — and must still complete."""
        m = implement_mod
        runner = ScriptedParent([
            *(_infra_refusal_report("SA-C1") for _ in range(5)),
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])

        report = _run(
            m, runner, RecordingSpawner(), [{"id": "SA-C1"}],
            infra_retry_limit=5, delay=lambda _s: None,
        )

        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert len(runner.calls) == 7
        assert report["infra_retries"] == {"SA-C1": 5}
        assert "exceeded its iteration bound" not in report["message"]

    def test_loop_bound_never_masks_a_never_finishing_harness(
        self, implement_mod,
    ):
        """The loop bound is a termination guarantee for the *stopping*
        path too: an always-infrastructure-refusing harness with the default
        flags terminates after exactly infra + finish attempts — it is never
        reported as "exceeded its iteration bound" (parent AC6,
        LP-0MVWEP3J3005Q4X6 AC4)."""
        m = implement_mod
        runner = ScriptedParent([
            *(_infra_refusal_report("SA-C1") for _ in range(10)),
        ])

        report = _run(
            m, runner, RecordingSpawner(), [{"id": "SA-C1"}],
            child_states={"SA-C1": {"id": "SA-C1", "status": "open"}},
            infra_retry_limit=5, finish_attempt_limit=1,
            delay=lambda _s: None,
        )

        # The refusal never self-clears: 5 infra retries + 1 finish attempt
        # were observed, then the chain stopped with the child named — the
        # iteration formula counted the budgets without tripping.
        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert len(runner.calls) == 6
        assert report["infra_retries"] == {"SA-C1": 5}
        assert report["finish_attempts"] == {"SA-C1": 1}
        assert report["sessions_spawned"] == 0
        assert "exceeded its iteration bound" not in report["message"]


# ---------------------------------------------------------------------------
# SA-0MVPHZ1CI0045QR0 — drive pane lifecycle and report parity
#
# The pane-driven transport (SA-0MVPHYIIE002OBC8) creates a Herdr pane per
# child. This slice owns that pane's lifecycle: completion is the agent's
# settled session state (`done`/`blocked`) reconciled against the child's real
# `phase_parent` stage, and every pane is either CLOSED (child terminal) or
# RETAINED and reported (blocked/failed) — never silently leaked. Report
# parity: every driven child carries `transport` + `pane_id`, and the human
# summary lists retained panes with an inspection remedy.
# ---------------------------------------------------------------------------


class RecordingPaneClient:
    """Record `close_pane` calls; scripted close outcome (fail-open tests)."""

    def __init__(self, close_success: bool = True):
        self.closed: list[str] = []
        self.close_success = close_success

    def close_pane(self, pane_id):
        self.closed.append(pane_id)
        return {
            "success": self.close_success,
            "pane_id": pane_id,
            "reason": "" if self.close_success else "pane_not_found",
        }


def _pane_result(
    *,
    success: bool = True,
    status: str = "done",
    pane: str = "w1:p1",
    reason: str = "",
) -> dict:
    """A spawner result in the pane transport's shape."""
    return {
        "success": success,
        "returncode": 0 if success else None,
        "reason": reason,
        "pane_id": pane,
        "transport": "pane",
        "agent_status": status,
    }


def _terminal_report(
    cid: str, status: str = "completed", action: str = "finished",
) -> dict:
    """A parent report that names *cid*'s terminal state and advances."""
    return {
        "success": True,
        "parent_advanced": True,
        "message": "advanced",
        "children_processed": [
            {"id": cid, "status": status, "action": action},
        ],
    }


class TestDrivePaneLifecycle:
    """Close-or-retain every drive-created pane; report transport parity."""

    def test_done_child_closes_its_pane_once_terminal(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            _terminal_report("SA-C1"),
        ])
        spawner = RecordingSpawner([_pane_result()])
        client = RecordingPaneClient()

        report = _run(m, runner, spawner, [{"id": "SA-C1"}],
                      pane_client=client)

        assert report["success"] is True
        assert report["parent_advanced"] is True
        # The pane is closed only after the parent confirmed the terminal
        # stage — the wait itself never decides terminality.
        assert client.closed == ["w1:p1"]
        entry = report["children_driven"][0]
        assert entry["result"] == "finished"
        assert entry["transport"] == "pane"
        assert entry["pane_id"] == "w1:p1"
        assert entry["pane_closed"] is True
        assert "retained_panes" not in report

    def test_blocked_child_is_retained_reported_and_never_closed(
        self, implement_mod, monkeypatch,
    ):
        m = implement_mod
        titles: list[tuple] = []
        monkeypatch.setattr(
            m, "_update_pane_title",
            lambda status, **kw: (
                titles.append((status, kw.get("pane_id")))
                or {"updated": True}
            ),
        )
        runner = ScriptedParent([_child_report("SA-C1")])
        spawner = RecordingSpawner([
            _pane_result(status="blocked", pane="w1:p2"),
        ])
        client = RecordingPaneClient()

        report = _run(m, runner, spawner, [{"id": "SA-C1"}],
                      pane_client=client)

        # A blocked agent stops the chain instead of being re-spawned.
        assert report["success"] is False
        assert report["parent_advanced"] is False
        assert len(runner.calls) == 1
        assert report["blocked_child"] == "SA-C1"
        assert report["blocked_pane"] == "w1:p2"
        # The pane is left open and named, with the 🚫 verdict title.
        assert client.closed == []
        assert titles == [("attention", "w1:p2")]
        entry = report["children_driven"][0]
        assert entry["result"] == "blocked"
        assert entry["pane_retained"] == "w1:p2"
        retained = report["retained_panes"]
        assert [p["pane_id"] for p in retained] == ["w1:p2"]
        assert retained[0]["remedy"] == "herdr pane read w1:p2"
        assert "herdr pane read w1:p2" in report["message"]

    def test_timeout_closes_the_pane_and_stops_the_chain(
        self, implement_mod,
    ):
        m = implement_mod
        runner = ScriptedParent([_child_report("SA-C1")])
        spawner = RecordingSpawner([_pane_result(
            success=False,
            pane="w1:p3",
            reason=(
                "Pane agent for SA-C1 timed out after 60s: deadline exceeded"
            ),
        )])
        client = RecordingPaneClient()

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states={"SA-C1": {
                "id": "SA-C1", "status": "in-progress",
                "stage": "intake_complete", "assignee": "",
            }},
            pane_client=client,
            child_timeout=60,
        )

        # IMPLEMENT_DRIVE_CHILD_TIMEOUT semantics: the resolved timeout is the
        # session budget, and an expired wait closes the pane.
        assert spawner.calls[0]["timeout"] == 60
        assert report["success"] is False
        assert report["failed_child"] == "SA-C1"
        assert client.closed == ["w1:p3"]
        entry = report["children_driven"][0]
        assert entry["timed_out"] is True
        assert entry["result"] == "failed"
        assert entry["pane_closed"] is True
        assert "retained_panes" not in report
        # The chain never re-spawned the child after the timeout.
        assert len(runner.calls) == 1
        assert report["sessions_spawned"] == 1

    def test_completed_late_child_closes_its_pane(self, implement_mod):
        """A timed-out session whose child is terminal is not a failure."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner([_pane_result(
            success=False,
            pane="w1:p4",
            reason=(
                "Pane agent for SA-C1 timed out after 60s: deadline exceeded"
            ),
        )])
        client = RecordingPaneClient()

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states={"SA-C1": [
                {"id": "SA-C1", "status": "in_progress",
                 "stage": "in_progress"},
                {"id": "SA-C1", "status": "completed",
                 "stage": "in_review"},
            ]},
            pane_client=client,
            child_timeout=60,
        )

        assert report["completed_late"] == 1
        assert report["success"] is True
        assert client.closed == ["w1:p4"]
        entry = report["children_driven"][0]
        assert entry["result"] == "completed-late"
        assert entry["pane_closed"] is True

    def test_failed_close_is_reported_as_a_retained_pane(
        self, implement_mod, monkeypatch,
    ):
        """A refused close never leaks the pane silently (fail-open)."""
        m = implement_mod
        titles: list[tuple] = []
        monkeypatch.setattr(
            m, "_update_pane_title",
            lambda status, **kw: (
                titles.append((status, kw.get("pane_id")))
                or {"updated": True}
            ),
        )
        runner = ScriptedParent([
            _child_report("SA-C1"),
            _terminal_report("SA-C1"),
        ])
        spawner = RecordingSpawner([_pane_result(pane="w1:p5")])
        client = RecordingPaneClient(close_success=False)

        report = _run(m, runner, spawner, [{"id": "SA-C1"}],
                      pane_client=client)

        assert client.closed == ["w1:p5"]
        entry = report["children_driven"][0]
        assert entry["pane_closed"] is False
        assert "pane_not_found" in entry["pane_cleanup"]["reason"]
        retained = report["retained_panes"]
        assert [p["pane_id"] for p in retained] == ["w1:p5"]
        assert retained[0]["remedy"] == "herdr pane read w1:p5"
        assert titles == [("note", "w1:p5")]

    def test_non_terminal_child_keeps_its_pane_open_and_reports_it(
        self, implement_mod, monkeypatch,
    ):
        """A success spawn whose child is still non-terminal retains the pane.

        The close-or-retain decision is the *parent report's* verdict, never
        the session's exit status: a session that exited cleanly but whose
        child is still ``in-progress`` (for example it stopped early without
        committing) must leave its pane open and reported, so the operator
        can inspect what the agent did instead of losing the transcript
        (SA-0MVPHZ1CI0045QR0 AC3/AC4, parent SA-0MVLKHY27005UK40 AC4).
        """
        m = implement_mod
        titles: list[tuple] = []
        monkeypatch.setattr(
            m, "_update_pane_title",
            lambda status, **kw: (
                titles.append((status, kw.get("pane_id")))
                or {"updated": True}
            ),
        )
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {
                "success": True,
                "next_child": None,
                "message": "SA-C1 is still in progress.",
                "children_processed": [
                    {"id": "SA-C1", "status": "in-progress",
                     "action": "started"},
                ],
            },
        ])
        spawner = RecordingSpawner([_pane_result(pane="w1:p6")])
        client = RecordingPaneClient()

        report = _run(m, runner, spawner, [{"id": "SA-C1"}],
                      pane_client=client)

        # The session exited successfully, but terminality was never
        # confirmed — so the pane is never closed on the exit status alone.
        assert report["sessions_spawned"] == 1
        assert report["parent_advanced"] is False
        assert client.closed == []
        entry = report["children_driven"][0]
        assert entry["pane_closed"] is False
        assert entry["pane_retained"] == "w1:p6"
        retained = report["retained_panes"]
        assert [p["pane_id"] for p in retained] == ["w1:p6"]
        assert "not terminal" in retained[0]["reason"]
        assert retained[0]["remedy"] == "herdr pane read w1:p6"
        # The retained pane carries its ⚠️ verdict title for the operator.
        assert titles == [("note", "w1:p6")]

    def test_drive_ending_before_resolution_retains_the_pane_untitled(
        self, implement_mod, monkeypatch,
    ):
        """A queued pane the drive never resolves is reported, not leaked.

        When the loop stops before the follow-up parent report names the
        child's state (no startable child left), the pane is still reported
        with its inspection remedy. No verdict title is applied: the drive
        has nothing to announce about that session yet
        (SA-0MVPHZ1CI0045QR0 AC3/AC4, parent SA-0MVLKHY27005UK40 AC4).
        """
        m = implement_mod
        titles: list[tuple] = []
        monkeypatch.setattr(
            m, "_update_pane_title",
            lambda status, **kw: (
                titles.append((status, kw.get("pane_id")))
                or {"updated": True}
            ),
        )
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {
                "success": True,
                "next_child": None,
                "message": "No startable child remained.",
                "children_processed": [],
            },
        ])
        spawner = RecordingSpawner([_pane_result(pane="w1:p7")])
        client = RecordingPaneClient()

        report = _run(m, runner, spawner, [{"id": "SA-C1"}],
                      pane_client=client)

        assert report["sessions_spawned"] == 1
        assert report["parent_advanced"] is False
        assert client.closed == []
        entry = report["children_driven"][0]
        assert entry["pane_closed"] is False
        assert entry["pane_retained"] == "w1:p7"
        assert "pane_title" not in entry
        retained = report["retained_panes"]
        assert [p["pane_id"] for p in retained] == ["w1:p7"]
        assert "drive ended before the parent reported" in retained[0]["reason"]
        assert retained[0]["remedy"] == "herdr pane read w1:p7"
        assert titles == []

    def test_headless_entries_carry_transport_and_pane_id(self, implement_mod):
        """Report parity holds for the headless path too (additive keys)."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(m, runner, spawner, [{"id": "SA-C1"}])

        entry = report["children_driven"][0]
        assert entry["transport"] == "headless"
        assert entry["pane_id"] is None
        assert "retained_panes" not in report

    def test_human_report_lists_retained_panes_and_transport(
        self, implement_mod, capsys,
    ):
        m = implement_mod
        report = {
            "phase": "drive",
            "work_item_id": PARENT,
            "success": False,
            "parent_advanced": False,
            "sessions_spawned": 1,
            "children_driven": [{
                "id": "SA-C1",
                "attempt": 1,
                "result": "blocked",
                "transport": "pane",
                "pane_id": "w1:p2",
                "pane_retained": "w1:p2",
            }],
            "blocked_child": "SA-C1",
            "blocked_pane": "w1:p2",
            "retained_panes": [{
                "id": "SA-C1",
                "pane_id": "w1:p2",
                "verdict": "attention",
                "reason": "the driven agent reported `blocked`",
                "remedy": "herdr pane read w1:p2",
            }],
            "message": "Child SA-C1 reported `blocked`; chain stopped.",
        }

        m._emit_phase_report(report, json_output=False)
        out = capsys.readouterr().out

        assert "🚫 SA-C1" in out
        assert "transport: pane, pane w1:p2" in out
        assert "Panes retained for inspection: 1" in out
        assert "herdr pane read w1:p2" in out


# ---------------------------------------------------------------------------
# SA-0MWFJSCPT005BO2R — pre-spawn re-validation: never spawn a no-op session
# ---------------------------------------------------------------------------

TERMINAL_CHILD = {"status": "completed", "stage": "in_review"}
OPEN_CHILD = {"status": "open", "stage": "intake_complete"}


class TestRevalidateStartableBeforeSpawn:
    """The reported ``next_child`` is re-validated before its session spawns.

    The recorded incident (AI_Hell, 2026-11-13) had nine ~50 s implement
    sessions that loaded the skill and exited without doing work. The
    session-level root cause was a provider error on the first turn (three
    messages, empty assistant message with ``stopReason: "error"``), which a
    state check cannot predict; the driver's own no-op class is a **stale
    ``parent`` report** — a child that is already terminal, or whose
    ``blocked`` hold still holds, by the time ``phase_drive`` acts on it.
    ``phase_drive`` therefore re-fetches the reported child immediately before
    the spawn and records the verdict in ``revalidated_children`` instead of
    paying for a session that can only no-op. Spawning for a genuinely
    startable child is unchanged (regression pinned below).
    """

    def test_already_terminal_child_is_not_spawned(self, implement_mod):
        """A terminal child is skipped and the chain continues with the next."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-DONE"),
            _child_report("SA-C2"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()
        children = [
            {"id": "SA-DONE", "status": "completed", "stage": "in_review"},
            {"id": "SA-C2", "status": "open", "stage": "intake_complete"},
        ]

        report = _run(
            m, runner, spawner, children,
            child_states={"SA-DONE": dict(TERMINAL_CHILD),
                          "SA-C2": dict(OPEN_CHILD)},
        )

        assert [c["id"] for c in spawner.calls] == ["SA-C2"]
        assert report["sessions_spawned"] == 1
        assert report["parent_advanced"] is True
        assert report["success"] is True
        verdict = report["revalidated_children"][0]
        assert verdict["id"] == "SA-DONE"
        assert verdict["startable"] is False
        assert verdict["action"] == "skip-terminal"
        assert "already terminal" in verdict["reason"]
        # The startable sibling's verdict is recorded too (additive surface).
        assert report["revalidated_children"][1]["startable"] is True
        assert "failed_child" not in report

    def test_soft_deleted_child_is_not_spawned(self, implement_mod):
        """A soft-deleted child (``deletedBy`` set, status open) is terminal."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-DEL"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner,
            [{"id": "SA-DEL", "status": "open", "deletedBy": "operator"}],
            child_states={"SA-DEL": {
                "status": "open", "stage": "intake_complete",
                "deletedBy": "operator",
            }},
        )

        assert spawner.calls == []
        assert report["sessions_spawned"] == 0
        assert report["success"] is True
        assert report["revalidated_children"][0]["action"] == "skip-terminal"

    def test_stage_only_terminal_child_is_not_spawned(self, implement_mod):
        """A child left at ``stage: in_review`` is terminal for the driver.

        The spawn-failure reconciliation already treats this shape as terminal
        (SA-0MVHXWLPG007OQVN); a pre-spawn decision must agree with it, or a
        session is spawned for a child that can only no-op.
        """
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-REV"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner,
            [{"id": "SA-REV", "status": "open", "stage": "in_review"}],
            child_states={"SA-REV": {"status": "open", "stage": "in_review"}},
        )

        assert spawner.calls == []
        assert report["revalidated_children"][0]["action"] == "skip-terminal"

    def test_blocked_child_whose_block_still_holds_is_not_spawned(
        self, implement_mod,
    ):
        """A ``blocked`` child with a non-terminal blocker spawns nothing."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-B"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner,
            [{"id": "SA-B", "status": "blocked", "stage": "plan_complete"}],
            child_states={"SA-B": {
                "status": "blocked", "stage": "plan_complete",
            }},
            blockers={"SA-B": [{"id": "SA-X", "status": "in-progress"}]},
        )

        assert spawner.calls == []
        assert report["sessions_spawned"] == 0
        assert report["success"] is True
        assert report["action"] == "skip-blocked"
        assert report["blocked_children"] == ["SA-B"]
        assert report["blocked_child_details"][0]["blocking_ids"] == ["SA-X"]
        assert report["revalidated_children"][0]["startable"] is False
        assert "block still holds" in report["revalidated_children"][0]["reason"]

    def test_stale_block_is_startable_again(self, implement_mod):
        """A ``blocked`` child whose blockers are all terminal is spawned."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-B"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner,
            [{"id": "SA-B", "status": "blocked", "stage": "plan_complete"}],
            child_states={"SA-B": {
                "status": "blocked", "stage": "plan_complete",
            }},
            blockers={"SA-B": [{"id": "SA-X", "status": "completed"}]},
        )

        assert [c["id"] for c in spawner.calls] == ["SA-B"]
        assert report["sessions_spawned"] == 1
        verdict = report["revalidated_children"][0]
        assert verdict["startable"] is True
        assert verdict["stale_block_ids"] == ["SA-X"]

    def test_genuinely_startable_child_is_still_spawned(self, implement_mod):
        """Regression: the re-validation never blocks real work."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner,
            [{"id": "SA-C1", "status": "open", "stage": "intake_complete"}],
            child_states={"SA-C1": dict(OPEN_CHILD)},
        )

        assert [c["id"] for c in spawner.calls] == ["SA-C1"]
        assert report["sessions_spawned"] == 1
        assert report["revalidated_children"] == [{
            "id": "SA-C1",
            "startable": True,
            "action": "implement",
            "status": "open",
            "stage": "intake_complete",
            "reason": "",
        }]

    def test_in_progress_child_is_startable(self, implement_mod):
        """An in-progress child is a resume, never a refused spawn."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-IP"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner,
            [{"id": "SA-IP", "status": "in_progress"}],
            child_states={"SA-IP": {
                "status": "in_progress", "stage": "in_progress",
            }},
        )

        assert [c["id"] for c in spawner.calls] == ["SA-IP"]
        assert report["revalidated_children"][0]["startable"] is True

    def test_unreadable_child_fails_open(self, implement_mod):
        """A transient worklog read failure never stalls the chain."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner,
            [{"id": "SA-C1", "status": "open"}],
            child_states={},  # every child fetch returns {} (unreadable)
        )

        assert [c["id"] for c in spawner.calls] == ["SA-C1"]
        verdict = report["revalidated_children"][0]
        assert verdict["startable"] is True
        assert "could not be re-fetched" in verdict["reason"]

    def test_rerunning_the_same_batch_produces_zero_no_op_sessions(
        self, implement_mod,
    ):
        """AC3: the same batch re-run spawns no session for a done child."""
        m = implement_mod
        spawner = RecordingSpawner()

        for _ in range(2):
            runner = ScriptedParent([
                _child_report("SA-DONE"),
                {"success": True, "parent_advanced": True,
                 "message": "advanced"},
            ])
            report = _run(
                m, runner, spawner,
                [{"id": "SA-DONE", "status": "completed",
                  "stage": "in_review"}],
                child_states={"SA-DONE": dict(TERMINAL_CHILD)},
            )
            assert report["sessions_spawned"] == 0
            assert [v["id"] for v in report["revalidated_children"]] == [
                "SA-DONE"
            ]
            assert report["revalidated_children"][0]["startable"] is False
            assert report["success"] is True

        assert spawner.calls == []

    def test_a_parent_that_keeps_offering_the_same_child_stops(
        self, implement_mod,
    ):
        """The re-validation skip cannot spin: a repeat offer stops the chain."""
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-DONE"),
            _child_report("SA-DONE"),
        ])
        spawner = RecordingSpawner()

        report = _run(
            m, runner, spawner,
            [{"id": "SA-DONE", "status": "completed", "stage": "in_review"}],
            child_states={"SA-DONE": dict(TERMINAL_CHILD)},
        )

        assert spawner.calls == []
        assert report["sessions_spawned"] == 0
        assert report["parent_advanced"] is False
        assert "keeps offering" in report["message"]
        assert "failed_child" not in report

    def test_human_report_names_a_revalidated_skip(
        self, implement_mod, capsys,
    ):
        """The human summary must show the skip, never present it as work."""
        m = implement_mod
        report = {
            "phase": "drive",
            "work_item_id": PARENT,
            "success": True,
            "parent_advanced": False,
            "sessions_spawned": 0,
            "children_driven": [],
            "revalidated_children": [{
                "id": "SA-DONE",
                "startable": False,
                "action": "skip-terminal",
                "status": "completed",
                "stage": "in_review",
                "reason": "it is already terminal "
                          "(status=completed, stage=in_review)",
            }],
            "message": "No session spawned for SA-DONE.",
        }

        m._emit_phase_report(report, json_output=False)
        out = capsys.readouterr().out

        assert "⏭ SA-DONE (skipped, no session)" in out
        assert "already terminal" in out


# ---------------------------------------------------------------------------
# F2: spawn-time provider-failure retry ordering (SA-0MWLDZBTF004LJFP)
# ---------------------------------------------------------------------------


def _provider_failure_result(cid: str, model: str = "MunitoAI/*") -> dict:
    """Scripted spawner result for a child session that died at model
    resolution (the ``_default_session_spawner`` non-zero-exit shape)."""
    return {
        "success": False,
        "returncode": 1,
        "reason": (
            f"Child session for {cid} exited with code 1: Error: No models "
            f'match pattern "{model}"'
        ),
    }


class TestSpawnInfraRetryOrdering:
    """Ordering: reconciliation outranks retry (F2 AC4).

    The retry decision runs *after* ``_reconcile_spawn_failure``: a child the
    reconciliation proved terminal (``completed-late``) is never retried, so a
    provider reason in the spawn output can never spawn a second session for
    work that is already done.
    """

    def test_completed_late_child_is_never_retried(self, implement_mod):
        m = implement_mod
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True, "message": "advanced"},
        ])
        spawner = RecordingSpawner([_provider_failure_result("SA-C1")])
        # Startable at the spawn decision, terminal when the killed session is
        # reconciled — the exact production shape the incident took.
        states = {
            "SA-C1": [
                {"id": "SA-C1", "status": "in_progress",
                 "stage": "intake_complete"},
                {"id": "SA-C1", "status": "completed",
                 "stage": "in_review"},
            ],
        }

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            child_states=states, infra_retry_limit=5, delay=lambda _s: None,
        )

        # Exactly one session: the terminal child was reconciled, not retried.
        assert len(spawner.calls) == 1
        assert report["success"] is True
        assert report["infra_retries"] == {}
        assert report["completed_late"] == 1
        assert [
            entry["result"] for entry in report["children_driven"]
        ] == ["completed-late"]
        assert not any(
            entry.get("result") == "infra-retry"
            for entry in report["children_driven"]
        )


# ---------------------------------------------------------------------------
# F1: drive model resolution — catalogue parsing + fast-fail pre-flight
# (SA-0MWOVAOMY008DESN, parent SA-0MWOUI3Q0006TTSY)
# ---------------------------------------------------------------------------
#
# This is the F1 *test-first* slice for the epic "drive cannot spawn headless
# child sessions — `pi -p` fails model resolution". The production helpers the
# cases below exercise are introduced by the sibling implementation slices:
#
#   F3 (SA-0MWOVCA0Z006RPIP) — _parse_model_catalogue, _available_providers,
#       _resolve_child_session_model, _resolve_drive_model and the phase_drive
#       pre-flight refusal (before the child loop, no lock/spawn/retry);
#   F4 (SA-0MWOVCDJ50076081) — wire the resolved pair into both spawners.
#
# Until those land every case is a **strict xfail tracer bullet**: it fails
# against the current code, the failure is the recorded red observation, and
# F3/F4 remove the markers once the helpers exist (a strict marker turns an
# unexpected pass into an XPASS failure, so the handoff cannot be forgotten).
#
# Contract pinned here (single source of truth for F3/F4):
#
# ``_parse_model_catalogue(text) -> tuple[tuple[str, str], ...]``
#     Parses the captured ``pi --list-models`` table (``provider  model
#     context  max-out  thinking  images``) into ordered ``(provider, model)``
#     pairs. Drops the header, ignores stderr-style ``Warning: …`` lines, blank
#     lines and malformed rows, and keeps providers whose display name contains
#     spaces (``Local Proxy``).
#
# ``_available_providers(catalogue) -> tuple[str, ...]``
#     Unique providers in first-appearance catalogue order.
#
# ``_resolve_child_session_model(candidates, catalogue) -> dict``
#     ``candidates`` is an ordered list of ``{"source", "provider", "model"}``
#     dicts (precedence order). The first candidate that maps to exactly one
#     concrete catalogue entry wins and yields
#     ``{"resolved": "provider/model", "source", "pattern", "error": ""}``.
#     A glob must match exactly one model; zero or multiple matches are
#     unresolvable (never a literal wildcard forwarded to ``pi``). When no
#     candidate resolves: ``{"resolved": None, "source": None, "pattern":
#     <first attempted>, "error": <actionable message>}``.
#
# ``phase_drive(..., resolve_drive_model=<callable>(env, worktree_path))``
#     Injectable resolution seam (mirrors ``parent_runner`` / ``spawn_session``
#     so the pre-flight is hermetic — no ``pi --list-models`` subprocess, no
#     host settings). The real default is ``_resolve_drive_model``. The
#     resolver runs **once** before the child loop; an unresolved model stops
#     the drive with ``success: False``, ``sessions_spawned: 0``, an empty
#     ``infra_retries`` counter, the resolution recorded under
#     ``report["model_resolution"]``, and a ``message`` naming both the
#     unresolvable pattern and the available providers.
#
# Hermetic by construction: the catalogue comes from a captured fixture and the
# resolver is injected — nothing shells out to ``pi`` or reads the host's
# ``~/.pi/agent/models.json``.

_RED_FIRST_REASON = (
    "F1 red-first tracer bullet (SA-0MWOVAOMY008DESN): drive model "
    "resolution/pre-flight is implemented by F3 (SA-0MWOVCA0Z006RPIP) and "
    "F4 (SA-0MWOVCDJ50076081); F3/F4 remove this strict marker when it lands."
)

#: Captured ``pi --list-models`` stdout (columns: provider, model, context,
#: max-out, thinking, images). The two ``MunitoAI`` rows make the historical
#: ``MunitoAI/*`` glob ambiguous (the exactly-one rule then reports it
#: unresolvable); ``Local Proxy/code`` is the concrete pair that resolves.
_CAPTURED_LIST_MODELS = """\
provider        model                                 context  max-out  thinking  images
deepseek        deepseek-flash                        1.0M     393.2K   yes       yes
deepseek        deepseek-v4-pro                       1.0M     393.2K   yes       no
Local Proxy     author                                128K     16.4K    no        no
Local Proxy     code                                  128K     16.4K    no        no
Local Proxy     plan                                  128K     16.4K    no        no
MunitoAI        deepseek-ai/deepseek-v4-flash         400K     16.4K    no        no
MunitoAI        deepseek-ai/deepseek-v4.1-flash-1017  400K     16.4K    no        no
opencode        beam-free                             1.0M     131.1K   yes       no
"""

#: The table preceded by the stderr-style warning Pi emits for an unservable
#: ``enabledModels`` entry, plus a trailing blank line — the parser must skip
#: both rather than treating them as catalogue rows.
_CAPTURED_LIST_MODELS_WITH_WARNING = (
    'Warning: No models match pattern "MunitoAI/*"\n'
    + _CAPTURED_LIST_MODELS
    + "\n"
)


def _require_helper(mod, name: str):
    """Return ``mod.<name>`` or fail with an F3-attributed message.

    A missing helper is the red observation this slice records; raising a
    plain ``pytest.fail`` keeps the strict-``xfail`` marker meaningful and the
    reason legible instead of an opaque ``AttributeError``.
    """
    helper = getattr(mod, name, None)
    if helper is None:
        pytest.fail(
            f"implement.py does not expose {name} yet — it is introduced by "
            "F3 (SA-0MWOVCA0Z006RPIP). Red-first tracer bullet until then."
        )
    return helper


def _candidates(*triples: tuple[str, str, str]) -> list[dict]:
    """Build ordered ``_resolve_child_session_model`` candidates."""
    return [
        {"source": source, "provider": provider, "model": model}
        for source, provider, model in triples
    ]


class TestParseModelCatalogue:
    """``pi --list-models`` → ``(provider, model)`` pairs (F1 AC4)."""

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_parses_provider_model_pairs_from_captured_catalogue(
        self, implement_mod,
    ):
        parse = _require_helper(implement_mod, "_parse_model_catalogue")
        catalogue = parse(_CAPTURED_LIST_MODELS)

        # Providers whose display name contains a space are preserved whole.
        assert ("Local Proxy", "code") in catalogue
        assert ("deepseek", "deepseek-flash") in catalogue
        assert ("MunitoAI", "deepseek-ai/deepseek-v4-flash") in catalogue
        # Every entry is a (provider, model) pair and order is preserved.
        assert all(len(pair) == 2 for pair in catalogue)
        assert catalogue[0] == ("deepseek", "deepseek-flash")

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_ignores_header_warning_and_blank_lines(self, implement_mod):
        parse = _require_helper(implement_mod, "_parse_model_catalogue")
        catalogue = parse(_CAPTURED_LIST_MODELS_WITH_WARNING)

        providers = {provider for provider, _ in catalogue}
        # The header and the stderr warning never become catalogue rows.
        assert "provider" not in providers
        assert "" not in providers
        assert not any("Warning:" in provider for provider in providers)
        # Exactly the eight real rows survive.
        assert len(catalogue) == 8
        assert ("opencode", "beam-free") in catalogue

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_available_providers_are_unique_in_catalogue_order(
        self, implement_mod,
    ):
        parse = _require_helper(implement_mod, "_parse_model_catalogue")
        available = _require_helper(implement_mod, "_available_providers")

        providers = available(parse(_CAPTURED_LIST_MODELS))

        assert tuple(providers) == (
            "deepseek", "Local Proxy", "MunitoAI", "opencode",
        )


class TestResolveChildSessionModel:
    """Exactly-one-concrete-match rule and precedence (F1 ACs 1, 3)."""

    @staticmethod
    def _resolve(mod, candidates):
        parse = _require_helper(mod, "_parse_model_catalogue")
        resolve = _require_helper(mod, "_resolve_child_session_model")
        return resolve(candidates, parse(_CAPTURED_LIST_MODELS))

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_resolves_an_exact_concrete_pair(self, implement_mod):
        result = self._resolve(
            implement_mod,
            _candidates(("env:IMPLEMENT_DRIVE", "Local Proxy", "code")),
        )

        assert result["resolved"] == "Local Proxy/code"
        assert result["source"] == "env:IMPLEMENT_DRIVE"
        assert result["pattern"] == "Local Proxy/code"
        assert not result["error"]

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_resolves_a_glob_that_matches_exactly_one(self, implement_mod):
        result = self._resolve(
            implement_mod,
            _candidates(("settings", "Local Proxy", "co*")),
        )

        assert result["resolved"] == "Local Proxy/code"
        # A concrete pair is returned, never the literal wildcard.
        assert "*" not in result["resolved"]
        assert "?" not in result["resolved"]

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_glob_matching_multiple_is_unresolvable(self, implement_mod):
        # The historical `MunitoAI/*` matches two concrete models.
        result = self._resolve(
            implement_mod,
            _candidates(("settings", "MunitoAI", "*")),
        )

        assert result["resolved"] is None
        assert result["pattern"] == "MunitoAI/*"
        assert result["error"]

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_glob_matching_nothing_is_unresolvable(self, implement_mod):
        result = self._resolve(
            implement_mod,
            _candidates(("settings", "DoesNotExist", "*")),
        )

        assert result["resolved"] is None
        assert result["pattern"] == "DoesNotExist/*"
        assert result["error"]

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_precedence_first_resolvable_candidate_wins(self, implement_mod):
        result = self._resolve(
            implement_mod,
            _candidates(
                # Highest precedence candidate is ambiguous → skipped.
                ("env:IMPLEMENT_DRIVE", "MunitoAI", "*"),
                # Lower precedence candidate resolves concretely → wins.
                ("env:PI", "Local Proxy", "code"),
            ),
        )

        assert result["resolved"] == "Local Proxy/code"
        assert result["source"] == "env:PI"


#: Resolution verdicts injected through the ``resolve_drive_model`` seam.
_RESOLVED_MODEL = {
    "resolved": "Local Proxy/code",
    "source": "settings",
    "pattern": "Local Proxy/code",
    "error": "",
    "available_providers": (
        "deepseek", "github-copilot", "Local Proxy", "MunitoAI", "opencode",
    ),
}

_UNRESOLVED_MODEL = {
    "resolved": None,
    "source": "settings",
    "pattern": "MunitoAI/*",
    "error": (
        'Model pattern "MunitoAI/*" matches no single concrete model in the '
        "available catalogue."
    ),
    "available_providers": (
        "deepseek", "github-copilot", "Local Proxy", "MunitoAI", "opencode",
    ),
}


class TestDriveModelFastFail:
    """``phase_drive`` refuses before spawning when the model is unresolvable.

    F1 AC1/AC2/AC3: the fast-fail message names the pattern and the available
    providers; no session is spawned and no infrastructure retry is consumed.
    """

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_unresolvable_model_fails_fast_without_spawning(
        self, implement_mod,
    ):
        m = implement_mod
        _require_helper(m, "_resolve_drive_model")
        # An empty script: any parent call raises, proving the refusal ran
        # before the child loop was ever entered.
        runner = ScriptedParent([])
        spawner = RecordingSpawner()
        resolver = mock.Mock(return_value=dict(_UNRESOLVED_MODEL))

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            resolve_drive_model=resolver,
        )

        assert report["success"] is False
        assert report["sessions_spawned"] == 0
        assert spawner.calls == []
        assert runner.calls == []
        assert report["infra_retries"] == {}
        assert report["model_resolution"] == _UNRESOLVED_MODEL

        message = report["message"]
        assert "MunitoAI/*" in message
        for provider in _UNRESOLVED_MODEL["available_providers"]:
            assert provider in message

    @pytest.mark.xfail(strict=True, reason=_RED_FIRST_REASON)
    def test_drive_resolves_the_model_once_before_spawning(
        self, implement_mod,
    ):
        m = implement_mod
        _require_helper(m, "_resolve_drive_model")
        runner = ScriptedParent([
            _child_report("SA-C1"),
            {"success": True, "parent_advanced": True},
        ])
        spawner = RecordingSpawner()
        resolver = mock.Mock(return_value=dict(_RESOLVED_MODEL))

        report = _run(
            m, runner, spawner, [{"id": "SA-C1"}],
            resolve_drive_model=resolver,
        )

        # One catalogue-backed resolution per drive invocation, then the
        # child is spawned normally on the resolved pair.
        assert resolver.call_count == 1
        assert report["success"] is True
        assert report["sessions_spawned"] == 1
        assert [call["id"] for call in spawner.calls] == ["SA-C1"]
