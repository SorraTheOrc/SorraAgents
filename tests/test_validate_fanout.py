"""Tests for skill/shared/validate_fanout.py (SA-0MSAK33NN008NS95).

Validates the F7 load-validation harness:
- AC1: A typical batch of audits (6-8 items) completes with controls enabled.
- AC2/AC3: With AUDIT_MAX_CONCURRENCY=N, the peak number of concurrent pi
  subprocesses never exceeds N, and the batch completes in reasonable time
  (no throughput regression).
- AC4: The harness produces a machine-readable JSON report with before/after
  and peak metrics suitable for comparing against the F1 baseline.

Each test writes its report into pytest's own ``tmp_path`` (never a fixed
``/tmp`` path): the suite may run concurrently with other suite runs on the
same host, and a shared output path lets those runs overwrite each other's
report (SA-0MVTV8WQ00014DSK).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skill" / "shared" / "validate_fanout.py"


def _run_harness(out_path: Path, *, max_concurrency: int = 2,
                 timeout: int = 300) -> subprocess.CompletedProcess:
    """Run the harness with *max_concurrency*, writing its report to *out_path*."""
    return subprocess.run(
        [
            sys.executable, str(SCRIPT_PATH),
            "--max-concurrency", str(max_concurrency),
            "--out", str(out_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _failed_results(result: subprocess.CompletedProcess) -> list[tuple[int, int, str]]:
    """Return ``(index, exit_code, stderr_tail)`` for every failed batch audit.

    The harness reports per-audit failures inside its JSON report rather than
    on its own stderr, so a batch failure is otherwise undiagnosable from the
    assertion message (SA-0MVTV8WQ00014DSK).
    """
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [
        (index, item.get("exit_code"), item.get("stderr_tail", ""))
        for index, item in enumerate(report.get("results", []))
        if item.get("exit_code") != 0
    ]


def _assert_batch_ok(result: subprocess.CompletedProcess) -> dict:
    """Assert the harness exited 0 and parse its report, naming any offenders."""
    assert result.returncode == 0, (
        f"batch failed: {result.stderr[-2000:]!r} "
        f"offenders={_failed_results(result)}"
    )
    report = json.loads(result.stdout)
    assert report["batch"]["ok"] is True, (
        f"batch ok=False, offenders={_failed_results(result)}"
    )
    return report


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
def test_batch_completes_with_ceiling_two(tmp_path):
    """A batch of 8 audits with max_concurrency=2 completes; peak pi <= 2."""
    result = _run_harness(tmp_path / "report-2.json", max_concurrency=2)
    report = _assert_batch_ok(result)

    assert report["success"] is True
    batch = report["batch"]
    assert batch["n"] == 8
    assert batch["ok"] is True
    # Reasonable time for 8 mock-pi audits with ceiling 2 (each pi call ~0.5s,
    # audits mostly serialize on the slot): should be well under 2 minutes.
    # On a loaded shared host the wall-clock budget is dominated by unrelated
    # concurrent sessions, so scale the budget with the batch-start load
    # average (load <= 2 keeps the strict 120s bound; heavier load scales it
    # linearly, capped below the 300s subprocess timeout).
    start_load = report["before"].get("load", {}).get("1min", 0.0)
    budget = 120.0 * max(1.0, start_load / 2.0)
    assert batch["elapsed_seconds"] < budget, (
        f"batch too slow: {batch['elapsed_seconds']}s (budget {budget:.0f}s "
        f"at start load {start_load:.1f})"
    )

    peak = report["peak"]["processes"]
    # The semaphore bounds concurrent batch pi subprocesses to the ceiling.
    # `batch_pi` counts only pi processes from this batch's mock binary (total
    # `pi` also includes operator sessions already running on the host).
    assert peak.get("batch_pi", 0) <= 2, f"pi concurrency exceeded ceiling: {peak}"
    # Audit runner subprocesses may exceed the pi ceiling (they queue on the
    # slot), but the pi count — the actual fan-out source — must stay bounded.
    assert all(r["exit_code"] == 0 for r in report["results"])


@pytest.mark.smoke
def test_batch_respects_ceiling_one(tmp_path):
    """With max_concurrency=1, peak pi concurrency must be exactly bounded."""
    result = _run_harness(tmp_path / "report-1.json", max_concurrency=1)
    report = _assert_batch_ok(result)
    peak = report["peak"]["processes"]
    assert peak.get("batch_pi", 0) <= 1, f"pi concurrency exceeded ceiling 1: {peak}"


def test_report_contains_before_after_and_peak(tmp_path):
    """The JSON report has before/after snapshots and peak metrics (AC4)."""
    result = _run_harness(tmp_path / "report-meta.json", max_concurrency=2)
    report = _assert_batch_ok(result)
    for key in ("before", "after", "peak", "batch"):
        assert key in report, f"missing {key} in report"
    assert "load" in report["before"] and "processes" in report["before"]
    assert "load" in report["after"] and "processes" in report["after"]
    assert "processes" in report["peak"]


@pytest.mark.smoke
def test_batch_isolates_host_ingress_cap_and_cleans_up(tmp_path, monkeypatch):
    """The batch's result depends only on the pi ceiling, not the host ingress cap.

    Regression pin for SA-0MVTV8WQ00014DSK: an 8-audit batch exceeds the
    default host-wide ingress cap (AUDIT_MAX_HOST_AUDITS=6), so two audits
    queued at ingress and — under host load — exhausted the bounded wait and
    exited non-zero, making batch.ok false for a reason unrelated to
    AUDIT_MAX_CONCURRENCY. Forcing a fail-fast ingress cap in the ambient
    environment makes the regression deterministic instead of load-dependent,
    and isolating TMPDIR proves the harness leaves no throwaway worklog trees
    behind (root cause of the sibling EDQUOT failures, SA-0MVTSV5SO008625I).
    """
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setenv("AUDIT_MAX_HOST_AUDITS", "2")
    monkeypatch.setenv("AUDIT_HOST_LOCK_TIMEOUT", "0")
    monkeypatch.setenv("TMPDIR", str(tmp_root))

    result = _run_harness(tmp_path / "report-ingress.json")
    report = _assert_batch_ok(result)

    assert all(r["exit_code"] == 0 for r in report["results"]), (
        f"ingress cap leaked into the batch: {_failed_results(result)}"
    )
    assert not list(tmp_root.glob("f7-validate-*")), (
        "harness leaked its throwaway worklog tree under TMPDIR: "
        f"{[p.name for p in tmp_root.glob('f7-validate-*')]}"
    )
