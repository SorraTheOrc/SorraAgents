#!/usr/bin/env python3
"""Skippable benchmark: Phase-2 deep-analysis latency with the citation cap.

Work item: LP-0MSQ32WM5000NCB7 F5 (benchmark, AC3).

Runs N real ``phase2_deep`` calls on a fixed fixture work item against the
local model (via the proxy) and asserts:

  - AC3: the median elapsed time is >= 30% below the 2026-08-12 baseline
    (median 1324 s of 1537/1324/903 -> threshold 0.70 x 1324 = 927 s);
  - AC4: the observed verdicts match the fixture's expected verdict set
    exactly (no new false "met" from the evidence-cap change);
  - AC5: per-run values, the median, and any verdict diff are printed for
    evidence capture.

Skip guards (AC1): the benchmark is ``@pytest.mark.benchmark`` AND self-skips
unless ALL of the following hold:

  - ``AUDIT_RUN_BENCHMARKS=1`` (explicit opt-in — a real N=3 run consumes
    45-75+ minutes of local-model time and must never fire accidentally);
  - the ``pi`` binary is on PATH;
  - the local proxy status endpoint is reachable;
  - the proxy reports at least one free slot (a busy proxy would stall the
    run and trip the in-process stall detector).

The fixture item is fixed (in-memory; criteria verifiable against
``skill/audit/scripts/audit_runner.py`` in the worktree) and runs against a
temporary worklog dir, so the timing metric is well-defined: median over N
real ``phase2_deep`` calls on the same input.
"""  # noqa: EXE001
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner

# 2026-08-12 Phase-2 deep-analysis baseline window (s): 1537 / 1324 / 903.
# The median (1324 s) is the comparison basis; a specific value may be
# substituted here if the operator matches one baseline to this fixture.
BASELINE_MEDIAN_S = 1324
THRESHOLD_S = 0.70 * BASELINE_MEDIAN_S  # 927.0 s — >=30% reduction
BENCHMARK_RUNS = 3

_SLOT_STATUS_URL = "http://localhost:8000/llama/local/status"
_OPT_IN_ENV = "AUDIT_RUN_BENCHMARKS"

# ---------------------------------------------------------------------------
# Fixed fixture (AC2): realistic audit-AC criteria verifiable against the
# audit runner itself in this worktree, with the expected verdict set.
# ---------------------------------------------------------------------------

_FIXTURE_ISSUE: dict = {
    "id": "BENCH-FIXTURE-1",
    "title": "Audit runner Phase 2 evidence-scope benchmark fixture",
    "description": (
        "## Goal\n"
        "Fixed fixture work item for the Phase 2 deep-analysis latency "
        "benchmark (LP-0MSQ32WM5000NCB7 F5).\n\n"
        "## Key Files\n"
        "- `skill/audit/scripts/audit_runner.py`\n"
        "- `skill/audit/SKILL.md`\n\n"
        "## Acceptance Criteria\n"
        "- AC1: the runner emits a per-call timing line to stderr.\n"
        "- AC2: Phase 2 prompts include a FILE SCOPE manifest.\n"
        "- AC3: the runner supports a --json output flag.\n"
        "- AC4: deep-analysis evidence must include a file:line reference.\n"
        "- AC5: Phase 2 uses a different model than Phase 1.\n"
    ),
}

# Initial verdicts are Phase-1-equivalent fixtures; the deep-analysis model
# confirms or corrects them. AC1-AC4 are genuinely true in audit_runner.py;
# AC5 is deliberately false (both phases share the resolved model).
_FIXTURE_ACS: list[dict] = [
    {
        "index": 0,
        "text": (
            "The audit runner emits a per-call timing line to stderr that "
            "includes the issue id, call context, and elapsed seconds."
        ),
        "verdict": "met",
        "evidence": "fixture phase-1 evidence",
    },
    {
        "index": 1,
        "text": (
            "Phase 2 deep-analysis prompts include a FILE SCOPE manifest "
            "instructing the model to read only the listed in-scope files."
        ),
        "verdict": "met",
        "evidence": "fixture phase-1 evidence",
    },
    {
        "index": 2,
        "text": (
            "The audit runner supports a --json flag that emits "
            "machine-readable JSON output."
        ),
        "verdict": "met",
        "evidence": "fixture phase-1 evidence",
    },
    {
        "index": 3,
        "text": (
            "Deep-analysis evidence must include at least one specific "
            "file:line reference."
        ),
        "verdict": "met",
        "evidence": "fixture phase-1 evidence",
    },
    {
        "index": 4,
        "text": (
            "Phase 2 deep analysis uses a different model than Phase 1 "
            "automated screening."
        ),
        "verdict": "unmet",
        "evidence": "fixture phase-1 evidence",
    },
]

# The expected deep-analysis verdict set (AC4: exact match required).
EXPECTED_VERDICTS: list[str] = ["met", "met", "met", "met", "unmet"]


