"""Separate-process child audit output parsing, retry and diagnostics.

Covers WL-0MU0FHSEP003PCDY: when child Phase-1 AC screens and Phase-2 deep
analysis run in separate-process mode (``child_in_main_slot=False``), each
child call spawns a ``pi -p --mode json`` subprocess whose stdout must parse
into verdicts instead of degrading every AC to ``partial`` (which then
cascades: partial child ACs revert in-review children and flip the parent's
``Ready to close`` assessment).

Tests assert:
- AC1/AC5a  well-formed child subprocess output (the expected JSON-stream
            format) parses to ``met`` verdicts through the child Phase-1
            screen path;
- AC1/AC5b  alternative stream shapes (a string-encoded assistant event)
            are recovered by the pi_utils fallback instead of falling back
            to partial;
- AC4/AC5c  an unparseable first response triggers the bounded JSON re-ask
            and a successful re-ask yields the correct verdicts;
- AC3/AC5d  after retry exhaustion the AC evidence and debug log record the
            failure phase and a truncated raw-output diagnostic without
            pretending the ACs were assessed;
- AC2       met child ACs are NOT degraded to partial (no cascade signal).
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner
from scripts.pi_utils import (
    extract_pi_text,
    extract_pi_text_with_fallback,
)


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests.

    Mirrors the other audit test modules: ``_call_pi`` acquires the real
    cross-process audit semaphore before launching the (mocked) subprocess,
    which can saturate under concurrent audit load. Replace it with a
    null-context so mocked return paths are exercised directly.
    """
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _child_with_acs(child_id: str = "CHILD-1", n_acs: int = 2) -> dict:
    """A child work-item dict with numbered acceptance criteria."""
    acs = "\n".join(f"{i}. CAC {i}: child criterion {i}" for i in range(1, n_acs + 1))
    return {
        "id": child_id,
        "title": f"Child {child_id}",
        "status": "in_progress",
        "stage": "in_review",
        "description": f"## Acceptance Criteria\n{acs}\n",
    }


def _make_runner():
    """A benign wl runner that succeeds on every call (no real wl needed)."""
    runner = mock.MagicMock()
    runner.side_effect = lambda cmd: SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"success": True}),
        stderr="",
    )
    return runner


def _met_batch(n: int) -> str:
    """A well-formed verdict batch for *n* ACs."""
    return json.dumps([
        {"index": i, "verdict": "met", "evidence": f"file.py:{i + 1}"}
        for i in range(n)
    ])


# ---------------------------------------------------------------------------
# pi_utils recovery (AC1/AC5b)
# ---------------------------------------------------------------------------

class TestExtractPiTextWithFallback:
    """The recovery pass parses alternative shapes but never invent content."""

    def test_primary_result_unchanged_when_non_empty(self):
        """The fallback returns the canonical extractor result unchanged."""
        stream = (
            '{"type": "message_update", "assistantMessageEvent": '
            '{"type": "text_end", "content": "hello world"}}'
        )
        assert extract_pi_text(stream) == "hello world"
        assert extract_pi_text_with_fallback(stream) == "hello world"

    def test_recovers_string_encoded_assistant_event(self):
        """A string-encoded assistantMessageEvent is recovered."""
        inner = {"type": "text_end", "content": _met_batch(1)}
        stream = json.dumps({
            "type": "message_update",
            "assistantMessageEvent": json.dumps(inner),
        })
        assert extract_pi_text(stream) == ""
        assert extract_pi_text_with_fallback(stream) == _met_batch(1)

    def test_recovers_assistant_message_content_list(self):
        """message_end assistant content lists yield text on both paths."""
        stream = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": _met_batch(2)}],
            },
        })
        assert extract_pi_text(stream) == _met_batch(2)
        assert extract_pi_text_with_fallback(stream) == _met_batch(2)

    def test_recovers_agent_end_last_assistant_message(self):
        """agent_end's last assistant message content is recovered."""
        stream = json.dumps({
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "prompt"},
                {"role": "assistant",
                 "content": [{"type": "text", "text": _met_batch(1)}]},
            ],
        })
        assert extract_pi_text_with_fallback(stream) == _met_batch(1)

    def test_user_prompt_echo_never_recovered(self):
        """The user prompt echo (which contains the criteria array) is never
        treated as model output."""
        stream = json.dumps({
            "type": "message_start",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": f"Criteria: {_met_batch(1)}"}],
            },
        })
        assert extract_pi_text_with_fallback(stream) == ""

    def test_session_preamble_never_recovered(self):
        """The session event's embedded preamble is skipped."""
        stream = json.dumps({
            "type": "session",
            "id": "sess-1",
            "sections": {"preamble": "You are an expert coding assistant..."},
        })
        assert extract_pi_text_with_fallback(stream) == ""

    def test_empty_and_invalid_inputs(self):
        """Empty and non-JSON inputs recover nothing."""
        assert extract_pi_text_with_fallback("") == ""
        assert extract_pi_text_with_fallback("not json at all") == ""


