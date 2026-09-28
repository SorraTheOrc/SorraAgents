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
