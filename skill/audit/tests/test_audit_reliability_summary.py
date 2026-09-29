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
