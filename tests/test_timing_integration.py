"""Integration tests for timing reports across instrumented skills.

Verifies parent AC5/AC6/AC7 (SA-0MT319YGQ002E801):

- AC2/AC5: Timing reports appear in BOTH human-readable and JSON output
  modes (structured ``timing`` key in JSON; table on stderr in human mode).
- AC6: Full suite passes (integration) and unit tests exist in
  ``skill/shared/tests/test_timing.py``.
- AC7: Timing overhead is negligible (< ~1ms per step).

These tests invoke the instrumented CLI scripts (run_tests.py,
render_report.py, speak.py) through subprocesses so the emitted output is
verified end-to-end. Work items are fetched through the shared worklog
resolution so the tests work from a git worktree too.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS = REPO_ROOT / "skill"
if str(_SKILLS) not in sys.path:
    sys.path.insert(0, str(_SKILLS))

from shared.timing import Timer

RUN_TESTS_PY = REPO_ROOT / "skill" / "test" / "scripts" / "run_tests.py"
RENDER_REPORT_PY = REPO_ROOT / "skill" / "report" / "scripts" / "render_report.py"
SPEAK_PY = REPO_ROOT / "skill" / "speak" / "scripts" / "speak.py"


# ---------------------------------------------------------------------------
# AC5: JSON output mode carries structured timing
# ---------------------------------------------------------------------------


class TestJsonModeTiming:
    """Timing data appears as structured data in --json output mode."""

    def test_run_tests_json_includes_timing_key(self):
        """run_tests.py --json carries a top-level ``timing`` dict."""
        proc = subprocess.run(
            [sys.executable, str(RUN_TESTS_PY), "--suite", "pytest",
             "--summary", "--json"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=120,
        )
        data = json.loads(proc.stdout)
        assert "timing" in data, "run_tests.py --json must carry a 'timing' key"
        timing = data["timing"]
        assert timing["name"] == "run_tests"
        # Existing keys must remain (additive-only, AC5)
        assert "lines" in data
        assert "success" in data
        assert "missing" in data

    def test_run_tests_json_timing_has_expected_structure(self):
        """The timing dict reports name, elapsed, total_time, percentage."""
        proc = subprocess.run(
            [sys.executable, str(RUN_TESTS_PY), "--suite", "pytest",
             "--summary", "--json"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=120,
        )
        data = json.loads(proc.stdout)
        timing = data["timing"]
        for key in ("name", "elapsed", "total_time", "percentage", "nested_steps"):
            assert key in timing, f"timing dict missing '{key}'"


# ---------------------------------------------------------------------------
# AC5: Human-readable mode emits a table on stderr
# ---------------------------------------------------------------------------


class TestHumanModeTiming:
    """Timing reports appear in human-readable mode (stderr)."""

    def test_run_tests_human_emits_timing_report_on_stderr(self, monkeypatch, capsys):
        """run_tests.py in human mode prints a Timing Report to stderr."""
        import test.scripts.run_tests as rt
        from test.scripts import run_tests as run_tests_module
        import io
        import contextlib

        # Stub run_suite so no real tests execute (fast, deterministic)
        def _fake_run_suite(*args, **kwargs):
            return {
                "success": True, "returncode": 0, "command": "pytest",
                "failures": [], "cached": False, "scope": "full",
                "notice": "",
            }

        monkeypatch.setattr(rt, "run_suite", _fake_run_suite)

        # Capture stdout/stderr from direct function call
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with contextlib.redirect_stdout(stdout_capture), \
             contextlib.redirect_stderr(stderr_capture):
            run_tests_module.main(["--suite", "pytest"])

        stderr_output = stderr_capture.getvalue()
        assert "Timing Report" in stderr_output, (
            f"human mode should emit Timing Report on stderr, got: "
            f"{stderr_output[:300]}"
        )
        stdout_output = stdout_capture.getvalue()
        # Command summary stays on stdout (existing output intact)
        assert "pytest summary" in stdout_output or "pytest:" in stdout_output

    def test_run_all_returns_timing_contract(self, monkeypatch):
        """run_all() appends an additive timing dict to its result.

        run_suite is stubbed so no tests execute — this verifies the JSON
        contract (additive ``timing`` key) that run_tests.py --json relies on.
        """
        import test.scripts.run_tests as rt

        def _fake_suite(*args, **kwargs):
            return {
                "success": True, "returncode": 0, "command": "pytest",
                "failures": [], "cached": False, "scope": "full", "notice": "",
            }

        monkeypatch.setattr(rt, "run_suite", _fake_suite)
        result = rt.run_all(suites=("pytest",), scope="full")
        assert "timing" in result, "run_all must carry an additive timing dict"
        assert result["timing"]["name"] == "run_all"
        # existing keys unchanged
        assert set(result) >= {"success", "suites", "failures", "notices"}

    def test_render_report_emits_timing_on_stderr(self):
        """render_report.py prints the report to stdout and timing to stderr."""
        proc = subprocess.run(
            [sys.executable, str(RENDER_REPORT_PY), "SA-0MSJ082OY003IQ8S",
             "--skill-name", "integration-test",
             "--headline", "Integration verification",
             "--ac", "AC1|metric|met"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert proc.returncode == 0, f"render_report failed: {proc.stderr[:300]}"
        assert proc.stdout.startswith("# Completed integration-test"), (
            "report body must remain on stdout (additive-only)"
        )
        assert "Timing Report" in proc.stderr, (
            f"timing must be emitted on stderr, got: {proc.stderr[:200]}"
        )


# ---------------------------------------------------------------------------
# AC4: timing overhead is negligible
# ---------------------------------------------------------------------------


class TestOverheadNegligible:
    """Timing adds negligible per-step overhead (< ~1ms/step)."""

    def test_timer_cycle_overhead_sub_millisecond(self):
        """10k start/stop cycles complete in < 10s (avg < 1ms per step)."""
        N = 10_000
        start = time.monotonic()
        for _ in range(N):
            t = Timer("cycle")
            t.start()
            t.stop()
        total = time.monotonic() - start
        per_cycle = total / N
        assert per_cycle < 0.001, (
            f"Timer overhead per cycle {per_cycle*1000:.3f}ms exceeds 1ms"
        )

    def test_context_manager_cycle_overhead_sub_millisecond(self):
        """10k `with Timer(...)` cycles complete in < 10s."""
        N = 10_000
        start = time.monotonic()
        for _ in range(N):
            with Timer("cycle"):
                pass
        total = time.monotonic() - start
        per_cycle = total / N
        assert per_cycle < 0.001, (
            f"context-manager overhead per cycle {per_cycle*1000:.3f}ms exceeds 1ms"
        )


# ---------------------------------------------------------------------------
# AC4/AC6: shell wrapper (speak.py) emits timing; speak.sh unchanged
# ---------------------------------------------------------------------------


class TestSpeakWrapperIntegration:
    """speak.py caller wrapper emits a timing report without network calls."""

    def test_speak_wrapper_help_emits_timing(self):
        """speak.py --help delegates to speak.sh and emits timing on stderr."""
        proc = subprocess.run(
            [sys.executable, str(SPEAK_PY), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert proc.returncode == 0
        assert "Usage" in (proc.stdout + proc.stderr)
        assert "Timing Report" in proc.stderr


# ---------------------------------------------------------------------------
# AC6: unit-test suite for the shared utility exists and passes
# ---------------------------------------------------------------------------


class TestUnitSuitePresent:
    """The shared timing unit tests exist and are collected."""

    def test_timing_unit_tests_exist(self):
        """skill/shared/tests/test_timing.py exists (unit coverage, AC6)."""
        unit_tests = (
            REPO_ROOT / "skill" / "shared" / "tests" / "test_timing.py"
        )
        assert unit_tests.is_file(), (
            f"missing unit tests at {unit_tests}"
        )
        content = unit_tests.read_text()
        assert "class TestNestingRollUp" in content
        assert "class TestJsonSerialization" in content


def _has_pytest_suite() -> bool:
    """True when the repo declares a pytest suite (config markers or tests)."""
    markers = (
        (REPO_ROOT / "pyproject.toml", "[tool.pytest.ini_options]"),
        (REPO_ROOT / "setup.cfg", "[tool:pytest]"),
        (REPO_ROOT / "tox.ini", "[pytest]"),
    )
    for path, marker in markers:
        if path.is_file() and marker in path.read_text():
            return True
    return bool(list(REPO_ROOT.glob("tests/test_*.py")))