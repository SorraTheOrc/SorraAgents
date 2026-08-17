"""Tests: false-positive screen contract (T1 — SA-0MST01NPD007MYG4).

Defines the model-judged false-positive screen contract that F1 will
implement: a single batched Pi call classifies each ruff finding as
``genuine`` / ``confident-false-positive`` / ``uncertain`` with a written
justification, with caution-first fallbacks on any failure.

Coverage per T1 ACs:
  1. Batched Pi call parses into per-finding classifications; a finding
     missing from the batch response falls back to ``uncertain``, never
     ``confident-false-positive``.
  2. Unparseable/provider-error screen output (Pi model failure, timeout,
     concurrency-limit marker) degrades to ``uncertain`` for ALL findings —
     no remediation can be triggered from a failed screen.
  3. The screen is skipped entirely (zero Pi calls) when the code-quality
     scan yields no ruff findings; non-ruff findings (eslint, etc.) are
     never sent to the screen.
  4. Justifications are recorded per finding and appear in the audit report
     output for all three classifications.
  5. Medium/low confident-false-positive findings are classified and
     reported but never marked remediable (remediation is blocking-severity
     only — see F2/T2).
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from skill.audit.scripts import audit_runner
from skill.audit.tests.wl_helpers import stateful_wl_side_effect


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests.

    Same pattern as ``test_audit_runner_phase1.py`` (SA-0MSCDC4750019G9Y):
    ``_call_pi`` acquires the real cross-process audit semaphore; under load
    it can saturate and make these mocked-path tests flaky.
    """
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


def _finding(severity: str, code: str = "X", linter: str = "ruff",
             file: str = "src/bad.py", line: int = 1) -> dict:
    """Minimal code-quality finding dict (matches linter_runner schema)."""
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "message": f"{code} message",
        "linter": linter,
        "code": code,
    }


def _screen_entry(index: int, classification: str, justification: str = "",
                  **overrides) -> dict:
    entry = {
        "index": index,
        "classification": classification,
        "justification": justification,
    }
    entry.update(overrides)
    return entry


def _ok_result(text: str) -> dict:
    """A healthy _call_pi result carrying *text* as extracted output."""
    return {"extracted_text": text}


# ===========================================================================
# AC1 — batched parse into per-finding classifications
# ===========================================================================

class TestParseBatchClassifications:
    """AC1: batched Pi response parses into per-finding classifications."""

    def test_batch_parses_all_three_classifications(self):
        findings = [
            _finding("high", code="F841"),
            _finding("critical", code="E402"),
            _finding("medium", code="W605"),
        ]
        raw = json.dumps([
            {"index": 0, "classification": "genuine", "justification": "real defect"},
            {"index": 1, "classification": "confident-false-positive",
             "justification": "rule misfires for this file"},
            {"index": 2, "classification": "uncertain", "justification": "cannot tell"},
        ])
        entries, failed = audit_runner._parse_fp_screen_response(raw, findings)
        assert failed is False
        assert [e["classification"] for e in entries] == [
            "genuine", "confident-false-positive", "uncertain",
        ]
        assert entries[0]["justification"] == "real defect"
        assert entries[1]["justification"] == "rule misfires for this file"
        assert entries[2]["justification"] == "cannot tell"

    def test_batch_positional_fallback_when_indexes_missing(self):
        """A batch without explicit 'index' keys maps positionally."""
        findings = [
            _finding("high", code="F841"),
            _finding("high", code="E402"),
        ]
        raw = json.dumps([
            {"classification": "genuine", "justification": "a"},
            {"classification": "uncertain", "justification": "b"},
        ])
        entries, failed = audit_runner._parse_fp_screen_response(raw, findings)
        assert failed is False
        assert [e["classification"] for e in entries] == ["genuine", "uncertain"]

    def test_missing_finding_defaults_uncertain_never_confident_false_positive(self):
        """AC1: a finding absent from the batch → uncertain (caution-first)."""
        findings = [
            _finding("high", code="F841"),
            _finding("high", code="E402"),
            _finding("high", code="W605"),
        ]
        # Batch covers only findings 0 and 2 — finding 1 (E402) is missing.
        raw = json.dumps([
            {"index": 0, "classification": "confident-false-positive",
             "justification": "misfire"},
            {"index": 2, "classification": "genuine", "justification": "real"},
        ])
        entries, failed = audit_runner._parse_fp_screen_response(raw, findings)
        assert failed is False
        missing = entries[1]
        assert missing["classification"] == "uncertain"
        assert "missing from the screen response" in missing["justification"]
        # The caution-first guarantee: no entry is ever downgraded from the
        # model's classification to a confident-false-positive.
        assert all(
            e["classification"] != "confident-false-positive"
            or e["index"] == 0
            for e in entries
        )

    def test_invalid_classification_normalizes_to_uncertain(self):
        """An out-of-vocabulary classification value → uncertain."""
        findings = [_finding("high", code="F841")]
        raw = json.dumps([
            {"index": 0, "classification": "maybe", "justification": "??"},
        ])
        entries, failed = audit_runner._parse_fp_screen_response(raw, findings)
        assert failed is False
        assert entries[0]["classification"] == "uncertain"
        assert "not recognized" in entries[0]["justification"]