def _proxy_status() -> dict | None:
    """Fetch the local proxy slot-status JSON, or None on any failure.

    The endpoint can be slow under load (2s probes occasionally time out),
    so the skip probe uses a 5s timeout.
    """
    try:
        with urllib.request.urlopen(_SLOT_STATUS_URL, timeout=5) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - skip guard: any failure -> not reachable
        return None


def _skip_reason() -> str | None:
    """Return a human-readable skip reason, or None when the benchmark can run."""
    if os.environ.get(_OPT_IN_ENV, "").strip() != "1":
        return (
            f"{_OPT_IN_ENV}=1 not set (explicit opt-in required for real-model "
            "benchmarks)"
        )
    if shutil.which("pi") is None:
        return "pi binary not found on PATH"
    status = _proxy_status()
    if status is None:
        return f"local proxy not reachable at {_SLOT_STATUS_URL}"
    free = int(status.get("available_slots", 0) or 0)
    if free < 1:
        return (
            f"local proxy has no free slots (available_slots={free}); a busy "
            "proxy would stall the run — retry when the model is idle"
        )
    return None


def _phase2_deep_elapsed(worktree_root: Path, worklog_dir: Path,
                         resolved_model: str) -> tuple[float, list[dict]]:
    """Run one real parent ``phase2_deep`` call; return (elapsed_s, verdicts).

    The citation cap is active at its default (5) via the resolver
    (``max_citations_per_ac=None``), which is the point of the benchmark.
    Elapsed time is parsed from the runner's own ``Per-call timing:`` line
    so the measured metric is exactly what operators see in production.
    """
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        updated_acs, _children, _completed = audit_runner._run_phase2_deep_analysis(
            dict(_FIXTURE_ISSUE),
            [dict(ac) for ac in _FIXTURE_ACS],
            [],
            resolved_model,
            pi_bin=shutil.which("pi") or "pi",
            timeout=1800,
            worklog_dir=str(worklog_dir),
            owning_root=worktree_root,
        )
    stderr_text = buf.getvalue()
    match = re.search(
        r"Per-call timing: .* context=phase2_deep .* elapsed_seconds=([0-9.]+)",
        stderr_text,
    )
    if match is None:
        raise AssertionError(
            "phase2_deep timing line not found in runner stderr:\n" + stderr_text[-2000:]
        )
    elapsed = float(match.group(1))
    verdicts = [ac.get("verdict", "") for ac in updated_acs]
    return elapsed, verdicts


@pytest.mark.benchmark
def test_phase2_deep_latency_citation_cap() -> None:
    """AC1-AC5: median phase2_deep latency >=30% below baseline, no false met.

    Skipped (with reason) unless explicitly opted in via
    ``AUDIT_RUN_BENCHMARKS=1`` and the local proxy is reachable with a free
    slot. On a real run, prints per-run elapsed values, the median, and any
    verdict diff for evidence capture (AC5).
    """
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(f"Benchmark skipped: {reason}")

    resolved_model = audit_runner._resolve_model_for_phase(
        audit_runner.AUDIT_PHASE, audit_runner._load_config(),
        audit_runner.DEFAULT_MODEL_SOURCE,
    )
    worktree_root = REPO_ROOT

    elapsed_list: list[float] = []
    verdict_sets: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="audit-bench-") as tmp:
        worklog_dir = Path(tmp) / ".worklog"
        worklog_dir.mkdir(parents=True)
        for run in range(BENCHMARK_RUNS):
            elapsed, verdicts = _phase2_deep_elapsed(
                worktree_root, worklog_dir, resolved_model,
            )
            elapsed_list.append(elapsed)
            verdict_sets.append(verdicts)
            print(
                f"Benchmark run {run + 1}/{BENCHMARK_RUNS}: "
                f"phase2_deep elapsed_seconds={elapsed:.1f} "
                f"verdicts={verdicts}",
                flush=True,
            )

    median = statistics.median(elapsed_list)
    print(
        f"Benchmark summary: per-run={[round(e, 1) for e in elapsed_list]} "
        f"median={median:.1f}s threshold={THRESHOLD_S:.1f}s "
        f"(baseline median {BASELINE_MEDIAN_S}s, >=30% reduction)",
        flush=True,
    )

    # AC4: no new false "met" — observed verdicts must match the expected set.
    if not all(vs == EXPECTED_VERDICTS for vs in verdict_sets):
        diffs = [
            (i + 1, expected, observed)
            for i, (expected, observed) in enumerate(zip(EXPECTED_VERDICTS, verdict_sets[-1]))
            if expected != observed
        ]
        raise AssertionError(
            "Verdict diff vs fixture expected set (AC4): "
            f"expected={EXPECTED_VERDICTS} observed={verdict_sets[-1]} "
            f"diffs={diffs}"
        )

    # AC3: median >= 30% below the 2026-08-12 baseline median.
    assert median < THRESHOLD_S, (
        f"Median phase2_deep latency {median:.1f}s is NOT >=30% below the "
        f"{BASELINE_MEDIAN_S}s baseline (threshold {THRESHOLD_S:.1f}s). "
        "Surface the measured reduction and reassess the target with the "
        "operator — do not silently loosen verdict semantics."
    )
