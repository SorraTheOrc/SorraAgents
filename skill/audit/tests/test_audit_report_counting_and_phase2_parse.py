"""Regression tests for report counting + Phase 2 parse-failure disclosure.

Discovered during the WL-0MU6UL3XY001M3VT audit (2026-09-18):

1. The report summary mixed parent and child criteria counts.  With 2
   partial parent ACs and 3 partial child ACs the header read
   "5 of 2 acceptance criteria are only partially met".  Parent-level
   counts must ignore child criteria (children have their own section).

2. When Phase 2 deep analysis emitted output that could not be parsed
   (e.g. an infrastructure error string), the code silently kept the
   Phase 1 verdicts.  A parse failure must now degrade to ``partial`` with
   explicit evidence, while a *valid* empty array (``[]``) keeps the
   established Phase 1-verdict contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _ac(text, verdict, evidence="docs/x.md:1 — ok"):
    return {"text": text, "verdict": verdict, "evidence": evidence}


def _issue():
    return {
        "id": "TEST-0001", "title": "t", "stage": "in_review",
        "status": "completed", "parentId": None, "priority": "medium",
    }


# ---------------------------------------------------------------------------
# 1. Parent-only verdict counting
# ---------------------------------------------------------------------------

class TestReportCounting:
    def _report(self, ac_results, child_results):
        return audit_runner._assemble_issue_report(
            _issue(), ac_results, child_results, [], 0, None, [], [],
            model="m", model_source="local",
        )

    def test_partial_summary_counts_parent_only(self):
        ac_results = [_ac("P1", "partial"), _ac("P2", "partial")]
        child_results = [{
            "id": "TEST-C1", "title": "child", "status": "open",
            "stage": "intake_complete",
            "ac_results": [_ac("C1", "partial"), _ac("C2", "partial"),
                           _ac("C3", "partial")],
        }]
        report = self._report(ac_results, child_results)
        assert "2 of 2 acceptance criteria are only partially met" in report
        assert "5 of 2" not in report

    def test_all_met_summary_counts_parent_total(self):
        ac_results = [_ac("P1", "met"), _ac("P2", "met")]
        child_results = [{
            "id": "TEST-C2", "title": "child", "status": "completed",
            "stage": "in_review", "ac_results": [_ac("C1", "met")],
        }]
        report = self._report(ac_results, child_results)
        assert "All 2 acceptance criteria for work item TEST-0001" in report

    def test_unmet_summary_counts_parent_only(self):
        ac_results = [_ac("P1", "unmet"), _ac("P2", "met")]
        child_results = [{
            "id": "TEST-C3", "title": "child", "status": "open",
            "stage": "in_review", "ac_results": [_ac("C1", "unmet")],
        }]
        report = self._report(ac_results, child_results)
        assert "1 of 2 acceptance criteria" in report
        assert "2 of 2" not in report


# ---------------------------------------------------------------------------
# 2. Phase 2 parse-failure disclosure
# ---------------------------------------------------------------------------

def _make_issue_with_acs():
    return {
        "id": "TEST-0002", "title": "t", "stage": "in_review",
        "status": "completed",
        "description": "## Acceptance Criteria\n- AC1: first",
    }


class TestPhase2ParseFailure:
    def test_unparseable_parent_output_marks_partial(self):
        acs = [_ac("AC1", "met")]
        issue = _make_issue_with_acs()
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "server error: quota exceeded"},
        ):
            updated, _children, completed = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )
        assert updated[0]["verdict"] == "partial"
        assert "could not be parsed" in updated[0]["evidence"]
        # A parse failure marks Phase 2 incomplete (mirrors timeout/provider error).
        assert completed is False

    def test_valid_empty_array_keeps_phase1_verdict(self):
        """A valid [] is not a parse failure — existing contract holds."""
        acs = [_ac("AC1", "met")]
        issue = _make_issue_with_acs()
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ):
            updated, _children, _completed = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )
        assert updated[0]["verdict"] == "met"

    def test_unparseable_child_output_marks_partial(self):
        child = {
            "id": "TEST-C", "title": "child", "status": "open",
            "stage": "in_review", "ac_results": [_ac("C1", "met")],
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "boom: provider blew up"},
        ):
            _acs, children, _completed = audit_runner._run_phase2_deep_analysis(
                _make_issue_with_acs(), [_ac("AC1", "met")], [child], "test-model",
            )
        assert children[0]["ac_results"][0]["verdict"] == "partial"
        assert "could not be parsed" in children[0]["ac_results"][0]["evidence"]