# ===========================================================================
# AC2 — unparseable / provider-error degradation
# ===========================================================================

class TestDegradationToUncertain:
    """AC2: any screen failure degrades EVERY finding to uncertain."""

    @pytest.mark.parametrize("raw_text", ["", "garbage not json", None])
    def test_unparseable_output_all_uncertain(self, raw_text):
        findings = [
            _finding("critical", code="F841"),
            _finding("high", code="E402"),
        ]
        entries, failed = audit_runner._parse_fp_screen_response(raw_text, findings)
        assert failed is True
        assert [e["classification"] for e in entries] == ["uncertain", "uncertain"]
        assert all(not e["remediable"] for e in entries)
        assert all(e["screen_failed"] for e in entries)
        assert all(
            "defaulted to uncertain" in e["justification"] for e in entries
        )

    @pytest.mark.parametrize("marker", ["_provider_error", "_timeout",
                                        "_concurrency_timeout"])
    def test_infra_markers_never_trigger_remediation(self, marker):
        """Provider error / timeout / concurrency markers → all uncertain,
        ac_fallback_used provenance set, zero remediable entries (T1 AC2)."""
        findings = [
            _finding("critical", code="F841"),
            _finding("high", code="E402"),
        ]
        ac_fallback_used = mock.Mock()
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "", marker: True},
        ) as call:
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )
        call.assert_called_once()
        assert [e["classification"] for e in entries] == ["uncertain", "uncertain"]
        assert all(e["screen_failed"] for e in entries)
        assert all(not e["remediable"] for e in entries)
        assert all(
            "defaulted to uncertain" in e["justification"] for e in entries
        )
        ac_fallback_used.set.assert_called()

    def test_runtime_error_degrades_all_uncertain(self):
        """A RuntimeError from the Pi call (pi binary failure) degrades all."""
        findings = [_finding("critical", code="F841")]
        ac_fallback_used = mock.Mock()
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=RuntimeError("pi missing"),
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )
        assert entries[0]["classification"] == "uncertain"
        assert entries[0]["screen_failed"] is True
        assert entries[0]["remediable"] is False
        ac_fallback_used.set.assert_called()

    def test_screen_failure_never_proposes_remediation(self):
        """No remediation proposal exists for ANY failed-screen entry."""
        entries = audit_runner._parse_fp_screen_response("", [_finding("critical")])[0]
        assert all(not e["remediable"] for e in entries)


# ===========================================================================
# AC3 — skip when no ruff findings (zero Pi calls); eslint never screened
# ===========================================================================

