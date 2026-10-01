#!/usr/bin/env python3
"""
Tests for the machine-hygiene metrics collection module.

Covers:
- AC1: all required metrics are collected
- AC2: threshold classification (low/warning/critical)
- AC3: collect_metrics() returns a structured dict
- AC4: graceful fallback when /proc/pressure/ is unavailable
- AC5: RSS aggregation groups by process name (top 10 + PID breakdown)
"""

import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

_SKILL_DIR = Path(__file__).resolve().parents[1]


def _load_metrics_module():
    """Load scripts/metrics.py under a unique name (avoids sys.path shadowing)."""
    spec = importlib.util.spec_from_file_location(
        "machine_hygiene_metrics", _SKILL_DIR / "scripts" / "metrics.py"
    )
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["machine_hygiene_metrics"] = module
    spec.loader.exec_module(module)
    return module


metrics = _load_metrics_module()


# ---------------------------------------------------------------------------
# classify_metric
# ---------------------------------------------------------------------------

class TestClassifyMetric(unittest.TestCase):
    def test_low_value(self):
        result = metrics.classify_metric(50, {"low": 0, "warning": 70, "critical": 85})
        self.assertEqual(result["status"], "low")
        self.assertEqual(result["symbol"], "🟢")

    def test_warning_value(self):
        result = metrics.classify_metric(75, {"low": 0, "warning": 70, "critical": 85})
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["symbol"], "🟡")

    def test_critical_value(self):
        result = metrics.classify_metric(90, {"low": 0, "warning": 70, "critical": 85})
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["symbol"], "🔴")

    def test_exact_warning_threshold_is_warning(self):
        result = metrics.classify_metric(70, {"low": 0, "warning": 70, "critical": 85})
        self.assertEqual(result["status"], "warning")

    def test_exact_critical_threshold_is_critical(self):
        result = metrics.classify_metric(85, {"low": 0, "warning": 70, "critical": 85})
        self.assertEqual(result["status"], "critical")


class TestMultiplierThresholds(unittest.TestCase):
    def test_load_average_multipliers(self):
        thresholds = metrics._multiplier_thresholds(
            {"warning_multiplier": 0.7, "critical_multiplier": 1.5}, 16
        )
        self.assertAlmostEqual(thresholds["warning"], 11.2)
        self.assertAlmostEqual(thresholds["critical"], 24.0)

    def test_runnable_multipliers(self):
        thresholds = metrics._multiplier_thresholds(
            {"warning_multiplier": 2.0, "critical_multiplier": 4.0}, 8
        )
        self.assertEqual(thresholds["warning"], 16.0)
        self.assertEqual(thresholds["critical"], 32.0)


# ---------------------------------------------------------------------------
# _read_proc_pressure
# ---------------------------------------------------------------------------

class TestReadProcPressure(unittest.TestCase):
    def test_valid_pressure_file(self):
        content = "some:stuff\navg10  60.00 60.00 60.00\n"
        with mock.patch.object(
            metrics, "_read_file_lines", return_value=content.split("\n")
        ):
            self.assertEqual(metrics._read_proc_pressure("cpu"), 60.0)

    def test_missing_file_returns_none(self):
        with mock.patch.object(metrics, "_read_file_lines", return_value=None):
            self.assertIsNone(metrics._read_proc_pressure("cpu"))

    def test_no_avg_line_returns_none(self):
        with mock.patch.object(
            metrics, "_read_file_lines", return_value=["just a line\n"]
        ):
            self.assertIsNone(metrics._read_proc_pressure("cpu"))


# ---------------------------------------------------------------------------
# _get_load_average / _get_memory_info
# ---------------------------------------------------------------------------

