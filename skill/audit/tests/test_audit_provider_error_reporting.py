#!/usr/bin/env python3
"""Provider-error reporting tests (SA-0MU32TFKB0012ALC, R6).

Verifies that provider errors are distinguished from parse failures in debug
entries and reports, that retry provenance is retained across the retry loop
(including a provider error that succeeds on retry), and that a per-run
provider-error count is surfaced.

All tests run offline.
"""  # noqa: EXE001
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _provider_error_stream(message: str = "boom") -> str:
    return json.dumps({
        "type": "agent_end",
        "messages": [{"role": "assistant", "stopReason": "error",
                       "errorMessage": message}],
    })


def _valid_stream() -> str:
    return json.dumps({
        "type": "agent_end",
        "messages": [{"role": "assistant",
                      "content": '{"verdict": "met", "evidence": "x.py:1"}'}],
    })


class TestProviderErrorRetryProvenance:
    def test_terminal_provider_error_records_attempts(self):
        process = mock.MagicMock()
        process.communicate.return_value = (_provider_error_stream(), "")
        with mock.patch.object(
            audit_runner.subprocess, "Popen", return_value=process
        ) as popen:
            result = audit_runner._call_pi("prompt", model="m", max_retries=1)
        assert popen.call_count == 2
        assert result.get("_provider_error") is True
        assert result.get("_provider_error_attempts") == 2
        assert result.get("_provider_error_retried") is True

    def test_provider_error_succeeds_on_retry_keeps_provenance(self):
        """AC3: a provider error that succeeds on retry is distinguishable."""
        first = mock.MagicMock()
        first.communicate.return_value = (_provider_error_stream(), "")
        second = mock.MagicMock()
        second.communicate.return_value = (_valid_stream(), "")
        with mock.patch.object(
            audit_runner.subprocess, "Popen", side_effect=[first, second]
        ):
            result = audit_runner._call_pi("prompt", model="m", max_retries=1)
        assert result.get("_provider_error") is not True
        assert result.get("_provider_error_retried") is True
        assert result.get("_provider_error_attempts") == 2


class TestProviderErrorDebugEntry:
    def test_debug_entry_distinguishes_provider_from_parse(self):
        provider = {
            "verdict": "unmet", "evidence": "Pi provider error: boom.",
            "raw_stdout": "stream", "raw_stderr": "",
            "_provider_error": True, "_provider_error_message": "boom",
            "_provider_error_attempts": 2, "_provider_error_retried": True,
            "elapsed_seconds": 1.0,
        }
        with mock.patch.object(
            audit_runner, "_call_pi", return_value=provider
        ), mock.patch.object(
            audit_runner, "_write_debug_log"
        ) as write, mock.patch.object(
            audit_runner, "_default_debug_log_path",
            return_value=Path("/tmp/audit_debug_SA-X.jsonl"),
        ):
            audit_runner._call_pi_and_maybe_log("SA-X", "parent", "prompt")

        entry = write.call_args[0][1]
        assert entry["reason"] == "provider_error"
        assert entry["parse_ok"] is None
        assert entry["provider_error"] == "boom"
        assert entry["provider_error_retried"] is True
        assert entry["provider_error_attempts"] == 2

    def test_parse_failure_entry_has_no_provider_provenance(self):
        parse_fail = {
            "raw_stdout": "stream", "extracted_text": "no json here",
            "elapsed_seconds": 1.0,
        }
        with mock.patch.object(
            audit_runner, "_call_pi", return_value=parse_fail
        ), mock.patch.object(
            audit_runner, "_write_debug_log"
        ) as write, mock.patch.object(
            audit_runner, "_default_debug_log_path",
            return_value=Path("/tmp/audit_debug_SA-X.jsonl"),
        ):
            audit_runner._call_pi_and_maybe_log(
                "SA-X", "parent", "prompt", json_expected=True,
            )
        entry = write.call_args[0][1]
        assert entry["reason"] == "parse_failure"
        assert entry["provider_error_retried"] is None


class TestProviderErrorCounts:
    def test_issue_json_reports_provider_error_count(self):
        acs = [
            {"text": "AC one", "verdict": "met", "evidence": "x.py:1"},
            {"text": "AC two", "verdict": "partial",
             "evidence": "Pi provider error: boom. Phase: Phase 2 deep analysis."},
            {"text": "AC three", "verdict": "partial",
             "evidence": "Pi model output could not be parsed."},
        ]
        payload = audit_runner._build_issue_json({"id": "SA-1"}, acs, [])
        assert payload["provider_error_count"] == 1

    def test_report_surfaces_provider_errors_distinctly(self):
        acs = [
            {"text": "AC two", "verdict": "partial",
             "evidence": "Pi provider error: boom. Phase: Phase 2 deep analysis."},
        ]
        report = audit_runner._assemble_issue_report({"id": "SA-1"}, acs, [])
        assert "Provider errors: 1 acceptance criteria" in report
        assert "distinct from a JSON parse failure" in report


def _connection_error_stream() -> str:
    """A provider stream whose assistant message ends in a connection error."""
    return _provider_error_stream("Connection error.")


def _valid_array_stream() -> str:
    """A provider stream whose assistant content is a valid verdict array."""
    return json.dumps({
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": json.dumps([
            {"index": 0, "verdict": "met", "evidence": "x.py:1"},
        ])}],
    })