class TestScreenSkipped:
    """AC3: the screen is skipped entirely when there is nothing to screen."""

    def test_no_ruff_findings_zero_pi_calls(self):
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as call:
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", [], pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=mock.Mock(),
            )
        assert entries == []
        call.assert_not_called()

    def test_non_ruff_findings_never_sent_to_screen(self):
        """eslint/other linter findings are filtered out — zero Pi calls."""
        eslint_findings = [
            _finding("high", code="no-unused-vars", linter="eslint"),
            _finding("critical", code="no-undef", linter="eslint"),
        ]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as call:
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", eslint_findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=mock.Mock(),
            )
        assert entries == []
        call.assert_not_called()

    def test_mixed_linters_only_ruff_screened(self):
        """Only ruff findings are sent; eslint findings never appear."""
        findings = [
            _finding("high", code="F841", linter="ruff"),
            _finding("critical", code="no-undef", linter="eslint"),
            _finding("high", code="E402", linter="ruff"),
        ]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=_ok_result(json.dumps([
                {"index": 0, "classification": "genuine", "justification": "a"},
                {"index": 1, "classification": "uncertain", "justification": "b"},
            ])),
        ) as call:
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=mock.Mock(),
            )
        call.assert_called_once()
        # 2 ruff findings → 2 entries; the eslint finding is absent.
        assert [e["finding"]["code"] for e in entries] == ["F841", "E402"]
        assert all(e["finding"]["linter"] == "ruff" for e in entries)

    def test_cmd_issue_no_screen_call_when_no_ruff_findings(self, capsys):
        """End-to-end: cmd_issue with eslint findings never invokes the
        screen (zero Pi calls under the FP context)."""
        runner = self._make_runner(description="## Acceptance Criteria\n- AC1: thing")
        contexts = []

        def _fake_pi(issue_id, context, prompt, **kwargs):
            contexts.append(context)
            return _ok_result(json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]))

        with (
            mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                              side_effect=_fake_pi),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True,
                              "findings": [_finding("high", code="no-undef",
                                                    linter="eslint")],
                              "fixes_applied": 0},
            ),
            mock.patch(
                "skill.code_review.scripts.create_quality_epics."
                "create_epics_for_findings",
                return_value={"epic_id": None},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=runner,
            )
        assert rc == 0
        assert audit_runner.FP_SCREEN_CONTEXT not in contexts

    # -- helpers ----------------------------------------------------------

    def _make_runner(self, description: str):
        """Mock wl/git runner mirroring test_audit_runner_phase1.py."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
        return mock_runner


# ===========================================================================
# AC1 — single batched Pi call; AC4 — justifications in report
# ===========================================================================

class TestScreenBatchedCall:
    """AC1: one batched call; AC4: justifications surface in the report."""

    def test_single_batched_call_classifies_all_ruff(self):
        findings = [
            _finding("high", code="F841"),
            _finding("critical", code="E402"),
        ]
        ac_fallback_used = mock.Mock()
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=_ok_result(json.dumps([
                {"index": 0, "classification": "confident-false-positive",
                 "justification": "misfires for generated code"},
                {"index": 1, "classification": "genuine",
                 "justification": "real bug"},
            ])),
        ) as call:
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )
        # Exactly ONE batched Pi call (AC1).
        call.assert_called_once()
        assert audit_runner.FP_SCREEN_CONTEXT in str(call.call_args)
        assert [e["classification"] for e in entries] == [
            "confident-false-positive", "genuine",
        ]
        assert entries[0]["justification"] == "misfires for generated code"
        assert entries[1]["justification"] == "real bug"
        ac_fallback_used.set.assert_not_called()

    def test_report_includes_classifications_and_justifications(self):
        """AC4: classifications + justifications appear in the report for
        all three classifications."""
        findings = [
            _finding("high", code="F841"),
            _finding("critical", code="E402"),
            _finding("medium", code="W605"),
        ]
        fp_results = [
            {"index": 0, "finding": findings[0],
             "classification": "genuine", "justification": "real defect",
             "remediable": False, "screen_failed": False},
            {"index": 1, "finding": findings[1],
             "classification": "confident-false-positive",
             "justification": "rule misfires for this file",
             "remediable": True, "screen_failed": False},
            {"index": 2, "finding": findings[2],
             "classification": "uncertain",
             "justification": "cannot tell",
             "remediable": False, "screen_failed": False},
        ]
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=findings,
            fp_screen_results=fp_results,
            model="test-model",
        )
        assert "#### False-positive screen" in report
        assert "| genuine | real defect |" in report
        assert "| confident-false-positive | rule misfires for this file |" in report
        assert "| uncertain | cannot tell |" in report

    def test_json_includes_screen_classifications(self):
        """AC4: classifications + justifications surface in _build_issue_json."""
        findings = [_finding("high", code="F841")]
        fp_results = [
            {"index": 0, "finding": findings[0],
             "classification": "confident-false-positive",
             "justification": "misfires",
             "remediable": True, "screen_failed": False},
        ]
        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=findings,
            fp_screen_results=fp_results,
        )
        screen = payload["code_quality"]["false_positive_screen"]
        assert len(screen) == 1
        assert screen[0]["classification"] == "confident-false-positive"
        assert screen[0]["justification"] == "misfires"
        assert screen[0]["remediable"] is True
        assert screen[0]["screen_failed"] is False
        assert screen[0]["code"] == "F841"

    def test_cmd_issue_screen_wired_and_surfaces_in_report(self, capsys):
        """Integration: the screen is invoked once from the pipeline and the
        screened CFP unblocks closure (F1 AC4) with justification in the
        report (AC4). The remediation loop (F2/T2 scope) is neutralized
        here — it is exercised by its own test module with a temp project
        root so it never writes into the real checkout."""
        runner = self._make_runner(description="## Acceptance Criteria\n- AC1: thing")

        def _fake_pi(issue_id, context, prompt, **kwargs):
            if context == audit_runner.FP_SCREEN_CONTEXT:
                return _ok_result(json.dumps([
                    {"index": 0, "classification": "confident-false-positive",
                     "justification": "unused import in fixture"},
                ]))
            # Parent AC review + Phase 2 deep analysis both return met.
            return _ok_result(json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]))

        def _neutral_loop(**kwargs):
            return {
                "iterations": 0,
                "max_iterations": 3,
                "exhausted": False,
                "commits": [],
                "fingerprint_before": kwargs.get("content_fingerprint"),
                "fingerprint_after": kwargs.get("content_fingerprint"),
                "cq_findings": kwargs.get("cq_findings"),
                "fp_screen_results": kwargs.get("fp_screen_results"),
            }

        with (
            mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                              side_effect=_fake_pi),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True,
                              "findings": [_finding("critical", code="F401")],
                              "fixes_applied": 0},
            ),
            mock.patch(
                "skill.code_review.scripts.create_quality_epics."
                "create_epics_for_findings",
                return_value={"epic_id": None},
            ),
            mock.patch.object(audit_runner, "_run_remediation_loop",
                              side_effect=_neutral_loop),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=runner,
            )
        assert rc == 0
        report = capsys.readouterr().out
        assert "Ready to close: Yes" in report
        assert "#### False-positive screen" in report
        assert "confident-false-positive" in report
        assert "unused import in fixture" in report

    def _make_runner(self, description: str):
        """Mock wl/git runner mirroring test_audit_runner_phase1.py."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
        return mock_runner


