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

The parent state machine and the session spawner are both injected so the
orchestration loop is exercised deterministically (no worklog, no subprocess).
"""

from __future__ import annotations

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


def _run(m, runner, spawner, children, **kwargs):
    p1, p2, p3 = _patch_parent(m, children)
    with p1, p2, p3:
        return m.phase_drive(
            PARENT, parent_runner=runner, spawn_session=spawner, **kwargs
        )


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
        assert [e["result"] for e in report["children_driven"]] == ["ok", "ok"]

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
