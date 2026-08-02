"""Tests for skill/shared/validate_fanout.py (SA-0MSAK33NN008NS95).

Validates the F7 load-validation harness:
- AC1: A typical batch of audits (6-8 items) completes with controls enabled.
- AC2/AC3: With AUDIT_MAX_CONCURRENCY=N, the peak number of concurrent pi
  subprocesses never exceeds N, and the batch completes in reasonable time
  (no throughput regression).
- AC4: The harness produces a machine-readable JSON report with before/after
  and peak metrics suitable for comparing against the F1 baseline.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skill" / "shared" / "validate_fanout.py"


def test_script_exists():
    assert SCRIPT_PATH.exists(), f"Script not found at {SCRIPT_PATH}"


def test_script_has_shebang():
    content = SCRIPT_PATH.read_text()
    assert content.startswith("#!/usr/bin/env python3")


def test_help_flag():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower() or "usage:" in result.stderr.lower()


@pytest.mark.smoke
def test_batch_completes_with_ceiling_two():
    """A batch of 8 audits with max_concurrency=2 completes; peak pi <= 2."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--max-concurrency", "2", "--out", "/tmp/f7-report-2.json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, f"batch failed: {result.stderr[-2000:]}"
    report = json.loads(result.stdout)

    assert report["success"] is True
    batch = report["batch"]
    assert batch["n"] == 8
    assert batch["ok"] is True
    # Reasonable time for 8 mock-pi audits with ceiling 2 (each pi call ~0.5s,
    # audits mostly serialize on the slot): should be well under 2 minutes.
    assert batch["elapsed_seconds"] < 120, f"batch too slow: {batch['elapsed_seconds']}s"

    peak = report["peak"]["processes"]
    # The semaphore bounds concurrent batch pi subprocesses to the ceiling.
    # `batch_pi` counts only pi processes from this batch's mock binary (total
    # `pi` also includes operator sessions already running on the host).
    assert peak.get("batch_pi", 0) <= 2, f"pi concurrency exceeded ceiling: {peak}"
    # Audit runner subprocesses may exceed the pi ceiling (they queue on the
    # slot), but the pi count — the actual fan-out source — must stay bounded.
    assert all(r["exit_code"] == 0 for r in report["results"])


@pytest.mark.smoke
def test_batch_respects_ceiling_one():
    """With max_concurrency=1, peak pi concurrency must be exactly bounded."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--max-concurrency", "1", "--out", "/tmp/f7-report-1.json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, f"batch failed: {result.stderr[-2000:]}"
    report = json.loads(result.stdout)
    peak = report["peak"]["processes"]
    assert peak.get("batch_pi", 0) <= 1, f"pi concurrency exceeded ceiling 1: {peak}"
    assert report["batch"]["ok"] is True


def test_report_contains_before_after_and_peak():
    """The JSON report has before/after snapshots and peak metrics (AC4)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--max-concurrency", "2", "--out", "/tmp/f7-report-meta.json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, f"batch failed: {result.stderr[-2000:]}"
    report = json.loads(result.stdout)
    for key in ("before", "after", "peak", "batch"):
        assert key in report, f"missing {key} in report"
    assert "load" in report["before"] and "processes" in report["before"]
    assert "load" in report["after"] and "processes" in report["after"]
    assert "processes" in report["peak"]
