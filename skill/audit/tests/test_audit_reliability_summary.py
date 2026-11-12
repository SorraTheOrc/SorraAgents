#!/usr/bin/env python3
"""Per-run audit reliability summary tests (SA-0MU32TH89000YHP9, R7).

Covers the summary key contract/order, once-per-run emission, counter
accuracy (calls/parse_ok/parse_failed/provider_errors/timeouts/
concurrency_waits), and emission on an early exit (AC1-AC4).

All tests run offline.
"""  # noqa: EXE001
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _logged(**result):
    """Call _call_pi_and_maybe_log with _call_pi + debug write mocked."""
    with mock.patch.object(
        audit_runner, "_call_pi", return_value=result
    ), mock.patch.object(audit_runner, "_write_debug_log"), mock.patch.object(
        audit_runner, "_default_debug_log_path",
        return_value=Path("/tmp/audit_debug_SA-X.jsonl"),
    ):
        audit_runner._call_pi_and_maybe_log(
            "SA-X", "parent", "prompt", json_expected=True,
        )


class TestReliabilitySummaryContract:
    def setup_method(self):
        audit_runner._rel_reset()

    def test_key_order_and_stability(self):
        audit_runner._rel_incr("calls", 3)
        audit_runner._rel_incr("provider_errors")
        line = audit_runner.render_reliability_summary(elapsed_seconds=1.5)
        assert line.startswith(audit_runner.RELIABILITY_SUMMARY_PREFIX + " ")
        keys = [p.split("=")[0] for p in line.split(" ", 1)[1].split()]
        assert keys == list(audit_runner.RELIABILITY_SUMMARY_KEYS) + [
            "elapsed_seconds"
        ]
        assert "calls=3" in line and "provider_errors=1" in line

    def test_emit_exactly_once_per_run(self, capsys):
        first = audit_runner.emit_reliability_summary()
        second = audit_runner.emit_reliability_summary()
        assert first is not None and second is None
        assert capsys.readouterr().err.count(
            audit_runner.RELIABILITY_SUMMARY_PREFIX
        ) == 1

    def test_reset_re_enables_emission(self):
        assert audit_runner.emit_reliability_summary() is not None
        audit_runner._rel_reset()
        assert audit_runner.emit_reliability_summary() is not None


class TestReliabilityCounters:
    def setup_method(self):
        audit_runner._rel_reset()

    def test_parse_ok_and_parse_failed(self):
        _logged(raw_stdout="x",
                extracted_text='[{"index": 0, "verdict": "met"}]')
        snap = audit_runner._rel_snapshot()
        assert snap["calls"] == 1 and snap["parse_ok"] == 1

        _logged(raw_stdout="x", extracted_text="narrative only")
        snap = audit_runner._rel_snapshot()
        assert snap["calls"] == 2 and snap["parse_failed"] == 1

    def test_provider_errors(self):
        _logged(raw_stdout="x", _provider_error=True,
                _provider_error_message="boom")
        assert audit_runner._rel_snapshot()["provider_errors"] == 1

    def test_timeouts_and_concurrency_waits(self):
        _logged(raw_stdout="", _timeout=True)
        _logged(raw_stdout="", _concurrency_timeout=True)
        snap = audit_runner._rel_snapshot()
        assert snap["timeouts"] == 1
        assert snap["concurrency_waits"] == 1
        assert snap["calls"] == 2

    def test_child_skips_counted(self):
        with mock.patch.object(audit_runner, "_rel_incr") as incr:
            audit_runner._record_child_budget_exceeded(None, "CHILD-1", 800, 710)
        incr.assert_called_once_with("child_skips")


