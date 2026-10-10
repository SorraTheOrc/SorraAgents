"""
Tests for machine-hygiene table rendering and the approval gate.

Covers:
- AC1: before table with values, status symbols and priority annotations
- AC2: after table rendered alongside the before state
- AC3: Unicode status symbols with ASCII fallback
- AC4: residual/legitimate load reporting
- AC5: approval gate refuses without explicit approval
"""

import importlib.util
import sys
import unittest
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, _SKILL_DIR / "scripts" / filename
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tables = _load_module("machine_hygiene_tables", "tables.py")


def _metrics():
    return {
        "load_average": {"value": 30.0, "status": "critical", "description": "load"},
        "cpu_pressure": {"value": 88.8, "status": "critical", "description": "cpu"},
        "memory_usage": {"value": 50.0, "status": "low", "description": "mem"},
        "zombie_processes": {"value": 7, "status": "warning", "description": "zombies"},
        "rss_breakdown": {"value": [
            {"name": "pytest", "total_rss_bytes": 2 * 1024 ** 3, "process_count": 1,
             "pids": [(10, 2 * 1024 ** 3)]},
            {"name": "grep", "total_rss_bytes": 3 * 1024 ** 3, "process_count": 1,
             "pids": [(11, 3 * 1024 ** 3)]},
        ], "status": "low"},
        "stale_lock_files": {"value": 0, "status": "low", "description": "none"},
    }


_ACTIONS = [
    {"action": "kill", "target": {"name": "grep", "pids": [11]},
     "expected_impact": "free 3 GB", "impact": "high", "risk": "low"},
    {"action": "renice", "target": {"name": "node", "pids": [5]},
     "expected_impact": "lower priority", "impact": "medium", "risk": "low"},
]


class TestBeforeTable(unittest.TestCase):
    def test_shows_values_symbols_and_priority(self):
        """AC1: before table has values, symbols and priority annotations."""
        out = tables.render_before_table(_metrics(), ascii_only=True)
        self.assertIn("load_average", out)
        self.assertIn("30", out)
        self.assertIn("CRIT", out)
        self.assertIn("P0", out)
        self.assertIn("WARN", out)
        self.assertIn("P1", out)

    def test_unicode_symbols(self):
        """AC3: Unicode symbols by default."""
        out = tables.render_before_table(_metrics())
        self.assertIn("🔴", out)
        self.assertIn("🟡", out)

    def test_ascii_fallback(self):
        """AC3: ASCII fallback when requested."""
        out = tables.render_before_table(_metrics(), ascii_only=True)
        self.assertNotIn("🔴", out)
        self.assertIn("OK", out)


class TestAfterTable(unittest.TestCase):
    def test_shows_before_and_after(self):
        """AC2: after table shows both before and after states."""
        before = _metrics()
        after = _metrics()
        after["cpu_pressure"] = {"value": 10.0, "status": "low", "description": "cpu"}
        out = tables.render_after_table(before, after, ascii_only=True)
        self.assertIn("After", out)
        self.assertIn("cpu_pressure", out)
        self.assertIn("-78.8", out.replace(" ", ""))  # delta 10 - 88.8

    def test_reports_residual_load(self):
        """AC4: legitimate residual load is reported."""
        out = tables.render_after_table(_metrics(), _metrics())
        self.assertIn("Residual", out)
        self.assertIn("pytest", out)

    def test_residual_load_empty(self):
        metrics = _metrics()
        metrics["rss_breakdown"] = {"value": []}
        out = tables.render_residual_load(metrics)
        self.assertIn("No legitimate residual load", out)


class TestProposedActions(unittest.TestCase):
    def test_renders_numbered_actions(self):
        out = tables.render_proposed_actions(_ACTIONS)
        self.assertIn("kill", out)
        self.assertIn("grep", out)
        self.assertIn("free 3 GB", out)
        self.assertIn("| 1", out)

    def test_no_actions(self):
        out = tables.render_proposed_actions([])
        self.assertIn("No remediation actions", out)


class TestApprovalGate(unittest.TestCase):
    def test_parse_all(self):
        self.assertEqual(tables.parse_approval("all", 3), [0, 1, 2])

    def test_parse_none_and_empty(self):
        self.assertEqual(tables.parse_approval("none", 3), [])
        self.assertEqual(tables.parse_approval("", 3), [])

    def test_parse_subset(self):
        self.assertEqual(tables.parse_approval("1,3", 3), [0, 2])

    def test_parse_malformed_fails_closed(self):
        self.assertEqual(tables.parse_approval("banana", 3), [])
        self.assertEqual(tables.parse_approval("1,oops", 3), [])

    def test_parse_out_of_range_ignored(self):
        self.assertEqual(tables.parse_approval("5", 3), [])

    def test_prompt_declined_returns_nothing(self):
        """AC5: no approval → no actions."""
        approved = tables.prompt_for_approval(
            _ACTIONS, input_fn=lambda _: "none", output_fn=lambda _: None
        )
        self.assertEqual(approved, [])

    def test_prompt_eof_returns_nothing(self):
        def _raise(_):
            raise EOFError

        approved = tables.prompt_for_approval(
            _ACTIONS, input_fn=_raise, output_fn=lambda _: None
        )
        self.assertEqual(approved, [])

    def test_prompt_approved_subset(self):
        approved = tables.prompt_for_approval(
            _ACTIONS, input_fn=lambda _: "1", output_fn=lambda _: None
        )
        self.assertEqual([a["action"] for a in approved], ["kill"])

    def test_prompt_no_actions_returns_empty(self):
        approved = tables.prompt_for_approval(
            [], input_fn=lambda _: "all", output_fn=lambda _: None
        )
        self.assertEqual(approved, [])

    def test_render_approval_gate_includes_findings(self):
        out = tables.render_approval_gate(
            _metrics(),
            [{"metric": "cpu_pressure", "status": "critical", "summary": "cpu"}],
            _ACTIONS,
        )
        self.assertIn("Before", out)
        self.assertIn("Pressure analysis", out)
        self.assertIn("Proposed actions", out)
        self.assertIn("Approval gate", out)


if __name__ == "__main__":
    unittest.main()