class TestGetLoadAverage(unittest.TestCase):
    def test_valid_load(self):
        with mock.patch(
            "builtins.open", mock.mock_open(read_data="4.42 2.10 1.05 2/150 12345")
        ):
            self.assertEqual(metrics._get_load_average(), 4.42)

    def test_missing_file_returns_zero(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertEqual(metrics._get_load_average(), 0.0)


class TestGetMemoryInfo(unittest.TestCase):
    def test_parse_meminfo(self):
        content = (
            "MemTotal:       16384000 kB\n"
            "MemFree:         1024000 kB\n"
            "MemAvailable:    8192000 kB\n"
            "SwapTotal:       2048000 kB\n"
            "SwapFree:        1024000 kB\n"
        )
        with mock.patch.object(
            metrics, "_read_file_lines", return_value=content.split("\n")
        ):
            result = metrics._get_memory_info()
        self.assertEqual(result["total_mb"], 16000.0)
        self.assertEqual(result["available_mb"], 8000.0)
        self.assertEqual(result["swap_total_mb"], 2000.0)
        self.assertEqual(result["swap_free_mb"], 1000.0)

    def test_missing_file(self):
        with mock.patch.object(metrics, "_read_file_lines", return_value=None):
            self.assertEqual(metrics._get_memory_info()["total_mb"], 0)


# ---------------------------------------------------------------------------
# _get_process_counts
# ---------------------------------------------------------------------------

def _open_from_map(mapping):
    def _side_effect(path, *args, **kwargs):
        if path in mapping:
            return io.StringIO(mapping[path])
        raise FileNotFoundError(path)

    return _side_effect


class TestGetProcessCounts(unittest.TestCase):
    def test_counts_processes_and_threads(self):
        mapping = {
            "/proc/1/status": "Name: systemd\nThreads: 3\n",
            "/proc/2/status": "Name: sshd\nThreads: 2\n",
        }
        with mock.patch("builtins.open", _open_from_map(mapping)):
            with mock.patch("os.listdir", return_value=["1", "2", "notapid"]):
                proc_count, thread_count = metrics._get_process_counts()
        self.assertEqual(proc_count, 2)
        self.assertEqual(thread_count, 5)

    def test_permission_error_handled(self):
        with mock.patch("os.listdir", side_effect=PermissionError):
            self.assertEqual(metrics._get_process_counts(), (0, 0))


# ---------------------------------------------------------------------------
# _get_rss_top10
# ---------------------------------------------------------------------------

class TestGetRssTop10(unittest.TestCase):
    def test_aggregate_by_name_with_pid_breakdown(self):
        mapping = {
            "/proc/100/status": "Name: grep\nVmRSS:   2048 kB\n",
            "/proc/101/status": "Name: grep\nVmRSS:   3072 kB\n",
            "/proc/102/status": "Name: node\nVmRSS:  10240 kB\n",
        }
        with mock.patch("builtins.open", _open_from_map(mapping)):
            with mock.patch("os.listdir", return_value=["100", "101", "102"]):
                result = metrics._get_rss_top10()

        self.assertEqual([item["name"] for item in result], ["node", "grep"])
        self.assertEqual(result[0]["total_rss_bytes"], 10240 * 1024)
        self.assertEqual(result[0]["process_count"], 1)
        self.assertEqual(result[1]["total_rss_bytes"], (2048 + 3072) * 1024)
        self.assertEqual(result[1]["process_count"], 2)
        self.assertEqual(sorted(pid for pid, _ in result[1]["pids"]), [100, 101])

    def test_empty_proc_dir(self):
        with mock.patch("os.listdir", return_value=[]):
            self.assertEqual(metrics._get_rss_top10(), [])


# ---------------------------------------------------------------------------
# _detect_stale_devices / _get_runnable_count
# ---------------------------------------------------------------------------

class TestDetectStaleDevices(unittest.TestCase):
    def test_zombie_detection(self):
        mapping = {
            "/proc/100/status": "Name: defunct_proc\nState: Z (zombie)   \n",
            "/proc/101/status": "Name: healthy\nState: S (sleeping)   \n",
        }
        with mock.patch("builtins.open", _open_from_map(mapping)):
            with mock.patch("os.listdir", return_value=["100", "101"]):
                with mock.patch("os.path.isdir", return_value=False):
                    result = metrics._detect_stale_devices()
        self.assertEqual(result["zombie_count"], 1)
        self.assertEqual(result["zombie_details"], [(100, "defunct_proc")])


class TestGetRunnableCount(unittest.TestCase):
    def test_count_r_and_d_states(self):
        mapping = {
            "/proc/100/status": "Name: running\nState: R (running)   \n",
            "/proc/101/status": "Name: sleeping\nState: S (sleeping)   \n",
            "/proc/102/status": "Name: blocked\nState: D (sleeping)   \n",
        }
        with mock.patch("builtins.open", _open_from_map(mapping)):
            with mock.patch("os.listdir", return_value=["100", "101", "102"]):
                self.assertEqual(metrics._get_runnable_count(), 2)


# ---------------------------------------------------------------------------
# collect_metrics
# ---------------------------------------------------------------------------

class TestCollectMetrics(unittest.TestCase):
    REQUIRED_KEYS = [
        "load_average",
        "cpu_pressure",
        "memory_pressure",
        "io_pressure",
        "memory_usage",
        "swap_usage",
        "process_count",
        "thread_count",
        "rss_top10",
        "rss_breakdown",
        "runnable_processes",
        "zombie_processes",
        "stale_lock_files",
    ]

    def test_returns_all_required_keys(self):
        result = metrics.collect_metrics()
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, result, f"missing metric {key}")

    def test_each_metric_is_structured(self):
        result = metrics.collect_metrics()
        for key, info in result.items():
            self.assertIn("value", info, key)
            self.assertIn("status", info, key)
            self.assertIn("description", info, key)
            self.assertIn("symbol", info, key)

    def test_valid_statuses(self):
        result = metrics.collect_metrics()
        for key, info in result.items():
            self.assertIn(info["status"], {"low", "warning", "critical"}, key)

    def test_pressure_fallback_marks_warning(self):
        """AC4: unavailable /proc/pressure degrades gracefully."""
        with mock.patch.object(metrics, "_read_proc_pressure", return_value=None):
            result = metrics.collect_metrics()
        for key in ("cpu_pressure", "memory_pressure", "io_pressure"):
            self.assertIsNone(result[key]["value"])
            self.assertEqual(result[key]["status"], "warning")
            self.assertIn("unavailable", result[key]["description"])

    def test_rss_breakdown_structure(self):
        result = metrics.collect_metrics()
        for item in result["rss_breakdown"]["value"]:
            self.assertIn("name", item)
            self.assertIn("total_rss_bytes", item)
            self.assertIn("process_count", item)
            self.assertIn("pids", item)


if __name__ == "__main__":
    unittest.main()
