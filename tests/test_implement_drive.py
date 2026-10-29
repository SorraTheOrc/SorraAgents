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

    ``phase_drive`` fetches the parent once and — after a failed spawn —
    re-fetches the affected child to classify it. ``states`` maps child id to
    the work-item dict ``wl_show`` should return for it. The parent fetch
    (``PARENT``) and any unmapped id get a minimal placeholder, so a fetch for
    the wrong id shows up as a non-terminal child instead of passing silently.

    Returns the patch plus the list of ids fetched, so a test can assert that
    reconciliation only happens on the failure path.
    """
    calls: list[str] = []

    def _show(work_item_id):
        calls.append(work_item_id)
        if work_item_id == PARENT:
            return {"id": PARENT}
        return states.get(work_item_id, {})

    return mock.patch.object(m, "wl_show", side_effect=_show), calls


def _run(m, runner, spawner, children, child_states=None, **kwargs):
    patches = list(_patch_parent(m, children))
    calls: list[str] = []
    comments: list[tuple[str, str]] = []
    if child_states is not None:
        state_patch, calls = _patch_child_state(m, child_states)
        patches.append(state_patch)
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
        states = {
            "SA-C1": {
                "id": "SA-C1", "status": "completed",
                "stage": "in_review",
            },
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
            child_states={"SA-C1": {
                "id": "SA-C1", "status": "completed",
                "stage": "in_review",
            }},
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
            child_states={"SA-C1": {
                "id": "SA-C1", "status": "done", "stage": "in_review",
            }},
        )

        assert "SA-C1" in report["_wl_show_calls"]
        assert report["success"] is True
        assert report["parent_advanced"] is True
        assert report["children_driven"][0]["result"] == "completed-late"

    def test_successful_spawn_does_not_re_fetch_child(self, implement_mod):
        """Reconciliation is scoped to the failure path (risk R1)."""
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
        assert "SA-C1" not in report["_wl_show_calls"]

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
            child_states={"SA-C1": {
                "id": "SA-C1", "status": "completed", "stage": "in_review",
            }},
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
            child_states={"SA-C1": {
                "id": "SA-C1", "status": "completed", "stage": "in_review",
            }},
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
            "SA-C1": {"id": "SA-C1", "status": "completed",
                      "stage": "in_review"},
            "SA-C2": {"id": "SA-C2", "status": "in_review",
                      "stage": "in_review"},
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
            child_states={"SA-C1": {
                "id": "SA-C1", "status": "completed", "stage": "in_review",
            }},
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
