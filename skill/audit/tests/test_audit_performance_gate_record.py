"""Gate/verdict consistency for the committed single-item measurement record.

The ≤ 10 min single-item wall-clock regression gate lives in
``docs/dev/audit-performance-baseline.md`` (SA-0MVI6VET100337TW). Before this
test existed the gate's figures were transcribed into that document by hand,
so a documented "MET"/"NOT MET" verdict could drift away from the run it
claims to describe without anything noticing.

This module pins the record to the evidence:

* the committed run fixture (``fixtures/audit_clean_room_run/``) holds the
  ``Per-call timing:`` lines and the ``AUDIT_RELIABILITY_SUMMARY`` line of one
  real single-item audit run (SA-0MVQ6OXOA003RQFU);
* every figure is re-derived from that fixture with the committed measurement
  harness (``scripts/measure_audit_performance.py --root <fixture> --json``);
* the document's per-call table, total and gate verdict must equal the
  harness output, and the verdict must equal ``total <= 600 s``.

No figure and no verdict may be hand-copied: if the run fixture changes, the
document fails until it is re-derived.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "audit_clean_room_run"
FIXTURE_LOG = FIXTURE_DIR / "clean_room_run.log"
HARNESS = REPO_ROOT / "scripts" / "measure_audit_performance.py"
BASELINE_DOC = REPO_ROOT / "docs" / "dev" / "audit-performance-baseline.md"

#: The committed regression gate (docs/dev/audit-performance-baseline.md).
GATE_SECONDS = 600.0

#: The work item whose measurement section is asserted.
WORK_ITEM_ID = "SA-0MVQ6OXOA003RQFU"

#: Every context a single-item audit can issue. ``false-positive-screen`` is
#: only issued when the run finds ruff findings to classify, so a healthy run
#: carries a subset of these — one call per context.
KNOWN_CONTEXTS = {"false-positive-screen", "parent", "phase2_deep"}

#: Reliability counters that must be zero for the recorded run to be healthy
#: evidence (a degraded call would make the wall clock unrepresentative).
HEALTHY_ZERO_COUNTERS = (
    "parse_failed",
    "timeouts",
    "provider_errors",
    "concurrency_waits",
    "verdict_reasks",
    "child_screen_timeouts",
    "parent_screen_timeouts",
)

_ROW_RE = re.compile(
    r"^\|\s*`?(?P<context>[A-Za-z0-9_-]+)`?\s*\|"
    r"\s*(?P<calls>\d+)\s*\|"
    r"\s*(?P<elapsed>\d+(?:\.\d+)?)\s*\|"
)
_TOTAL_ROW_RE = re.compile(
    r"^\|\s*\*\*Total\*\*\s*\|"
    r"\s*\*\*(?P<calls>\d+)\*\*\s*\|"
    r"\s*\*\*(?P<elapsed>\d+(?:\.\d+)?)\*\*\s*\|"
)
_VERDICT_RE = re.compile(
    r"^Gate verdict:\s*\*\*(?P<verdict>NOT MET|MET)\*\*"
    r"\s+[—-]\s+total\s+(?P<total>\d+(?:\.\d+)?)\s*s\b"
)


def _harness_report() -> dict:
    """Run the committed harness over the fixture and return its JSON report."""
    proc = subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(FIXTURE_DIR), "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert proc.returncode == 0, (
        f"harness failed (exit {proc.returncode}):\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


def _reliability_summary() -> dict[str, int | float]:
    """Parse the fixture's single ``AUDIT_RELIABILITY_SUMMARY`` line."""
    matches = [
        line for line in FIXTURE_LOG.read_text(encoding="utf-8").splitlines()
        if line.startswith("AUDIT_RELIABILITY_SUMMARY ")
    ]
    assert len(matches) == 1, (
        f"the fixture must carry exactly one reliability summary, "
        f"found {len(matches)}"
    )

    fields: dict[str, int | float] = {}
    for token in matches[0].split()[1:]:
        name, _, raw = token.partition("=")
        fields[name] = float(raw) if name == "elapsed_seconds" else int(raw)
    return fields


def _record_section() -> str:
    """Return the baseline document's measurement section for this work item.

    The section starts at the ``##`` heading naming the work item and ends at
    the next ``##`` heading, so figures from other measurements can never be
    mistaken for this run's.
    """
    lines = BASELINE_DOC.read_text(encoding="utf-8").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("## ") and WORK_ITEM_ID in line:
            start = index
            break
    assert start is not None, (
        f"{BASELINE_DOC} has no section recording the {WORK_ITEM_ID} "
        "single-item measurement"
    )
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end])