# ---------------------------------------------------------------------------
# _call_pi: separate-process subprocess parsing (AC1/AC5a)
# ---------------------------------------------------------------------------

class TestCallPiChildSubprocessParsing:
    """_call_pi parses child subprocess stdout into verdicts."""

    def _fake_proc(self, stdout: str, stderr: str = ""):
        proc = mock.MagicMock()
        proc.communicate.return_value = (stdout, stderr)
        proc.returncode = 0
        return proc

    def test_parseable_json_stream_yields_verdict(self):
        """A well-formed pi JSON-stream (text_end + agent_end) parses to a
        met verdict with the extracted text — no 'Unparseable' path."""
        stream = "\n".join([
            '{"type": "session", "version": 3}',
            '{"type": "agent_start"}',
            '{"type": "message_start", "message": {"role": "user", "content": ""}}',
            '{"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": ' + json.dumps(_met_batch(1)) + '}}',
            '{"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": ' + json.dumps(_met_batch(1)) + '}]}]}',
        ])
        with mock.patch.object(
            audit_runner.subprocess, "Popen", return_value=self._fake_proc(stream)
        ):
            result = audit_runner._call_pi(
                "prompt", model="test-model", issue_id="CHILD-1",
                context="child:CHILD-1", child_screen=True, enable_tools=True,
            )

        assert result["verdict"] == "met"
        assert _met_batch(1) in result.get("extracted_text", "")
        assert not result.get("_unparseable_output")

    def test_string_encoded_event_recovered_not_partial(self):
        """An alternative-shape stream (string-encoded assistant event) is
        recovered by the fallback instead of degrading to partial."""
        inner = {"type": "text_end", "content": _met_batch(1)}
        stream = json.dumps({
            "type": "message_update",
            "assistantMessageEvent": json.dumps(inner),
        })
        with mock.patch.object(
            audit_runner.subprocess, "Popen", return_value=self._fake_proc(stream)
        ):
            result = audit_runner._call_pi("prompt", model="test-model")

        assert result["verdict"] == "met"
        assert _met_batch(1) in result.get("extracted_text", "")
        assert not result.get("_unparseable_output")

    def test_genuinely_empty_stream_is_diagnostic_not_met(self):
        """A stream with no recoverable text yields the unparseable diagnostic
        (never a false 'met'), carrying a truncated head+tail of the output."""
        stream = (
            '{"type": "session", "version": 3}\n'
            '{"type": "agent_start"}\n'
            + "x" * 9000
        )
        with mock.patch.object(
            audit_runner.subprocess, "Popen", return_value=self._fake_proc(stream)
        ):
            result = audit_runner._call_pi("prompt", model="test-model")

        assert result["verdict"] == "unmet"
        assert result.get("_unparseable_output") is True
        assert result.get("_unparseable_raw_stdout")
        assert "truncated" in result["_unparseable_raw_stdout"]
        assert result.get("_unparseable_raw_stdout")[:5] == '{"typ'


# ---------------------------------------------------------------------------
# Child Phase-1 AC screen (separate-process) — parse + retry + diagnostics
# ---------------------------------------------------------------------------

