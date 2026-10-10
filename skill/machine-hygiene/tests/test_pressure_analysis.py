"""
Tests for the machine-hygiene pressure analysis and remediation module.

Covers:
- AC1: analyze_pressures() identifies primary pressure sources
- AC2: find_ancestry() resolves parent/session/systemd-unit attribution
- AC3: propose_remediations() emits kill/renice/prune/pace with targets
- AC4: risk assessment (low/medium/high) from ancestry
- AC5: rank_actions() orders by impact-to-risk ratio
"""

import importlib.util
import sys
import tempfile
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


pa = _load_module("machine_hygiene_pressure_analysis", "pressure_analysis.py")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_proc(root: Path, pid: int, *, name, ppid, uid=1000,
                session=99, unit=None, cmdline=""):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(
        f"Name:\t{name}\nPid:\t{pid}\nPPid:\t{ppid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
        encoding="utf-8",
    )
    # pid (comm) state ppid pgrp session tty ...
    (d / "stat").write_text(
        f"{pid} ({name}) S {ppid} {ppid} {session} 0 -1 0 0 0 0 0 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    if unit:
        (d / "cgroup").write_text(f"0::/system.slice/{unit}\n", encoding="utf-8")
    if cmdline:
        (d / "cmdline").write_text(cmdline.replace(" ", "\x00") + "\x00", encoding="utf-8")


class TestFindAncestry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_proc(self.root, 100, name="bash", ppid=1, uid=1000, session=5)
        _write_proc(self.root, 1, name="systemd", ppid=0, uid=0)
        _write_proc(
            self.root, 123, name="grep", ppid=100, uid=1000, session=5,
            unit="session-5.scope", cmdline="grep -r foo",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_parent_session_and_unit(self):
        """AC2: parent, session and systemd unit are attributed."""
        info = pa.find_ancestry(123, proc_root=str(self.root))
        self.assertEqual(info["name"], "grep")
        self.assertEqual(info["ppid"], 100)
        self.assertEqual(info["uid"], 1000)
        self.assertEqual(info["session"], 5)
        self.assertEqual(info["systemd_unit"], "session-5.scope")
        self.assertIn("grep", info["cmdline"])

    def test_walks_ancestor_chain(self):
        info = pa.find_ancestry(123, proc_root=str(self.root))
        names = [a["name"] for a in info["ancestors"]]
        self.assertEqual(names, ["bash", "systemd"])

    def test_missing_pid_degrades_gracefully(self):
        info = pa.find_ancestry(999, proc_root=str(self.root))
        self.assertFalse(info["available"])
        self.assertIsNone(info["name"])


class TestAssessRisk(unittest.TestCase):
    def test_root_owned_is_high(self):
        ancestry = {"available": True, "uid": 0, "ancestors": [], "systemd_unit": None}
        self.assertEqual(pa.assess_risk(ancestry, terminating=True), "high")

    def test_unavailable_terminating_is_high(self):
        self.assertEqual(pa.assess_risk({"available": False}, terminating=True), "high")

    def test_interactive_terminating_is_medium(self):
        ancestry = {
            "available": True, "uid": 1000, "name": "bash",
            "systemd_unit": "session-5.scope", "ancestors": [{"pid": 1, "name": "systemd"}],
        }
        self.assertEqual(pa.assess_risk(ancestry, terminating=True), "medium")

    def test_ordinary_non_terminating_is_low(self):
        ancestry = {
            "available": True, "uid": 1000, "name": "worker",
            "systemd_unit": "ci.service", "ancestors": [{"pid": 1, "name": "systemd"}],
        }
        self.assertEqual(pa.assess_risk(ancestry, terminating=False), "low")

    def test_ordinary_terminating_is_low(self):
        ancestry = {
            "available": True, "uid": 1000, "name": "worker",
            "systemd_unit": "ci.service", "ancestors": [{"pid": 1, "name": "systemd"}],
        }
        self.assertEqual(pa.assess_risk(ancestry, terminating=True), "low")


# ---------------------------------------------------------------------------
# analyze_pressures
# ---------------------------------------------------------------------------

def _stub_ancestry(pid):
    return {
        "pid": pid, "name": "grep", "ppid": 1, "uid": 1000,
        "session": 5, "systemd_unit": "session-5.scope",
        "cmdline": "grep -r foo", "ancestors": [{"pid": 1, "name": "systemd"}],
        "available": True,
    }


def _metrics(**overrides):
    base = {
        "cpu_pressure": {"value": 90.0, "status": "critical", "description": "CPU stall"},
        "memory_usage": {"value": 50.0, "status": "low", "description": "used half"},
        "load_average": {"value": 30.0, "status": "critical", "description": "load high"},
        "rss_breakdown": {
            "value": [
                {"name": "grep", "total_rss_bytes": 2 * 1024 ** 3,
                 "process_count": 1, "pids": [(123, 2 * 1024 ** 3)]},
            ],
            "status": "low",
        },
        "zombie_processes": {"value": 0, "status": "low", "description": "none"},
        "stale_lock_files": {"value": 0, "status": "low", "description": "none"},
    }
    base.update(overrides)
    return base


class TestAnalyzePressures(unittest.TestCase):
    def test_identifies_warning_and_critical_metrics(self):
        """AC1: elevated metrics become pressures."""
        pressures = pa.analyze_pressures(_metrics(), ancestry_fn=_stub_ancestry)
        metrics = {p["metric"] for p in pressures}
        self.assertIn("cpu_pressure", metrics)
        self.assertIn("load_average", metrics)
        self.assertNotIn("memory_usage", metrics)  # low → not a pressure

    def test_attaches_contributors_with_ancestry(self):
        pressures = pa.analyze_pressures(_metrics(), ancestry_fn=_stub_ancestry)
        cpu = next(p for p in pressures if p["metric"] == "cpu_pressure")
        self.assertEqual(cpu["contributors"][0]["name"], "grep")
        self.assertEqual(cpu["contributors"][0]["ancestry"]["session"], 5)

    def test_no_pressures_when_all_low(self):
        metrics = {k: {"value": 0, "status": "low", "description": ""}
                   for k in pa._PRESSURE_METRICS}
        metrics["rss_breakdown"] = {"value": []}
        self.assertEqual(pa.analyze_pressures(metrics, ancestry_fn=_stub_ancestry), [])


# ---------------------------------------------------------------------------
# propose_remediations
# ---------------------------------------------------------------------------

class TestProposeRemediations(unittest.TestCase):
    def test_runaway_ephemeral_tool_proposes_kill(self):
        """AC3: runaway ephemeral tool → kill with target + impact."""
        actions = pa.propose_remediations(
            pa.analyze_pressures(_metrics(), ancestry_fn=_stub_ancestry)
        )
        kills = [a for a in actions if a["action"] == "kill"]
        self.assertTrue(kills)
        kill = kills[0]
        self.assertEqual(kill["target"]["pids"], [123])
        self.assertEqual(kill["risk"], "medium")  # interactive session
        self.assertTrue(kill["expected_impact"])

    def test_cpu_critical_proposes_pace(self):
        actions = pa.propose_remediations(
            pa.analyze_pressures(_metrics(), ancestry_fn=_stub_ancestry)
        )
        self.assertTrue(any(a["action"] == "pace" for a in actions))

    def test_zombies_propose_prune(self):
        metrics = _metrics(zombie_processes={"value": 7, "status": "critical"})
        actions = pa.propose_remediations(
            pa.analyze_pressures(metrics, ancestry_fn=_stub_ancestry)
        )
        self.assertTrue(any(a["action"] == "prune" for a in actions))

    def test_stale_locks_propose_prune(self):
        metrics = _metrics(stale_lock_files={"value": 3, "status": "low"})
        actions = pa.propose_remediations_from_metrics(metrics)
        self.assertTrue(any(
            a["action"] == "prune" and a["target"].get("scope") == "stale-lock-files"
            for a in actions
        ))

    def test_large_non_ephemeral_under_memory_pressure_proposes_renice(self):
        metrics = _metrics(
            memory_usage={"value": 97.0, "status": "critical"},
            rss_breakdown={"value": [
                {"name": "node", "total_rss_bytes": 3 * 1024 ** 3,
                 "process_count": 1, "pids": [(55, 3 * 1024 ** 3)]},
            ]},
        )
        actions = pa.propose_remediations(
            pa.analyze_pressures(metrics, ancestry_fn=_stub_ancestry)
        )
        self.assertTrue(any(a["action"] == "renice" for a in actions))


# ---------------------------------------------------------------------------
# rank_actions
# ---------------------------------------------------------------------------

class TestRankActions(unittest.TestCase):
    def test_ranks_by_impact_to_risk_ratio(self):
        """AC5: higher impact/risk ratio ranks first."""
        high_ratio = pa._action("kill", {"pids": [1]}, "r", "i", "high", "low")
        low_ratio = pa._action("prune", {"pids": [2]}, "r", "i", "low", "high")
        ranked = pa.rank_actions([low_ratio, high_ratio])
        self.assertEqual(ranked[0]["action"], "kill")
        self.assertEqual(ranked[1]["action"], "prune")

    def test_rank_is_deterministic_for_ties(self):
        a = pa._action("renice", {"pids": [2]}, "r", "i", "medium", "medium")
        b = pa._action("kill", {"pids": [1]}, "r", "i", "medium", "medium")
        ranked = pa.rank_actions([a, b])
        self.assertEqual([x["action"] for x in ranked], ["kill", "renice"])


if __name__ == "__main__":
    unittest.main()
