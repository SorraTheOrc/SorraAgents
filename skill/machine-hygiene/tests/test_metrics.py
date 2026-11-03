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
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
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
#
# Fixtures are verbatim kernel output captured from a PSI-enabled host
# (Ubuntu, kernel >= 4.20: cpu exposes only a ``some`` line, memory and io
# expose ``some`` + ``full``). A fabricated fixture previously blessed the
# buggy parser, so these samples must stay byte-faithful to /proc/pressure.
# ---------------------------------------------------------------------------

PSI_CPU_SOME_ONLY = (
    "some avg10=11.58 avg60=9.30 avg300=4.71 total=84206005618\n"
)
PSI_MEMORY_SOME_FULL = (
    "some avg10=2.50 avg60=1.20 avg300=0.80 total=64384303\n"
    "full avg10=1.25 avg60=0.60 avg300=0.40 total=35437059\n"
)
PSI_IO_SOME_FULL = (
    "some avg10=0.05 avg60=0.11 avg300=0.10 total=398915844\n"
    "full avg10=0.03 avg60=0.07 avg300=0.06 total=270870893\n"
)


def _with_pressure_content(content: str):
    """Patch the pressure-file reader with ``content`` (verbatim kernel text)."""
    return mock.patch.object(
        metrics, "_read_file_lines", return_value=content.splitlines(keepends=True)
    )


class TestReadProcPressure(unittest.TestCase):
    def test_cpu_some_line_is_parsed(self):
        """AC1: real cpu output (some only) yields the avg10 percentage."""
        with _with_pressure_content(PSI_CPU_SOME_ONLY):
            self.assertEqual(metrics._read_proc_pressure("cpu"), 11.58)
            self.assertEqual(metrics._read_proc_pressure("cpu", "some"), 11.58)

    def test_selector_picks_different_lines_of_the_same_sample(self):
        """AC1: ``some`` and ``full`` select different lines of one file."""
        with _with_pressure_content(PSI_MEMORY_SOME_FULL):
            self.assertEqual(metrics._read_proc_pressure("memory", "some"), 2.5)
            self.assertEqual(metrics._read_proc_pressure("memory", "full"), 1.25)

    def test_io_full_line_is_parsed(self):
        with _with_pressure_content(PSI_IO_SOME_FULL):
            self.assertEqual(metrics._read_proc_pressure("io", "full"), 0.03)

    def test_missing_file_returns_none(self):
        """AC2: missing /proc/pressure/<key> degrades to None."""
        with mock.patch.object(metrics, "_read_file_lines", return_value=None):
            self.assertIsNone(metrics._read_proc_pressure("cpu"))

    def test_empty_file_returns_none(self):
        """AC2: an empty pressure file degrades to None."""
        with _with_pressure_content(""):
            self.assertIsNone(metrics._read_proc_pressure("memory", "full"))

    def test_unparsable_avg10_returns_none(self):
        """AC2: a non-numeric avg10 value degrades to None (no crash)."""
        content = "some avg10=abc avg60=0.00 avg300=0.00 total=1\n"
        with _with_pressure_content(content):
            self.assertIsNone(metrics._read_proc_pressure("cpu"))

    def test_missing_requested_line_returns_none_without_fallback(self):
        """AC2: an absent ``full`` line is None — never a silent fallback."""
        with _with_pressure_content(PSI_CPU_SOME_ONLY):
            self.assertIsNone(metrics._read_proc_pressure("cpu", "full"))

    def test_unrelated_content_returns_none(self):
        with _with_pressure_content("just a line\n"):
            self.assertIsNone(metrics._read_proc_pressure("cpu"))

    def test_invalid_selector_raises(self):
        """A selector other than some/full is a programming error."""
        with _with_pressure_content(PSI_MEMORY_SOME_FULL), \
             self.assertRaises(ValueError):
            metrics._read_proc_pressure("memory", "avg10")


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
        with mock.patch("builtins.open", _open_from_map(mapping)), \
             mock.patch("os.listdir", return_value=["1", "2", "notapid"]):
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
        with mock.patch("builtins.open", _open_from_map(mapping)), \
             mock.patch("os.listdir", return_value=["100", "101", "102"]):
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
        with mock.patch("builtins.open", _open_from_map(mapping)), \
             mock.patch("os.listdir", return_value=["100", "101"]), \
             mock.patch("os.path.isdir", return_value=False):
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
        with mock.patch("builtins.open", _open_from_map(mapping)), \
             mock.patch("os.listdir", return_value=["100", "101", "102"]):
            self.assertEqual(metrics._get_runnable_count(), 2)