class TestChildPhase1ScreenSeparateProcess:
    """_phase1_review_child_acs in separate-process mode."""

    def _phase1_result(self, ac_count: int = 2):
        """A parseable result the child phase-1 call would return."""
        return {
            "verdict": "met",
            "evidence": "",
            "extracted_text": _met_batch(ac_count),
            "raw_stdout": "",
            "raw_stderr": "",
        }

    def _screen(self, child_id: str = "CHILD-1", n_acs: int = 2, **kwargs):
        return audit_runner._phase1_review_child_acs(
            0, _child_with_acs(child_id, n_acs),
            phase1_model="test-model",
            full_model="test-model",
            pi_bin="pi",
            debug_log=None,
            timeout=None,
            runner=_make_runner(),
            script_failure_callback=lambda ctx, exc: None,
        )

    def test_parseable_stream_yields_met_verdicts(self, capsys):
        """AC1/AC5a: well-formed child output yields met verdicts and no
        'Unparseable' warning in the separate-process path."""
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=self._phase1_result()
        ):
            _ci, acs = self._screen()

        assert len(acs) == 2
        assert all(ac["verdict"] == "met" for ac in acs), acs
        err = capsys.readouterr().err
        assert "Unparseable" not in err

    def test_retry_succeeds_after_unparseable_first_response(self, capsys):
        """AC4/AC5c: an unparseable first response triggers the bounded JSON
        re-ask; a successful re-ask yields the correct met verdicts."""
        records: list[str] = []

        def _fake_call(issue_id, context, prompt, **kwargs):
            records.append(context)
            if context == "verdict_reask":
                return self._phase1_result(2)
            return {
                "verdict": "unmet",
                "evidence": "",
                "extracted_text": "",
                "raw_stdout": "no usable text",
                "raw_stderr": "",
                "_unparseable_output": True,
                "_unparseable_raw_stdout": "no usable text",
            }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ):
            _ci, acs = self._screen()

        assert "verdict_reask" in records
        assert len(acs) == 2
        assert all(ac["verdict"] == "met" for ac in acs), acs

    def test_retry_exhausted_records_diagnostic_evidence(self, capsys):
        """AC3/AC5d: after retry exhaustion, ACs are partial with evidence
        naming the failure phase and including raw output (truncated)."""
        unparseable = {
            "verdict": "unmet",
            "evidence": "",
            "extracted_text": "",
            "raw_stdout": "RAW-STREAM" * 100,
            "raw_stderr": "",
            "_unparseable_output": True,
            "_unparseable_raw_stdout": "RAW-STREAM" * 100,
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=unparseable
        ):
            _ci, acs = self._screen()

        assert len(acs) == 2
        for ac in acs:
            assert ac["verdict"] == "partial"
            assert "Phase 1 child screening" in ac["evidence"], ac["evidence"]
            assert "raw output" in ac["evidence"].lower(), ac["evidence"]
        # The 'Unparseable' warning is still surfaced on stderr.
        assert "Unparseable" in capsys.readouterr().err

    def test_retry_respects_child_screen_budget(self):
        """AC4: the child-screen re-ask forwards child_screen=True so it stays
        bounded by the short Phase-1 child budget, not the full 1800 s."""
        seen_kwargs = {}

        def _fake_call(issue_id, context, prompt, **kwargs):
            if context == "verdict_reask":
                seen_kwargs.update(kwargs)
                return self._phase1_result(2)
            return {
                "verdict": "unmet",
                "evidence": "",
                "extracted_text": "",
                "_unparseable_output": True,
                "_unparseable_raw_stdout": "x",
            }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call
        ):
            self._screen()

        assert seen_kwargs.get("child_screen") is True

    def test_met_child_acs_do_not_cascade_partial(self):
        """AC2: met child ACs stay met — nothing in the separate-process
        child path degrades a correctly-parsed verdict to partial."""
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=self._phase1_result()
        ):
            _ci, acs = self._screen()
        assert all(ac["verdict"] == "met" for ac in acs)
        assert not any("could not be parsed" in ac["evidence"] for ac in acs)