def _make_processes(outputs):
    """Return one mock Popen process per (stdout, stderr) tuple in *outputs*."""
    procs = []
    for stdout, stderr in outputs:
        proc = mock.MagicMock()
        proc.communicate.return_value = (stdout, stderr)
        procs.append(proc)
    return procs


def _summary_key(line, key):
    """Extract ``<key>=<value>`` from a reliability summary line."""
    for part in line.split():
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1]
    return None


class TestBoundedSlotAwareProviderRecovery:
    """AC1: a transient connection error that recovers on retry yields
    ``parse_ok > 0`` for Phase 2 and the verdict re-ask."""

    def setup_method(self):
        audit_runner._rel_reset()

    def test_phase2_connection_error_recovers_with_parse_ok(self):
        with mock.patch.object(
            audit_runner.subprocess, "Popen",
            side_effect=_make_processes([
                (_connection_error_stream(), ""),
                (_valid_array_stream(), ""),
            ]),
        ), mock.patch.object(audit_runner, "_write_debug_log"), mock.patch.object(
            audit_runner, "_default_debug_log_path",
            return_value=Path("/tmp/audit_debug_SA-X.jsonl"),
        ):
            result = audit_runner._call_pi_and_maybe_log(
                "SA-X", "phase2_deep", "prompt", json_expected=True,
                max_retries=1,
            )
        assert result.get("_provider_error") is not True
        assert result.get("_provider_error_retried") is True
        snap = audit_runner._rel_snapshot()
        assert snap["parse_ok"] == 1
        assert snap["provider_errors"] == 0

    def test_verdict_reask_connection_error_recovers(self):
        with mock.patch.object(
            audit_runner.subprocess, "Popen",
            side_effect=_make_processes([
                (_connection_error_stream(), ""),
                (_valid_array_stream(), ""),
            ]),
        ), mock.patch.object(audit_runner, "_write_debug_log"), mock.patch.object(
            audit_runner, "_default_debug_log_path",
            return_value=Path("/tmp/audit_debug_SA-X.jsonl"),
        ):
            batch = audit_runner._reask_json_array_once(
                "SA-X", "plan", "pi", None, None, "criteria",
            )
        assert batch is not None
        assert batch[0]["index"] == 0
        snap = audit_runner._rel_snapshot()
        assert snap["parse_ok"] == 1
        assert snap["verdict_reasks"] == 1


class TestProviderErrorEvidenceActionable:
    """AC2: exhausted provider errors name the provider, phase, attempts,
    elapsed seconds and remediation — never a bare 'Connection error.' line."""

    def test_exhausted_connection_error_evidence_is_actionable(self):
        with mock.patch.object(
            audit_runner.subprocess, "Popen",
            return_value=_make_processes([
                (_connection_error_stream(), ""),
            ])[0],
        ):
            result = audit_runner._call_pi(
                "prompt", model="plan", context="phase2_deep", max_retries=1,
            )
        assert result.get("_provider_error") is True
        evidence = result.get("evidence", "")
        assert "Pi provider error: Connection error." in evidence
        assert "Provider: plan" in evidence
        assert "Phase: Phase 2 deep analysis" in evidence
        assert "Attempts: 2" in evidence
        assert "Elapsed:" in evidence
        assert "Remediation:" in evidence
        assert "AUDIT_MAX_CONCURRENCY=1" in evidence
        assert "--force" in evidence

    def test_phase2_child_evidence_uses_actionable_helper(self):
        child = {
            "id": "CHILD-1",
            "title": "Child",
            "ac_results": [
                {"index": 0, "text": "AC1", "verdict": "met",
                 "evidence": "phase1"},
            ],
        }
        provider_result = {
            "verdict": "unmet",
            "evidence": "",
            "extracted_text": "",
            "_provider_error": True,
            "_provider_error_message": "Connection error.",
            "_provider_error_attempts": 2,
            "elapsed_seconds": 3.25,
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=provider_result
        ):
            _ci, updated, timeout = audit_runner._deep_analyze_child(
                0, child, "plan", "pi", None, None, mock.MagicMock(),
            )
        assert timeout is True
        evidence = updated["ac_results"][0]["evidence"]
        assert "Pi provider error: Connection error." in evidence
        assert "Provider: plan" in evidence
        assert "Phase: Phase 2 child deep analysis" in evidence
        assert "Attempts: 2" in evidence
        assert "Elapsed: 3.2s" in evidence
        assert "AUDIT_MAX_CONCURRENCY=1" in evidence


class TestSummaryPerAttemptIdentity:
    """AC3: the reliability summary carries a per-attempt identity."""

    def setup_method(self):
        audit_runner._rel_reset()

    def test_summary_carries_run_id_and_started_at(self):
        line = audit_runner.render_reliability_summary(elapsed_seconds=1.0)
        assert "run_id=" in line
        assert "started_at=" in line

    def test_run_id_changes_between_runs(self):
        first_id = _summary_key(
            audit_runner.render_reliability_summary(elapsed_seconds=1.0),
            "run_id",
        )
        audit_runner._rel_reset()
        second_id = _summary_key(
            audit_runner.render_reliability_summary(elapsed_seconds=1.0),
            "run_id",
        )
        assert first_id and second_id
        assert first_id != second_id

    def test_summary_keeps_stable_keys_before_identity(self):
        line = audit_runner.render_reliability_summary(elapsed_seconds=0.5)
        keys = [p.split("=")[0] for p in line.split(" ", 1)[1].split()]
        assert keys == list(audit_runner.RELIABILITY_SUMMARY_KEYS) + [
            "elapsed_seconds", "run_id", "started_at",
        ]
