#!/usr/bin/env python3
"""
Integration tests for the machine-hygiene skill.

Exercises the full workflow end-to-end against synthetic host metrics and
temporary /proc-like fixtures.

Covers:
- AC1: full workflow metrics → analysis → proposal → gate → execution → after
- AC2: temporary fixtures for high CPU pressure, memory pressure, zombies
- AC5: modules import without side effects and public functions are documented
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
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


evaluate = _load("machine_hygiene_evaluate", "evaluate.py")
pa = _load("machine_hygiene_pressure_analysis", "pressure_analysis.py")
tables = _load("machine_hygiene_tables", "tables.py")
actions = _load("machine_hygiene_actions", "actions.py")
metrics = _load("machine_hygiene_metrics", "metrics.py")


def _safe_ancestry(pid):
    return {"pid": pid, "name": "grep", "ppid": 1, "uid": 1000, "session": 5,
            "systemd_unit": "session-5.scope", "ancestors": [], "available": True}


def _base_metrics():
    return {
        "load_average": {"value": 5.0, "status": "low", "description": "load"},
        "cpu_pressure": {"value": 10.0, "status": "low", "description": "cpu"},
        "memory_pressure": {"value": 10.0, "status": "low", "description": "mem"},
        "io_pressure": {"value": 10.0, "status": "low", "description": "io"},
        "memory_usage": {"value": 40.0, "status": "low", "description": "mem"},
        "swap_usage": {"value": 0.0, "status": "low", "description": "swap"},
        "process_count": {"value": 100, "status": "low", "description": "procs"},
        "thread_count": {"value": 200, "status": "low", "description": "threads"},
        "rss_top10": {"value": 0, "status": "low", "description": "rss"},
        "rss_breakdown": {"value": [], "status": "low"},
        "runnable_processes": {"value": 4, "status": "low", "description": "run"},
        "zombie_processes": {"value": 0, "status": "low", "description": "none"},
        "stale_lock_files": {"value": 0, "status": "low", "description": "none"},
    }


class TestHighCpuPressureScenario(unittest.TestCase):
    def _metrics(self):
        m = _base_metrics()
        m["cpu_pressure"] = {"value": 92.0, "status": "critical", "description": "cpu"}
        m["load_average"] = {"value": 30.0, "status": "critical", "description": "load"}
        m["runnable_processes"] = {"value": 40, "status": "warning", "description": "run"}
        m["rss_breakdown"] = {"value": [
            {"name": "grep", "total_rss_bytes": 4 * 1024 ** 3,
             "process_count": 1, "pids": [(123, 4 * 1024 ** 3)]},
        ], "status": "low"}
        return m

    def test_full_workflow(self):
        """AC1: end-to-end flow reaches action execution and after table."""
        result = evaluate.run_workflow(
            metrics_fn=self._metrics,
            ancestry_fn=_safe_ancestry,
            auto_approve=True,
            dry_run=True,
        )
        self.assertTrue(any(p["metric"] == "cpu_pressure" for p in result["pressures"]))
        self.assertTrue(result["actions"])
        self.assertIn("Before", result["before_table"])
        self.assertIn("After", result["after_table"])
        self.assertTrue(result["outcome"]["dry_run"])

    def test_runaway_grep_kill_is_proposed(self):
        result = evaluate.run_workflow(
            metrics_fn=self._metrics,
            ancestry_fn=_safe_ancestry,
            auto_approve=True,
            dry_run=True,
        )
        self.assertTrue(any(a["action"] == "kill" for a in result["actions"]))


class TestMemoryPressureScenario(unittest.TestCase):
    def _metrics(self):
        m = _base_metrics()
        m["memory_usage"] = {"value": 97.0, "status": "critical", "description": "mem"}
        m["swap_usage"] = {"value": 80.0, "status": "critical", "description": "swap"}
        m["rss_breakdown"] = {"value": [
            {"name": "node", "total_rss_bytes": 5 * 1024 ** 3,
             "process_count": 1, "pids": [(55, 5 * 1024 ** 3)]},
        ], "status": "low"}
        return m

    def test_renice_proposed_for_large_non_ephemeral(self):
        result = evaluate.run_workflow(
            metrics_fn=self._metrics,
            ancestry_fn=_safe_ancestry,
            auto_approve=True,
            dry_run=True,
        )
        self.assertTrue(any(a["action"] == "renice" for a in result["actions"]))


class TestZombieScenario(unittest.TestCase):
    def _metrics(self):
        m = _base_metrics()
        m["zombie_processes"] = {"value": 25, "status": "critical", "description": "z"}
        return m

    def test_prune_proposed_for_zombies(self):
        result = evaluate.run_workflow(
            metrics_fn=self._metrics,
            ancestry_fn=_safe_ancestry,
            auto_approve=True,
            dry_run=True,
        )
        self.assertTrue(any(a["action"] == "prune" for a in result["actions"]))


class TestTemporaryProcFixtures(unittest.TestCase):
    """AC2: temporary /proc-like fixture data."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, pid, name, ppid, uid=1000, session=7, state="S"):
        d = self.root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(
            f"Name:\t{name}\nPPid:\t{ppid}\nUid:\t{uid}\nState:\t{state}\n",
            encoding="utf-8",
        )
        (d / "stat").write_text(
            f"{pid} ({name}) {state} {ppid} {ppid} {session} 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
            encoding="utf-8",
        )
        (d / "cgroup").write_text("0::/user.slice/session-7.scope\n", encoding="utf-8")

    def test_ancestry_from_fixture(self):
        self._write(1, "systemd", 0, uid=0)
        self._write(200, "grep", 1)
        info = pa.find_ancestry(200, proc_root=str(self.root))
        self.assertEqual(info["name"], "grep")
        self.assertEqual(info["session"], 7)
        self.assertEqual(info["systemd_unit"], "session-7.scope")


class TestImportSideEffects(unittest.TestCase):
    """AC5: modules import without side effects; public functions documented."""

    def test_modules_import_quietly(self):
        for filename in ("metrics.py", "pressure_analysis.py", "tables.py",
                         "actions.py", "evaluate.py", "config.py"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _load(f"reimport_{filename.replace('.', '_')}", filename)
            self.assertEqual(buf.getvalue(), "", f"{filename} printed on import")

    def test_public_functions_have_docstrings(self):
        modules = [metrics, pa, tables, actions, evaluate]
        missing = []
        for module in modules:
            for name, obj in vars(module).items():
                if name.startswith("_"):
                    continue
                if callable(obj) and getattr(obj, "__module__", None) == module.__name__:
                    if not (obj.__doc__ or "").strip():
                        missing.append(f"{module.__name__}.{name}")
        self.assertEqual(missing, [], f"public functions missing docstrings: {missing}")

    def test_modules_have_docstrings(self):
        for module in (metrics, pa, tables, actions, evaluate):
            self.assertTrue((module.__doc__ or "").strip(), module.__name__)


if __name__ == "__main__":
    unittest.main()