# ===========================================================================
# AC5 — medium/low confident-false-positives are never remediable
# ===========================================================================

class TestRemediationScope:
    """AC5: only blocking-severity confident-false-positives are remediable."""

    @pytest.mark.parametrize("severity", ["medium", "low"])
    def test_medium_low_cfp_classified_but_not_remediable(self, severity):
        findings = [_finding(severity, code="F841")]
        raw = json.dumps([
            {"index": 0, "classification": "confident-false-positive",
             "justification": "misfires"},
        ])
        entries, _ = audit_runner._parse_fp_screen_response(raw, findings)
        assert entries[0]["classification"] == "confident-false-positive"
        assert entries[0]["remediable"] is False

    def test_critical_cfp_remediable(self):
        findings = [_finding("critical", code="F841")]
        raw = json.dumps([
            {"index": 0, "classification": "confident-false-positive",
             "justification": "misfires"},
        ])
        entries, _ = audit_runner._parse_fp_screen_response(raw, findings)
        assert entries[0]["classification"] == "confident-false-positive"
        assert entries[0]["remediable"] is True

    def test_uncertain_never_remediable(self):
        findings = [_finding("critical", code="F841")]
        raw = json.dumps([
            {"index": 0, "classification": "uncertain",
             "justification": "cannot tell"},
        ])
        entries, _ = audit_runner._parse_fp_screen_response(raw, findings)
        assert entries[0]["classification"] == "uncertain"
        assert entries[0]["remediable"] is False

    def test_medium_low_cfp_reported_in_report(self):
        """AC5: medium CFP is classified AND reported (surfaces in report)."""
        findings = [_finding("medium", code="F841")]
        fp_results = [
            {"index": 0, "finding": findings[0],
             "classification": "confident-false-positive",
             "justification": "misfires for fixture",
             "remediable": False, "screen_failed": False},
        ]
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=findings,
            fp_screen_results=fp_results,
            model="test-model",
        )
        assert "confident-false-positive" in report
        assert "misfires for fixture" in report