class TestChildPhase1ScreenDebugLogDiagnostics:
    """AC3: the fallback debug entry records the raw subprocess output."""

    def test_fallback_debug_log_has_raw_stdout_and_phase(self, tmp_path):
        log_path = tmp_path / "debug.jsonl"
        unparseable = {
            "verdict": "unmet",
            "evidence": "",
            "extracted_text": "",
            "raw_stdout": "RAW-UNPARSEABLE-OUTPUT",
            "raw_stderr": "",
            "_unparseable_output": True,
            "_unparseable_raw_stdout": "RAW-UNPARSEABLE-OUTPUT",
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=unparseable
        ):
            audit_runner._phase1_review_child_acs(
                0, _child_with_acs(), phase1_model="test-model",
                full_model="test-model", pi_bin="pi",
                debug_log=str(log_path), timeout=None, runner=_make_runner(),
                script_failure_callback=lambda ctx, exc: None,
            )

        assert log_path.exists()
        entry = json.loads(log_path.read_text())
        assert entry["context"] == "child_ac_fallback"
        assert entry["phase"] == "Phase 1 child screening"
        assert "RAW-UNPARSEABLE-OUTPUT" in entry.get("raw_stdout_truncated", "")
        assert entry["reason"] == "parse_failure"


# ---------------------------------------------------------------------------
# Child Phase-2 deep analysis (separate-process)
# ---------------------------------------------------------------------------

class TestChildDeepAnalysisSeparateProcess:
    """_deep_analyze_child in separate-process mode."""

    def _child(self) -> dict:
        return {
            "id": "CHILD-1",
            "title": "Child",
            "ac_results": [
                {"index": 0, "text": "CAC 1", "verdict": "met", "evidence": "p1"},
                {"index": 1, "text": "CAC 2", "verdict": "met", "evidence": "p1"},
            ],
        }

    def _parseable(self, n: int = 2) -> dict:
        return {
            "verdict": "met",
            "evidence": "",
            "extracted_text": _met_batch(n),
            "raw_stdout": "",
            "raw_stderr": "",
        }

    def _deep(self, **kwargs):
        return audit_runner._deep_analyze_child(
            0, self._child(), "test-model", "pi", None, None,
            _make_runner(),
            ac_fallback_used=None,
        )

    def test_parseable_stream_yields_met(self):
        """AC1: a parseable Phase-2 child stream preserves met verdicts."""
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=self._parseable(),
        ):
            _ci, updated, timeout = self._deep()

        assert timeout is False
        assert [ac["verdict"] for ac in updated["ac_results"]] == ["met", "met"]

    def test_retry_recovers_after_unparseable_stream(self):
        """AC4/AC5c: a parse failure triggers the bounded re-ask; a successful
        re-ask restores the met verdicts."""
        calls: list[str] = []

        def _fake(issue_id, context, prompt, **kwargs):
            calls.append(context)
            if context == "verdict_reask":
                return self._parseable(2)
            return {
                "verdict": "unmet",
                "evidence": "",
                "extracted_text": "",
                "_unparseable_output": True,
                "_unparseable_raw_stdout": "RAW-DEEP",
            }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake
        ):
            _ci, updated, timeout = self._deep()

        assert "verdict_reask" in calls
        assert timeout is False
        assert [ac["verdict"] for ac in updated["ac_results"]] == ["met", "met"]

    def test_exhausted_retry_records_phase_and_raw(self):
        """AC3/AC5d: with both attempts unparseable, partial evidence names
        the Phase-2 child deep phase and includes truncated raw output."""
        unparseable = {
            "verdict": "unmet",
            "evidence": "",
            "extracted_text": "",
            "_unparseable_output": True,
            "_unparseable_raw_stdout": "RAW-DEEP-OUTPUT",
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=unparseable
        ):
            _ci, updated, timeout = self._deep()

        assert timeout is True or all(
            ac["verdict"] == "partial" for ac in updated["ac_results"]
        )
        for ac in updated["ac_results"]:
            assert "Phase 2 child deep analysis" in ac["evidence"], ac["evidence"]
            assert "RAW-DEEP-OUTPUT" in ac["evidence"], ac["evidence"]
