"""
Tests for machine-hygiene action execution and the evaluate.py orchestrator.

Covers:
- AC1: only approved actions execute (nothing without approval)
- AC2: approved vs declined are tracked and reported
- AC3: kill / renice / prune / pace are supported
- AC4: kill safety checks (uid, system-critical, session context)
- AC5: evaluate.run_workflow orchestrates the six steps
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, _SKILL_DIR / "scripts" / filename
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


actions = _load("machine_hygiene_actions", "actions.py")
evaluate = _load("machine_hygiene_evaluate", "evaluate.py")


def _safe_ancestry(pid):
    return {
        "pid": pid, "name": "grep", "ppid": 1, "uid": 1000, "session": 5,
        "systemd_unit": "session-5.scope", "ancestors": [], "available": True,
    }


class _KillRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))


# ---------------------------------------------------------------------------
# verify_kill_safety (AC4)
# ---------------------------------------------------------------------------

class TestVerifyKillSafety(unittest.TestCase):
    def test_safe_process(self):
        result = actions.verify_kill_safety(123, _safe_ancestry(123), operator_uid=1000)
        self.assertTrue(result["safe"])
        self.assertEqual(result["reasons"], [])

    def test_uid_mismatch_unsafe(self):
        ancestry = _safe_ancestry(123)
        ancestry["uid"] = 0
        result = actions.verify_kill_safety(123, ancestry, operator_uid=1000)
        self.assertFalse(result["safe"])
        self.assertTrue(any("uid" in r for r in result["reasons"]))

    def test_system_critical_unsafe(self):
        ancestry = _safe_ancestry(123)
        ancestry["name"] = "systemd"
        result = actions.verify_kill_safety(123, ancestry, operator_uid=1000)
        self.assertFalse(result["safe"])
        self.assertTrue(any("system-critical" in r for r in result["reasons"]))

    def test_protected_pid_unsafe(self):
        result = actions.verify_kill_safety(1, _safe_ancestry(1), operator_uid=1000)
        self.assertFalse(result["safe"])

    def test_missing_ancestry_unsafe(self):
        self.assertFalse(actions.verify_kill_safety(123, None, operator_uid=1000)["safe"])
        self.assertFalse(actions.verify_kill_safety(123, {"available": False},
                                                    operator_uid=1000)["safe"])

    def test_missing_session_unsafe(self):
        ancestry = _safe_ancestry(123)
        ancestry["session"] = None
        self.assertFalse(
            actions.verify_kill_safety(123, ancestry, operator_uid=1000)["safe"]
        )


# ---------------------------------------------------------------------------
# execute_actions (AC1/AC2/AC3)
# ---------------------------------------------------------------------------

class TestExecuteActions(unittest.TestCase):
    def test_approved_kill_executes(self):
        recorder = _KillRecorder()
        action = {"action": "kill", "target": {"name": "grep", "pids": [123]}}
        outcome = actions.execute_actions(
            [action], dry_run=False, operator_uid=1000,
            kill_fn=recorder, ancestry_fn=_safe_ancestry,
        )
        self.assertEqual(len(outcome["executed"]), 1)
        self.assertEqual(recorder.calls, [(123, 15)])  # SIGTERM

    def test_unsafe_kill_is_skipped(self):
        recorder = _KillRecorder()

        def unsafe(pid):
            return {"pid": pid, "name": "systemd", "uid": 1000, "session": 1,
                    "ancestors": [], "available": True}

        action = {"action": "kill", "target": {"name": "systemd", "pids": [1]}}
        outcome = actions.execute_actions(
            [action], dry_run=False, operator_uid=1000,
            kill_fn=recorder, ancestry_fn=unsafe,
        )
        self.assertEqual(outcome["executed"], [])
        self.assertEqual(len(outcome["skipped"]), 1)
        self.assertEqual(recorder.calls, [])

    def test_dry_run_never_calls_kill(self):
        recorder = _KillRecorder()
        action = {"action": "kill", "target": {"name": "grep", "pids": [123]}}
        outcome = actions.execute_actions(
            [action], dry_run=True, operator_uid=1000,
            kill_fn=recorder, ancestry_fn=_safe_ancestry,
        )
        self.assertEqual(len(outcome["dry_run"]), 1)
        self.assertEqual(recorder.calls, [])

    def test_declined_actions_are_reported_not_executed(self):
        recorder = _KillRecorder()
        approved = [{"action": "pace", "target": {"scope": "x"}}]
        declined = [{"action": "kill", "target": {"name": "grep", "pids": [123]}}]
        outcome = actions.execute_actions(
            approved, declined, dry_run=False, operator_uid=1000,
            kill_fn=recorder, ancestry_fn=_safe_ancestry,
        )
        self.assertEqual(outcome["approved_count"], 1)
        self.assertEqual(outcome["declined_count"], 1)
        self.assertEqual(recorder.calls, [])
        self.assertTrue(any(r["action"] == "pace" for r in outcome["results"]))

    def test_renice_executes_with_injected_fn(self):
        calls = []
        action = {"action": "renice", "target": {"name": "node", "pids": [55]}}
        outcome = actions.execute_actions(
            [action], dry_run=False, renice_fn=lambda pid, inc: calls.append((pid, inc)),
        )
        self.assertEqual(calls, [(55, 10)])
        self.assertEqual(len(outcome["executed"]), 1)

    def test_prune_stale_sessions(self):
        pruned = []
        action = {"action": "prune", "target": {"sessions": [{"id": 1}, {"id": 2}]}}
        outcome = actions.execute_actions(
            [action], dry_run=False, prune_fn=lambda s: pruned.extend(s),
        )
        self.assertEqual(len(pruned), 2)
        self.assertEqual(outcome["executed"][0]["action"], "prune")

    def test_prune_no_sessions_skipped(self):
        outcome = actions.execute_actions(
            [{"action": "prune", "target": {}}], dry_run=False,
        )
        self.assertEqual(outcome["skipped"][0]["action"], "prune")

    def test_pace_is_reported_non_destructive(self):
        outcome = actions.execute_actions(
            [{"action": "pace", "target": {"scope": "concurrent-sessions"}}],
            dry_run=False,
        )
        self.assertEqual(outcome["results"][0]["status"], "reported")


# ---------------------------------------------------------------------------
# evaluate.run_workflow (AC5)
# ---------------------------------------------------------------------------

def _fake_metrics():
    return {
        "load_average": {"value": 30.0, "status": "critical", "description": "load"},
        "cpu_pressure": {"value": 90.0, "status": "critical", "description": "cpu"},
        "memory_pressure": {"value": None, "status": "warning", "description": "n/a"},
        "io_pressure": {"value": None, "status": "warning", "description": "n/a"},
        "memory_usage": {"value": 50.0, "status": "low", "description": "mem"},
        "swap_usage": {"value": 0.0, "status": "low", "description": "swap"},
        "process_count": {"value": 100, "status": "low", "description": "procs"},
        "thread_count": {"value": 200, "status": "low", "description": "threads"},
        "rss_top10": {"value": 0, "status": "low", "description": "rss"},
        "rss_breakdown": {"value": [
            {"name": "grep", "total_rss_bytes": 2 * 1024 ** 3,
             "process_count": 1, "pids": [(123, 2 * 1024 ** 3)]},
        ], "status": "low"},
        "runnable_processes": {"value": 20, "status": "warning", "description": "run"},
        "zombie_processes": {"value": 0, "status": "low", "description": "none"},
        "stale_lock_files": {"value": 0, "status": "low", "description": "none"},
    }


class TestRunWorkflow(unittest.TestCase):
    def _run(self, response, dry_run=True):
        return evaluate.run_workflow(
            metrics_fn=_fake_metrics,
            ancestry_fn=_safe_ancestry,
            input_fn=lambda _: response,
            output_fn=lambda _: None,
            dry_run=dry_run,
        )

    def test_orchestrates_six_steps(self):
        """AC5: before → pressures → actions → gate → execute → after."""
        result = self._run("all")
        for key in ("before", "pressures", "actions", "approved",
                    "outcome", "after", "before_table", "after_table"):
            self.assertIn(key, result)
        self.assertTrue(result["pressures"])

    def test_decline_executes_nothing(self):
        """AC1: no approval → no destructive outcome."""
        result = self._run("none")
        self.assertEqual(result["outcome"]["executed"], [])
        self.assertEqual(result["outcome"]["approved_count"], 0)

    def test_auto_approve_runs_all(self):
        result = evaluate.run_workflow(
            metrics_fn=_fake_metrics,
            ancestry_fn=_safe_ancestry,
            output_fn=lambda _: None,
            auto_approve=True,
            dry_run=True,
        )
        self.assertEqual(result["outcome"]["approved_count"], len(result["actions"]))
        self.assertEqual(result["outcome"]["executed"], [])
        self.assertTrue(result["outcome"]["dry_run"] or not result["actions"])


class TestCli(unittest.TestCase):
    def test_json_mode(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # Avoid real /proc + real prompt: --yes --dry-run --json.
            rc = evaluate.main(["--json", "--yes", "--dry-run"])
        payload = json.loads(buf.getvalue())
        self.assertIn("before", payload)
        self.assertIn("actions", payload)
        self.assertIn(rc, (0, 3))


if __name__ == "__main__":
    unittest.main()