# ---------------------------------------------------------------------------
# collect_metrics
# ---------------------------------------------------------------------------

class TestCollectMetrics(unittest.TestCase):
    REQUIRED_KEYS: ClassVar[list[str]] = [
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
        """AC6: unavailable /proc/pressure degrades gracefully.

        The description names the requested line so an ``n/a`` row explains
        which PSI metric was expected.
        """
        with mock.patch.object(metrics, "_read_proc_pressure", return_value=None):
            result = metrics.collect_metrics()
        expected_line = {
            "cpu_pressure": "some avg10",
            "memory_pressure": "full avg10",
            "io_pressure": "full avg10",
        }
        for key, line in expected_line.items():
            self.assertIsNone(result[key]["value"])
            self.assertEqual(result[key]["status"], "warning")
            self.assertIn("unavailable", result[key]["description"])
            self.assertIn(line, result[key]["description"])

    def test_pressure_metrics_select_their_psi_lines(self):
        """AC3: cpu reads ``some``, memory and io read ``full``."""
        with mock.patch.object(
            metrics, "_read_proc_pressure", return_value=0.0
        ) as reader:
            metrics.collect_metrics()
        self.assertEqual(
            [call.args for call in reader.call_args_list],
            [("cpu", "some"), ("memory", "full"), ("io", "full")],
        )

    def test_pressure_values_and_descriptions_render(self):
        """AC3: parsed stall percentages render with their selected line."""
        psi = {("cpu", "some"): 12.5, ("memory", "full"): 6.0, ("io", "full"): 85.0}

        def fake_read(key, which="some"):
            return psi[(key, which)]

        with mock.patch.object(metrics, "_read_proc_pressure", side_effect=fake_read):
            result = metrics.collect_metrics()

        self.assertEqual(result["cpu_pressure"]["value"], 12.5)
        self.assertEqual(result["cpu_pressure"]["status"], "low")
        self.assertEqual(result["memory_pressure"]["value"], 6.0)
        self.assertEqual(result["memory_pressure"]["status"], "warning")
        self.assertEqual(result["io_pressure"]["value"], 85.0)
        self.assertEqual(result["io_pressure"]["status"], "critical")

        self.assertIn("some avg10", result["cpu_pressure"]["description"])
        self.assertIn("full avg10", result["memory_pressure"]["description"])
        self.assertIn("full avg10", result["io_pressure"]["description"])

    def test_memory_pressure_uses_its_own_psi_thresholds(self):
        """AC3/AC5: memory_pressure is not classified as memory_usage.

        10 % of *full* memory stalls is warning under the PSI-calibrated
        ``memory_pressure`` block (5/20) but would be low under the
        ``memory_usage`` block (80/95).
        """
        psi = {("cpu", "some"): 0.0, ("memory", "full"): 10.0, ("io", "full"): 0.0}

        def fake_read(key, which="some"):
            return psi[(key, which)]

        mem_info = {
            "total_mb": 16000.0,
            "free_mb": 2000.0,
            "available_mb": 8000.0,
            "buffers_mb": 0.0,
            "cached_mb": 0.0,
            "swap_total_mb": 0.0,
            "swap_free_mb": 0.0,
        }
        with mock.patch.object(metrics, "_read_proc_pressure", side_effect=fake_read), \
             mock.patch.object(metrics, "_get_memory_info", return_value=mem_info):
            result = metrics.collect_metrics()

        self.assertEqual(result["memory_pressure"]["status"], "warning")
        # 50 % RAM used stays low under the memory_usage block (80/95).
        self.assertEqual(result["memory_usage"]["status"], "low")

    def test_rss_breakdown_structure(self):
        result = metrics.collect_metrics()
        for item in result["rss_breakdown"]["value"]:
            self.assertIn("name", item)
            self.assertIn("total_rss_bytes", item)
            self.assertIn("process_count", item)
            self.assertIn("pids", item)


# ---------------------------------------------------------------------------
# Per-category threshold boundaries (AC1)
# ---------------------------------------------------------------------------

class TestCategoryBoundaries(unittest.TestCase):
    """Boundary classification for every configured metric category."""

    def setUp(self):
        self.cfg = metrics.load_config()

    def _classify(self, category, value):
        return metrics.classify_metric(value, self.cfg[category])["status"]

    def test_cpu_pressure_boundaries(self):
        self.assertEqual(self._classify("cpu_pressure", 69.9), "low")
        self.assertEqual(self._classify("cpu_pressure", 70), "warning")
        self.assertEqual(self._classify("cpu_pressure", 85), "critical")

    def test_memory_usage_boundaries(self):
        self.assertEqual(self._classify("memory_usage", 79.9), "low")
        self.assertEqual(self._classify("memory_usage", 80), "warning")
        self.assertEqual(self._classify("memory_usage", 95), "critical")

    def test_io_pressure_boundaries(self):
        self.assertEqual(self._classify("io_pressure", 49.9), "low")
        self.assertEqual(self._classify("io_pressure", 50), "warning")
        self.assertEqual(self._classify("io_pressure", 80), "critical")

    def test_memory_pressure_boundaries(self):
        self.assertEqual(self._classify("memory_pressure", 4.9), "low")
        self.assertEqual(self._classify("memory_pressure", 5), "warning")
        self.assertEqual(self._classify("memory_pressure", 20), "critical")

    def test_swap_usage_boundaries(self):
        self.assertEqual(self._classify("swap_usage", 29.9), "low")
        self.assertEqual(self._classify("swap_usage", 30), "warning")
        self.assertEqual(self._classify("swap_usage", 70), "critical")

    def test_zombie_processes_boundaries(self):
        self.assertEqual(self._classify("zombie_processes", 4), "low")
        self.assertEqual(self._classify("zombie_processes", 5), "warning")
        self.assertEqual(self._classify("zombie_processes", 20), "critical")

    def test_load_average_multiplier_boundaries(self):
        thresholds = metrics._multiplier_thresholds(self.cfg["load_average"], 16)
        self.assertEqual(metrics.classify_metric(11.0, thresholds)["status"], "low")
        self.assertEqual(metrics.classify_metric(11.2, thresholds)["status"], "warning")
        self.assertEqual(metrics.classify_metric(24.0, thresholds)["status"], "critical")


# ---------------------------------------------------------------------------
# Multi-agent host recalibration (AC1/AC2)
# ---------------------------------------------------------------------------

class TestProcessAndThreadThresholds(unittest.TestCase):
    """AC1/AC2: process_count and thread_count use dedicated core-derived blocks."""

    def setUp(self):
        self.cfg = metrics.load_config()

    def _thresholds(self, metric, cores=16):
        return metrics._multiplier_thresholds(self.cfg[metric], cores)

    def test_dedicated_blocks_have_expected_16_core_bounds(self):
        process = self._thresholds("process_count")
        thread = self._thresholds("thread_count")
        self.assertEqual((process["warning"], process["critical"]), (768.0, 1536.0))
        self.assertEqual((thread["warning"], thread["critical"]), (2560.0, 5120.0))

    def test_multi_agent_profile_classifies_low(self):
        """AC2: 560 processes / 2250 threads on 16 cores are green."""
        process = self._thresholds("process_count")
        thread = self._thresholds("thread_count")
        self.assertEqual(metrics.classify_metric(560, process)["status"], "low")
        self.assertEqual(metrics.classify_metric(2250, thread)["status"], "low")

    def test_above_critical_multiplier_still_classifies_critical(self):
        """AC2: the alarm is recalibrated, not disabled."""
        process = self._thresholds("process_count")
        thread = self._thresholds("thread_count")
        self.assertEqual(
            metrics.classify_metric(16 * 96 + 1, process)["status"], "critical"
        )
        self.assertEqual(
            metrics.classify_metric(16 * 320 + 1, thread)["status"], "critical"
        )

    def test_collect_metrics_does_not_reuse_runnable_thresholds(self):
        """AC1: collect_metrics no longer passes runnable thresholds to either."""
        with mock.patch.object(metrics, "_get_core_count", return_value=16), \
             mock.patch.object(
                 metrics, "_get_process_counts", return_value=(560, 2250)
             ):
            result = metrics.collect_metrics()

        self.assertEqual(result["process_count"]["status"], "low")
        self.assertEqual(result["thread_count"]["status"], "low")
        # Under the borrowed runnable thresholds (cores*2 / cores*4) the same
        # profile would have been critical for both — proving the reuse is
        # gone rather than the numbers merely loosened.
        runnable = self._thresholds("runnable_processes")
        self.assertEqual(metrics.classify_metric(560, runnable)["status"], "critical")
        self.assertEqual(metrics.classify_metric(2250, runnable)["status"], "critical")

    def test_descriptions_report_their_own_bounds(self):
        with mock.patch.object(metrics, "_get_core_count", return_value=16), \
             mock.patch.object(
                 metrics, "_get_process_counts", return_value=(560, 2250)
             ):
            result = metrics.collect_metrics()
        self.assertIn("warn: 768", result["process_count"]["description"])
        self.assertIn("crit: 1536", result["process_count"]["description"])
        self.assertIn("warn: 2560", result["thread_count"]["description"])
        self.assertIn("crit: 5120", result["thread_count"]["description"])


# ---------------------------------------------------------------------------
# Swap/memory cross-check (AC5)
# ---------------------------------------------------------------------------

class TestSwapUsageGating(unittest.TestCase):
    """AC5: swap warns only when memory is under current pressure."""

    def setUp(self):
        self.cfg = metrics.load_config()

    def _swap(self, swap_pct, mem_pct, mem_pressure):
        return metrics._classify_swap_usage(
            swap_pct, mem_pct, mem_pressure, self.cfg
        )

    def test_swap_without_memory_pressure_is_low(self):
        """69 % swap with 35 % RAM and no stalls is historical accumulation."""
        result = self._swap(69.0, 35.0, 0.0)
        self.assertEqual(result["status"], "low")
        self.assertTrue(result["gated"])

    def test_unavailable_psi_degrades_to_memory_usage(self):
        """Graceful degradation when PSI memory data is unavailable."""
        self.assertEqual(self._swap(69.0, 35.0, None)["status"], "low")
        self.assertEqual(self._swap(69.0, 85.0, None)["status"], "warning")

    def test_both_elevated_warns(self):
        result = self._swap(45.0, 85.0, 0.0)
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["gated"])

    def test_high_memory_escalates_swap_to_critical(self):
        result = self._swap(45.0, 96.0, 0.0)
        self.assertEqual(result["status"], "critical")
        self.assertTrue(result["escalated"])

    def test_memory_pressure_psi_alone_warns(self):
        """Capacity is low but the PSI full line is elevated → swap warns."""
        result = self._swap(45.0, 50.0, 10.0)
        self.assertEqual(result["status"], "warning")

    def test_low_swap_stays_low_even_under_memory_pressure(self):
        self.assertEqual(self._swap(10.0, 90.0, 10.0)["status"], "low")

    def test_collect_metrics_gates_swap_on_memory(self):
        """The 2025-10-21 host profile: swap 69 %, memory 35 %, no stalls."""
        mem_info = {
            "total_mb": 16000.0, "free_mb": 1000.0, "available_mb": 10400.0,
            "buffers_mb": 0.0, "cached_mb": 0.0,
            "swap_total_mb": 8000.0, "swap_free_mb": 2480.0,
        }
        psi = {("cpu", "some"): 11.58, ("memory", "full"): 0.0, ("io", "full"): 0.0}

        def fake_read(key, which="some"):
            return psi[(key, which)]

        with mock.patch.object(metrics, "_get_memory_info", return_value=mem_info), \
             mock.patch.object(metrics, "_read_proc_pressure", side_effect=fake_read):
            result = metrics.collect_metrics()

        self.assertEqual(result["swap_usage"]["value"], 69.0)
        self.assertEqual(result["swap_usage"]["status"], "low")
        self.assertIn("historical accumulation", result["swap_usage"]["description"])