def _documented_rows() -> dict[str, dict]:
    """Parse the documented per-call table into ``{context: {calls, elapsed}}``."""
    rows: dict[str, dict] = {}
    for line in _record_section().splitlines():
        match = _ROW_RE.match(line)
        if match is None:
            continue
        rows[match.group("context")] = {
            "calls": int(match.group("calls")),
            "elapsed": float(match.group("elapsed")),
        }
    return rows


def _documented_total() -> dict:
    """Parse the documented ``**Total**`` row."""
    for line in _record_section().splitlines():
        match = _TOTAL_ROW_RE.match(line)
        if match is not None:
            return {
                "calls": int(match.group("calls")),
                "elapsed": float(match.group("elapsed")),
            }
    raise AssertionError("the measurement record has no `**Total**` table row")


def _documented_verdict() -> tuple[str, float]:
    """Parse the documented ``Gate verdict:`` line into ``(verdict, total)``."""
    for line in _record_section().splitlines():
        match = _VERDICT_RE.match(line)
        if match is not None:
            return match.group("verdict"), float(match.group("total"))
    raise AssertionError("the measurement record has no `Gate verdict:` line")


def test_fixture_holds_one_single_item_run():
    """The committed fixture is one run's calls: known contexts, one call each."""
    report = _harness_report()

    assert report["success"] is True
    contexts = [row["context"] for row in report["contexts"]]
    assert set(contexts) <= KNOWN_CONTEXTS, (
        f"unexpected audit contexts in the fixture: {set(contexts) - KNOWN_CONTEXTS}"
    )
    assert len(contexts) == len(set(contexts)), "one row per context expected"
    assert all(row["calls"] == 1 for row in report["contexts"]), (
        "the fixture must hold exactly one call per context"
    )


def test_fixture_run_is_healthy_evidence():
    """The recorded run has no degraded call that would distort its wall clock."""
    summary = _reliability_summary()

    for counter in HEALTHY_ZERO_COUNTERS:
        assert summary[counter] == 0, (
            f"the recorded run reports {counter}={summary[counter]}; a degraded "
            "run cannot be gate evidence"
        )


def test_fixture_reliability_summary_agrees_with_parsed_calls():
    """The run's own summary agrees with the harness-derived call count/total."""
    report = _harness_report()
    summary = _reliability_summary()

    assert summary["calls"] == report["total_calls"]
    # The summary's elapsed_seconds is the run's wall clock, which contains the
    # LLM calls, so it can never be shorter than their summed duration.
    assert summary["elapsed_seconds"] >= report["elapsed_total_seconds"]


def test_documented_per_call_profile_matches_harness():
    """Every documented per-call figure is the harness-derived figure."""
    report = _harness_report()
    harness_rows = {
        row["context"]: {
            "calls": row["calls"],
            "elapsed": row["total_seconds"],
        }
        for row in report["contexts"]
    }

    assert _documented_rows() == harness_rows


def test_documented_total_matches_harness():
    """The documented total LLM wall clock is the harness-derived total."""
    report = _harness_report()

    assert _documented_total() == {
        "calls": report["total_calls"],
        "elapsed": report["elapsed_total_seconds"],
    }


def test_documented_end_to_end_is_secondary_and_metric_flagged_for_producer():
    """End-to-end stays secondary context and the gate metric is producer-flagged.

    The regression gate's headline definition is "end to end"
    (docs/dev/audit-performance-baseline.md § Regression gate) while the
    measured gate (AC1 wording) is total LLM wall clock. The record must
    explicitly flag that divergence for producer confirmation rather than
    silently redefining the metric (parent SA-0MVKXNWRS005EKBM child AC4).
    """
    text = BASELINE_DOC.read_text()

    # End-to-end wall clock is recorded as secondary context, never the gate.
    assert re.search(
        r"End-to-end wall clock \(secondary context[^)]*\):\s*\*\*236 s\*\*",
        text,
    ), "end-to-end wall clock must be recorded as secondary context"

    # The gate-metric divergence is explicitly flagged for the producer.
    assert "gate metric" in text.lower(), (
        "the record must name the gate-metric question"
    )
    assert "producer" in text.lower(), (
        "the record must flag the gate-metric question for the producer"
    )


def test_documented_verdict_is_derived_from_the_gate_and_the_total():
    """The documented verdict is ``total <= 600 s``, not a hand-written claim."""
    report = _harness_report()
    expected_total = report["elapsed_total_seconds"]
    expected_verdict = "MET" if expected_total <= GATE_SECONDS else "NOT MET"

    verdict, stated_total = _documented_verdict()

    assert stated_total == expected_total
    assert verdict == expected_verdict