class TestReliabilitySummaryEarlyExit:
    def test_emitted_when_issue_run_raises(self, capsys):
        audit_runner._rel_reset()
        args = types.SimpleNamespace(
            issue_id="SA-X", do_not_persist=True, timeout=None,
            parent_timeout=None, pi_bin="pi", model=None, phase1_model=None,
            model_source="local", json=True, debug_log=None, force=True,
            worklog_dir=None, batch_phase2=None, green_run=None,
            audit_children=False, max_child_audits=None,
            max_citations_per_ac=None, run_tests=False, no_execute=True,
            checkpoint_dir=None, no_checkpoint=False,
            child_in_main_slot=False, batch_drain=None,
        )
        with mock.patch.object(
            audit_runner, "cmd_issue", side_effect=RuntimeError("boom")
        ), pytest.raises(RuntimeError):
            audit_runner._run_issue_command(args)
        assert audit_runner.RELIABILITY_SUMMARY_PREFIX in capsys.readouterr().err


class TestPerRunIsolation:
    """AC1: two sequential runs must not bleed counters or summary values."""

    def setup_method(self):
        audit_runner._rel_reset()

    def test_two_sequential_runs_have_isolated_counters_and_summaries(
            self, capsys):
        # Run A: two parseable calls, no provider errors.
        audit_runner._rel_reset()
        _logged(raw_stdout="x",
                extracted_text='[{"index": 0, "verdict": "met"}]')
        _logged(raw_stdout="x",
                extracted_text='[{"index": 0, "verdict": "met"}]')
        snap_a = audit_runner._rel_snapshot()
        line_a = audit_runner.emit_reliability_summary(elapsed_seconds=1.0)

        # Run B: one provider error and no parseable call.
        audit_runner._rel_reset()
        _logged(raw_stdout="x", _provider_error=True,
                _provider_error_message="boom")
        snap_b = audit_runner._rel_snapshot()
        line_b = audit_runner.emit_reliability_summary(elapsed_seconds=2.0)

        # Each run's counters describe only that run (no bleed-through).
        assert snap_a["calls"] == 2
        assert snap_a["parse_ok"] == 2
        assert snap_a["provider_errors"] == 0
        assert snap_b["calls"] == 1
        assert snap_b["parse_ok"] == 0
        assert snap_b["provider_errors"] == 1

        # The emitted summaries differ and carry only their own run's values.
        assert line_a is not None and line_b is not None
        assert line_a != line_b
        assert "calls=2" in line_a and "parse_ok=2" in line_a
        assert "provider_errors=0" in line_a
        assert "calls=1" in line_b and "provider_errors=1" in line_b
        assert "parse_ok=0" in line_b


class TestSummaryReflectsOnlyCurrentAttempt:
    """The summary lists only counters incremented in the current attempt."""

    def setup_method(self):
        audit_runner._rel_reset()

    def test_summary_reflects_only_this_attempts_calls(self):
        # A parseable call (calls + parse_ok), a timeout (calls + timeouts +
        # parse_failed because nothing was parsed), a provider error
        # (calls + provider_errors) and a JSON-contract re-ask.
        _logged(raw_stdout="x",
                extracted_text='[{"index": 0, "verdict": "met"}]')
        _logged(raw_stdout="", _timeout=True)
        _logged(raw_stdout="x", _provider_error=True,
                _provider_error_message="boom")
        audit_runner._rel_incr("verdict_reasks")

        snap = audit_runner._rel_snapshot()
        assert snap == {
            "calls": 3,
            "parse_ok": 1,
            "parse_failed": 1,
            "timeouts": 1,
            "child_skips": 0,
            "provider_errors": 1,
            "concurrency_waits": 0,
            "verdict_reasks": 1,
            "child_screen_timeouts": 0,
            "parent_screen_timeouts": 0,
        }

        line = audit_runner.render_reliability_summary(elapsed_seconds=0.5)
        assert "calls=3" in line
        assert "parse_ok=1" in line
        assert "parse_failed=1" in line
        assert "timeouts=1" in line
        assert "child_skips=0" in line
        assert "provider_errors=1" in line
        assert "concurrency_waits=0" in line
        assert "verdict_reasks=1" in line
        assert "child_screen_timeouts=0" in line
        assert "parent_screen_timeouts=0" in line
