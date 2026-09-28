#!/usr/bin/env python3
"""Debug-log reason taxonomy tests (SA-0MU32TAMB007HI99).

``_call_pi_and_maybe_log`` previously labelled *every* call that carried
``raw_stdout``/``raw_stderr`` as ``parse_failure`` — and every successful call
carries ``raw_stdout``. These tests pin the corrected taxonomy:

- a successfully parsed JSON array -> ``call_trace`` with ``parse_ok: true``;
- a genuine unparseable response -> ``parse_failure`` with ``parse_ok: false``;
- a provider error -> ``provider_error`` (never ``parse_failure``);
- a returned call whose caller did not declare a JSON array expectation ->
  the neutral ``call_trace`` with ``parse_ok: null`` (never ``parse_failure``).

All tests run offline: ``_call_pi``, ``_write_debug_log`` and
``_default_debug_log_path`` are mocked.
"""  # noqa: EXE001
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _logged_entry(result: dict, **overrides) -> dict:
    """Run _call_pi_and_maybe_log with mocks and return the written entry."""
    with (
        mock.patch.object(audit_runner, "_call_pi", return_value=result),
        mock.patch.object(audit_runner, "_write_debug_log") as mock_write,
        mock.patch.object(
            audit_runner,
            "_default_debug_log_path",
            return_value=Path("/tmp/audit_debug_SA-X.jsonl"),
        ),
    ):
        audit_runner._call_pi_and_maybe_log(
            "SA-X", "parent", "prompt", **overrides
        )
    assert mock_write.called, "expected a debug entry to be written"
    return mock_write.call_args[0][1]


class TestDebugReasonTaxonomy:
    def test_successful_json_array_is_call_trace(self) -> None:
        """A parseable array must not be labelled parse_failure (AC1/AC2)."""
        entry = _logged_entry(
            {
                "raw_stdout": "{}",
                "extracted_text": '[{"index": 0, "verdict": "met"}]',
                "elapsed_seconds": 1.0,
            },
            json_expected=True,
        )
        assert entry["reason"] == "call_trace"
        assert entry["parse_ok"] is True

    def test_genuine_parse_failure_is_labelled(self) -> None:
        """A real unparseable response stays distinguishable (AC3)."""
        entry = _logged_entry(
            {
                "raw_stdout": "{}",
                "extracted_text": "I reviewed the code but emitted no JSON.",
                "elapsed_seconds": 1.0,
            },
            json_expected=True,
        )
        assert entry["reason"] == "parse_failure"
        assert entry["parse_ok"] is False

    def test_provider_error_is_not_parse_failure(self) -> None:
        """Provider errors remain provider_error (AC4)."""
        entry = _logged_entry(
            {
                "raw_stdout": "",
                "raw_stderr": "boom",
                "_provider_error": True,
                "_provider_error_message": "Connection error.",
                "elapsed_seconds": 1.0,
            },
            json_expected=True,
        )
        assert entry["reason"] == "provider_error"
        assert entry["provider_error"] == "Connection error."
        assert entry["parse_ok"] is None

    def test_default_reason_is_neutral_call_trace(self) -> None:
        """Without a JSON expectation, returning output is a neutral trace (AC2)."""
        entry = _logged_entry(
            {
                "raw_stdout": "{}",
                "extracted_text": "not json",
                "elapsed_seconds": 1.0,
            },
        )
        assert entry["reason"] == "call_trace"
        assert entry["parse_ok"] is None
