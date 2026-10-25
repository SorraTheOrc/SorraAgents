"""Behavioural tests for the audit performance measurement harness.

Covers SA-0MVI6VET100337TW (AC1/AC2 of the parent epic SA-0MVF1NKZQ009P3ZG):
``scripts/measure_audit_performance.py`` parses ``Per-call timing:`` lines from
an audit-debug corpus (``~/.audit_debug`` by default), prints a stable
per-context table (calls, total, p50, p90, max, count-at-budget) and is
read-only.

The harness is exercised through its public API (:func:`measure`,
:func:`render_table`, :func:`main`) against a small synthetic fixture corpus
under ``tests/fixtures/audit-performance/`` whose aggregates are hand-computed
here. That keeps the tests independent of the host's live (append-only) debug
logs.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "measure_audit_performance.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "audit-performance"

# Hand-computed aggregates for the fixture corpus (see the fixture log files).
# Context: (calls, total_seconds, p50, p90, max, @600, @900, @1800)
EXPECTED_ELAPSED = {
    "parent": (6, 3300.0, 450.0, 1050.0, 1200.0, 1, 1, 0),
    "child": (4, 1290.16, 300.0, 555.11, 600.16, 1, 0, 0),
    "verdict_reask": (2, 2261.0, 1130.5, 1666.1, 1800.0, 0, 0, 1),
    "phase2_deep": (2, 690.0, 345.0, 549.0, 600.0, 1, 0, 0),
    "phase2_child": (2, 1200.0, 600.0, 840.0, 900.0, 0, 1, 0),
    "false-positive-screen": (2, 718.0, 359.0, 550.2, 598.0, 0, 0, 0),
}

# Same corpus read on the epic brief's ad hoc basis, which used the per-AC mean
# for the Phase 2 contexts (``avg_ac_elapsed_seconds``) instead of the full
# call duration.
EXPECTED_PER_AC_TOTAL_SECONDS = {
    "phase2_deep": 330.0,
    "phase2_child": 450.0,
}


def _load_module():
    """Load the harness by file location (``scripts`` is not import-safe)."""
    assert SCRIPT.exists(), f"harness missing: {SCRIPT}"
    spec = importlib.util.spec_from_file_location(
        "_measure_audit_performance", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load_module()


def _context_row(report: dict, context: str) -> dict:
    for row in report["contexts"]:
        if row["context"] == context:
            return row
    raise AssertionError(f"context missing from report: {context}")


# ---------------------------------------------------------------------------
# Aggregation behaviour
# ---------------------------------------------------------------------------


def test_per_context_aggregates_match_hand_computed_values(harness):
    """Every aggregate column is computed correctly for each context."""
    report = harness.measure(FIXTURE_ROOT)

    assert {row["context"] for row in report["contexts"]} == set(EXPECTED_ELAPSED)

    for context, expected in EXPECTED_ELAPSED.items():
        calls, total, p50, p90, maximum, at600, at900, at1800 = expected
        row = _context_row(report, context)
        assert row["calls"] == calls, context
        assert row["total_seconds"] == pytest.approx(total, abs=0.01), context
        assert row["p50_seconds"] == pytest.approx(p50, abs=0.01), context
        assert row["p90_seconds"] == pytest.approx(p90, abs=0.01), context
        assert row["max_seconds"] == pytest.approx(maximum, abs=0.01), context
        assert row["at_budget"]["600"] == at600, context
        assert row["at_budget"]["900"] == at900, context
        assert row["at_budget"]["1800"] == at1800, context


def test_context_suffixes_are_normalised(harness):
    """``child:<id>`` and ``phase2_child:<ac-index>`` collapse to base contexts."""
    report = harness.measure(FIXTURE_ROOT)
    contexts = {row["context"] for row in report["contexts"]}

    assert "child" in contexts
    assert "phase2_child" in contexts
    assert not any(":" in context for context in contexts)


def test_corpus_wide_totals_and_shares(harness):
    """Corpus totals, median, Phase 1 share and wasted share are reported."""
    report = harness.measure(FIXTURE_ROOT)

    assert report["total_calls"] == 18
    assert report["total_seconds"] == pytest.approx(9459.16, abs=0.01)
    assert report["median_seconds"] == pytest.approx(455.5, abs=0.01)
    assert report["phase1_share_pct"] == pytest.approx(48.5, abs=0.05)
    # Wasted = verdict_reask time + calls landing on a budget (600/900/1800 s).
    assert report["wasted_share_pct"] == pytest.approx(62.0, abs=0.05)


def test_shares_are_always_wall_clock_based(harness):
    """The ``--metric per-ac`` view never changes the wall-clock share figures."""
    elapsed = harness.measure(FIXTURE_ROOT)
    per_ac = harness.measure(FIXTURE_ROOT, metric="per-ac")

    assert elapsed["phase1_share_pct"] == per_ac["phase1_share_pct"]
    assert elapsed["wasted_share_pct"] == per_ac["wasted_share_pct"]


def test_per_ac_metric_reproduces_the_brief_definition(harness):
    """``metric='per-ac'`` sums ``avg_ac_elapsed_seconds`` for Phase 2 rows."""
    report = harness.measure(FIXTURE_ROOT, metric="per-ac")

    for context, expected_total in EXPECTED_PER_AC_TOTAL_SECONDS.items():
        assert _context_row(report, context)["total_seconds"] == pytest.approx(
            expected_total, abs=0.01
        ), context
    assert report["total_seconds"] == pytest.approx(8349.16, abs=0.01)
    # Corpus percentiles follow the metric, the median stays wall-clock.
    assert report["p50_seconds"] == pytest.approx(300.0, abs=0.01)
    assert report["median_seconds"] == pytest.approx(455.5, abs=0.01)


def test_at_budget_buckets_are_configurable(harness):
    """``--at-budget`` selects which budgets the count-at-budget columns use."""
    report = harness.measure(FIXTURE_ROOT, budgets=(90.0, 600.0))

    assert _context_row(report, "parent")["at_budget"] == {"90": 0, "600": 1}
    assert _context_row(report, "child")["at_budget"] == {"90": 1, "600": 1}
    assert _context_row(report, "phase2_child")["at_budget"] == {"90": 0, "600": 0}


def test_noise_lines_and_non_log_files_are_ignored(harness):
    """Only anchored ``*.log`` timing lines count (no prompt/table false hits)."""
    report = harness.measure(FIXTURE_ROOT)

    parent = _context_row(report, "parent")
    # The 9999 s record sits behind a log prefix; the 5000 s one is a .txt file.
    assert parent["max_seconds"] == pytest.approx(1200.0)
    assert report["total_calls"] == 18
    assert report["files_scanned"] == 7


def test_context_filter_restricts_the_report(harness):
    """``contexts=`` limits the report to the requested base contexts."""
    report = harness.measure(FIXTURE_ROOT, contexts=("parent",))

    assert [row["context"] for row in report["contexts"]] == ["parent"]
    assert report["total_calls"] == 6
    assert report["total_seconds"] == pytest.approx(3300.0)
    assert report["phase1_share_pct"] == pytest.approx(100.0)
    # Two parent calls landed on a budget (600 s, 900 s).
    assert report["wasted_share_pct"] == pytest.approx(45.5, abs=0.05)


def test_empty_corpus_is_reported_loudly(harness, tmp_path, capsys):
    """A corpus with no timing lines exits non-zero with actionable advice."""
    exit_code = harness.main(["--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no 'Per-call timing:' lines found" in captured.err
    assert "--root" in captured.err


# ---------------------------------------------------------------------------
# Rendering and CLI contract
# ---------------------------------------------------------------------------


def test_render_table_has_the_mandated_columns(harness):
    """The table exposes context, calls, total, p50, p90, max and at-budget."""
    table = harness.render_table(harness.measure(FIXTURE_ROOT))
    header = table.splitlines()[0]

    for column in ("Context", "Calls", "Total(h)", "p50", "p90", "max"):
        assert column in header
    for budget in (600, 900, 1800):
        assert f"@{budget}s" in header

    # One row per context plus a TOTAL row.
    assert len(table.splitlines()) >= len(EXPECTED_ELAPSED) + 2
    assert table.splitlines()[-1].startswith("TOTAL")


def test_cli_json_output(harness, capsys):
    """``--json`` emits a machine-readable report on stdout."""
    exit_code = harness.main(["--root", str(FIXTURE_ROOT), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert payload["metric"] == "elapsed"
    assert payload["total_calls"] == 18
    assert payload["budgets"] == [600.0, 900.0, 1800.0]
    assert len(payload["contexts"]) == len(EXPECTED_ELAPSED)


def test_cli_accepts_context_and_budget_options(harness, capsys):
    """``--context`` and ``--at-budget`` reach the aggregation."""
    exit_code = harness.main(
        [
            "--root",
            str(FIXTURE_ROOT),
            "--context",
            "child",
            "--at-budget",
            "90,600",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert [row["context"] for row in payload["contexts"]] == ["child"]
    assert payload["budgets"] == [90.0, 600.0]
    assert payload["contexts"][0]["at_budget"] == {"90": 1, "600": 1}


def test_cli_rejects_an_unknown_metric(harness, capsys):
    """An invalid ``--metric`` is a usage error, not a silent default."""
    with pytest.raises(SystemExit):
        harness.main(["--root", str(FIXTURE_ROOT), "--metric", "bogus"])
    assert "bogus" in capsys.readouterr().err


def test_cli_rejects_an_invalid_budget(harness, capsys):
    """A non-numeric ``--at-budget`` is a usage error with a clear message."""
    with pytest.raises(SystemExit):
        harness.main(
            ["--root", str(FIXTURE_ROOT), "--at-budget", "600,not-a-number"]
        )
    captured = capsys.readouterr().err
    assert "invalid --at-budget value" in captured
    assert "not-a-number" in captured


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_harness_is_read_only(harness, tmp_path):
    """Measuring never writes to, renames or deletes corpus files."""
    corpus = tmp_path / "corpus"
    for source in FIXTURE_ROOT.rglob("*"):
        if source.is_file():
            destination = corpus / source.relative_to(FIXTURE_ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(corpus)): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in sorted(corpus.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    harness.measure(corpus)
    harness.measure(corpus, metric="per-ac")
    harness.main(["--root", str(corpus), "--json"])

    assert snapshot() == before