# ===========================================================================
# F1 AC4 — blocking semantics with the screen
# ===========================================================================

class TestBlockingWithScreen:
    """Only confident-false-positive critical/high findings stop blocking;
    uncertain findings remain blocking with the producer-annotation."""

    def test_cfp_critical_does_not_block(self):
        findings = [_finding("critical", code="F841")]
        fp_results = [
            {"index": 0, "finding": findings[0],
             "classification": "confident-false-positive",
             "justification": "misfires", "remediable": True,
             "screen_failed": False},
        ]
        blocked, reason = audit_runner._has_phase1_blocking_issues(
            findings, [], fp_screen_results=fp_results,
        )
        assert blocked is False
        assert reason == ""

    def test_uncertain_critical_blocks_with_annotation(self):
        """Uncertain stays blocking, annotated for the producer (F1 AC4)."""
        findings = [_finding("critical", code="F841")]
        fp_results = [
            {"index": 0, "finding": findings[0],
             "classification": "uncertain",
             "justification": "cannot tell", "remediable": False,
             "screen_failed": True},
        ]
        blocked, reason = audit_runner._has_phase1_blocking_issues(
            findings, [], fp_screen_results=fp_results,
        )
        assert blocked is True
        assert audit_runner.FP_CANDIDATE_ANNOTATION in reason

    def test_genuine_critical_blocks(self):
        findings = [_finding("critical", code="F841")]
        fp_results = [
            {"index": 0, "finding": findings[0],
             "classification": "genuine", "justification": "real defect",
             "remediable": False, "screen_failed": False},
        ]
        blocked, _ = audit_runner._has_phase1_blocking_issues(
            findings, [], fp_screen_results=fp_results,
        )
        assert blocked is True

    def test_unscreened_critical_still_blocks(self):
        """Backward compat: no screen results → critical/high still blocks."""
        findings = [_finding("critical", code="F841")]
        blocked, _ = audit_runner._has_phase1_blocking_issues(findings, [])
        assert blocked is True

    def test_non_ruff_critical_never_screened_still_blocks(self):
        """Non-ruff findings are never screened → always block at critical."""
        findings = [_finding("critical", code="no-undef", linter="eslint")]
        blocked, _ = audit_runner._has_phase1_blocking_issues(findings, [])
        assert blocked is True

    def test_report_ready_yes_when_cfp_screened(self):
        """F1 AC4: a screened CFP critical finding no longer blocks closure
        when all ACs are acceptable."""
        findings = [_finding("critical", code="F841")]
        fp_results = [
            {"index": 0, "finding": findings[0],
             "classification": "confident-false-positive",
             "justification": "misfires", "remediable": True,
             "screen_failed": False},
        ]
        ac_results = [{"text": "AC1", "verdict": "met", "evidence": "x"}]
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, ac_results, [],
            code_quality_findings=findings,
            fp_screen_results=fp_results,
            model="test-model",
        )
        assert "Ready to close: Yes" in report
        assert "no longer block closure" in report


# ===========================================================================
# Screen failure surfaces in the report (T1 AC2 traceability)
# ===========================================================================

class TestScreenFailureReporting:
    """A degraded screen leaves visible traceability in the report."""

    def test_screen_failed_entries_annotated_in_report(self):
        findings = [_finding("critical", code="F841")]
        fp_results = [
            {"index": 0, "finding": findings[0],
             "classification": "uncertain",
             "justification": audit_runner.FP_SCREEN_FAILED_JUSTIFICATION,
             "remediable": False, "screen_failed": True},
        ]
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            code_quality_findings=findings,
            fp_screen_results=fp_results,
            model="test-model",
        )
        assert "Screen degraded" in report
        assert audit_runner.FP_SCREEN_FAILED_JUSTIFICATION in report
        assert "Ready to close: No" in report
