#!/usr/bin/env python3
"""
Approval-gate refusal-path tests.

Verifies that no destructive action is ever taken without explicit operator
approval — the core safety invariant of the machine-hygiene skill.

Covers:
- AC4: the approval-gate refusal path
- no kill/renice/prune execution when the operator declines, is silent (EOF),
  or supplies malformed input
- the orchestrator only executes the approved subset
"""

import importlib.util
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


tables = _load("machine_hygiene_tables", "tables.py")
actions = _load("machine_hygiene_actions", "actions.py")
evaluate = _load("machine_hygiene_evaluate", "evaluate.py")


class _Recorder:
    def __init__(self):
        self.kills = []
        self.renices = []

    def kill(self, pid, sig):
        self.kills.append((pid, sig))

    def renice(self, pid, inc):
        self.renices.append((pid, inc))


def _dangerous_actions():
    return [
        {"action": "kill", "target": {"name": "grep", "pids": [123]},
         "expected_impact": "free memory", "impact": "high", "risk": "low"},
        {"action": "renice", "target": {"name": "node", "pids": [55]},
         "expected_impact": "lower priority", "impact": "medium", "risk": "low"},
    ]


def _safe_ancestry(pid):
    return {"pid": pid, "name": "grep", "ppid": 1, "uid": 1000, "session": 5,
            "ancestors": [], "available": True}


class TestNoActionWithoutApproval(unittest.TestCase):
    def test_declined_executes_nothing(self):
        recorder = _Recorder()
        outcome = actions.execute_actions(
            [], _dangerous_actions(), dry_run=False, operator_uid=1000,
            kill_fn=recorder.kill, renice_fn=recorder.renice,
            ancestry_fn=_safe_ancestry,
        )
        self.assertEqual(outcome["executed"], [])
        self.assertEqual(recorder.kills, [])
        self.assertEqual(recorder.renices, [])

    def test_prompt_none_declines(self):
        approved = tables.prompt_for_approval(
            _dangerous_actions(), input_fn=lambda _: "none", output_fn=lambda _: None
        )
        self.assertEqual(approved, [])

    def test_prompt_eof_declines(self):
        def _raise(_):
            raise EOFError

        approved = tables.prompt_for_approval(
            _dangerous_actions(), input_fn=_raise, output_fn=lambda _: None
        )
        self.assertEqual(approved, [])

    def test_prompt_malformed_declines(self):
        approved = tables.prompt_for_approval(
            _dangerous_actions(), input_fn=lambda _: "kill them all now",
            output_fn=lambda _: None,
        )
        self.assertEqual(approved, [])

    def test_parse_approval_fail_closed(self):
        self.assertEqual(tables.parse_approval("maybe", 2), [])
        self.assertEqual(tables.parse_approval("", 2), [])

    def test_partial_approval_executes_only_approved(self):
        recorder = _Recorder()
        dangerous = _dangerous_actions()
        approved = [dangerous[0]]  # only the kill
        outcome = actions.execute_actions(
            approved, [dangerous[1]], dry_run=False, operator_uid=1000,
            kill_fn=recorder.kill, renice_fn=recorder.renice,
            ancestry_fn=_safe_ancestry,
        )
        self.assertEqual(recorder.kills, [(123, 15)])
        self.assertEqual(recorder.renices, [])
        self.assertEqual(outcome["declined_count"], 1)


class TestOrchestratorGate(unittest.TestCase):
    def _metrics(self):
        return {
            "load_average": {"value": 30.0, "status": "critical", "description": "l"},
            "cpu_pressure": {"value": 90.0, "status": "critical", "description": "c"},
            "memory_pressure": {"value": None, "status": "warning", "description": "n"},
            "io_pressure": {"value": None, "status": "warning", "description": "n"},
            "memory_usage": {"value": 50.0, "status": "low", "description": "m"},
            "swap_usage": {"value": 0.0, "status": "low", "description": "s"},
            "process_count": {"value": 10, "status": "low", "description": "p"},
            "thread_count": {"value": 20, "status": "low", "description": "t"},
            "rss_top10": {"value": 0, "status": "low", "description": "r"},
            "rss_breakdown": {"value": [
                {"name": "grep", "total_rss_bytes": 2 * 1024 ** 3,
                 "process_count": 1, "pids": [(123, 2 * 1024 ** 3)]},
            ], "status": "low"},
            "runnable_processes": {"value": 20, "status": "warning", "description": "run"},
            "zombie_processes": {"value": 0, "status": "low", "description": "none"},
            "stale_lock_files": {"value": 0, "status": "low", "description": "none"},
        }

    def test_orchestrator_declined_has_no_executed_actions(self):
        result = evaluate.run_workflow(
            metrics_fn=self._metrics,
            ancestry_fn=_safe_ancestry,
            input_fn=lambda _: "none",
            output_fn=lambda _: None,
            dry_run=False,
        )
        self.assertEqual(result["outcome"]["executed"], [])
        self.assertEqual(result["outcome"]["approved_count"], 0)
        self.assertTrue(result["actions"])


if __name__ == "__main__":
    unittest.main()