# ---------------------------------------------------------------------------
# Stale-lock validation (AC4)
# ---------------------------------------------------------------------------

class TestStaleLockValidation(unittest.TestCase):
    """AC4: /var/lock entries are validated by age, ownership and liveness."""

    OLD_SECONDS = 2 * 24 * 60 * 60

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.now = time.time()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, content="", age_seconds=0):
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        stamp = self.now - age_seconds
        os.utime(path, (stamp, stamp))
        return str(path)

    def _inspect(self, **kwargs):
        return metrics._inspect_lock_files(str(self.dir), now=self.now, **kwargs)

    def _stale_names(self, **kwargs):
        return sorted(
            d["path"].rsplit("/", 1)[-1]
            for d in metrics._find_stale_lock_files(
                str(self.dir), now=self.now, **kwargs
            )
        )

    def test_boot_time_alsa_kernel_locks_are_not_stale(self):
        for name in ("asound.state.lock", "card0.lock", "card1.lock", "card2.lock"):
            self._write(name, "", age_seconds=30 * 24 * 60 * 60)
        details = self._inspect()
        self.assertEqual(len(details), 4)
        self.assertEqual(self._stale_names(), [])
        for detail in details:
            self.assertFalse(detail["stale"])
            self.assertIn("kernel-owned", detail["reason"])

    def test_synthetic_stale_lock_is_reported(self):
        self._write("leftover.lock", "999999\n", age_seconds=self.OLD_SECONDS)
        self.assertEqual(self._stale_names(), ["leftover.lock"])
        stale = metrics._find_stale_lock_files(str(self.dir), now=self.now)
        self.assertIn("dead", stale[0]["reason"])
        self.assertEqual(stale[0]["pid"], 999999)

    def test_recent_lock_is_not_stale(self):
        self._write("fresh.lock", "999999\n", age_seconds=60)
        self.assertEqual(self._stale_names(), [])

    def test_lock_with_live_owner_is_not_stale(self):
        self._write("held.lock", f"{os.getpid()}\n", age_seconds=self.OLD_SECONDS)
        self.assertEqual(self._stale_names(), [])

    def test_root_owned_lock_is_not_stale(self):
        self._write("root.lock", "999999\n", age_seconds=self.OLD_SECONDS)
        fake = SimpleNamespace(st_uid=0, st_mtime=self.now - self.OLD_SECONDS)
        details = self._inspect(stat_fn=lambda _p: fake)
        self.assertEqual(len(details), 1)
        self.assertFalse(details[0]["stale"])
        self.assertIn("root-owned", details[0]["reason"])

    def test_old_lock_without_pid_is_stale(self):
        self._write("anonymous.lock", "", age_seconds=self.OLD_SECONDS)
        stale = metrics._find_stale_lock_files(str(self.dir), now=self.now)
        self.assertEqual(len(stale), 1)
        self.assertIn("no owning PID", stale[0]["reason"])

    def test_missing_lock_dir_is_not_fatal(self):
        missing = str(self.dir / "does-not-exist")
        self.assertEqual(metrics._find_stale_lock_files(missing), [])

    def test_detect_stale_devices_returns_only_stale_locks(self):
        for name in ("asound.state.lock", "card0.lock", "card1.lock", "card2.lock"):
            self._write(name, "", age_seconds=30 * 24 * 60 * 60)
        self._write("leftover.lock", "", age_seconds=self.OLD_SECONDS)
        # Scan the fixture lock dir instead of the real /var/lock.
        result = metrics._detect_stale_devices(lock_dir=str(self.dir))
        self.assertEqual(
            [p.rsplit("/", 1)[-1] for p in result["lock_files"]],
            ["leftover.lock"],
        )
        self.assertEqual(result["lock_files_inspected"], 5)


if __name__ == "__main__":
    unittest.main()
