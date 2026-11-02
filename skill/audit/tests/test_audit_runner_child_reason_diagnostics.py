"""Child-assessment reason diagnostics (SA-0MVAWFYZE0040SQ3 AC3).

Machine-parseable reason codes emitted in ``_build_issue_json`` and
``_assemble_issue_report`` that distinguish an unserviced-in-main-slot
child from a genuine not-ready child.

Tests assert on:
- the ``REASON_UNSERVICED_IN_MAIN_SLOT`` / ``REASON_NOT_ASSESSED`` constants,
- the ``_classify_child_assessment_reason`` helper,
- ``child_reasons`` and ``not_assessed_children`` in the JSON payload,
- the greppable ``Child assessment reasons:`` line in the markdown report,
- both the stored-verdict-inherited case and the no-stored-verdict case.
"""
from skill.audit.scripts import audit_runner as ar

#: Parent ACs (all met) shared across diagnostic tests.
_PARENT_ACS = [
    {"text": "AC one", "verdict": "met", "evidence": "parent.py:1"},
]


def unserviced_child(**overrides):
    """Fixture: an unserviced in-main-slot child (partial ac + pending marker)."""
    child = {
        "id": "CHILD-UNS",
        "title": "Child CHILD-UNS",
        "status": "in_progress",
        "stage": "in_review",
        "ac_results": [
            {"text": "CAC1", "verdict": "partial",
             "evidence": "verdict pending main-session review."},
        ],
    }
    child.update(overrides)
    return child


def unserviced_child_with_stored_verdict():
    """Unserviced child that HAS a previously stored verdict (reused_from +
    child_audit_ready markers, same as the child-verdict reuse path)."""
    return unserviced_child(child_audit_ready=True,
                            reused_from="2026-10-01T00:00:00.000Z")


def unserviced_child_no_verdict():
    """Unserviced child with NO stored verdict (screens never serviced)."""
    return unserviced_child(stage="plan_complete")


def genuinely_not_ready_child():
    """A genuinely NOT-READY child: blocking verdict, no pending marker."""
    return {
        "id": "CHILD-NOTREADY",
        "title": "Child CHILD-NOTREADY",
        "status": "in_progress",
        "stage": "plan_complete",
        "child_audit_ready": False,
        "ac_results": [
            {"text": "CAC1", "verdict": "unmet",
             "evidence": "child.py:4 — blocking"},
        ],
    }


def assessed_in_review_child():
    """A fully assessed child in in_review — no reason code expected."""
    return unserviced_child(
        id="CHILD-OK",
        child_audit_ready=True,
        ac_results=[{"text": "CAC1", "verdict": "met", "evidence": "child.py:1"}],
    )


class TestReasonCodeConstants:
    def test_unserviced_in_main_slot_value(self):
        assert ar.REASON_UNSERVICED_IN_MAIN_SLOT == "unserviced_in_main_slot"

    def test_not_assessed_value(self):
        assert ar.REASON_NOT_ASSESSED == "not_assessed"


class TestClassifyChildAssessmentReason:
    def test_unserviced_with_stored_verdict(self):
        child = unserviced_child_with_stored_verdict()
        assert ar._classify_child_assessment_reason(
            child, {"CHILD-UNS"}) == ar.REASON_UNSERVICED_IN_MAIN_SLOT

    def test_unserviced_no_verdict(self):
        child = unserviced_child_no_verdict()
        assert ar._classify_child_assessment_reason(
            child, {"CHILD-UNS"}) == ar.REASON_UNSERVICED_IN_MAIN_SLOT

    def test_genuinely_not_ready(self):
        child = genuinely_not_ready_child()
        assert ar._classify_child_assessment_reason(
            child, None) == ar.REASON_NOT_ASSESSED

    def test_assessed_in_review_no_reason(self):
        child = assessed_in_review_child()
        assert ar._classify_child_assessment_reason(child, None) is None

    def test_no_stale_signal_means_no_unserviced_classification(self):
        """The stale pending signal is required for the unserviced reason."""
        child = unserviced_child_with_stored_verdict()
        assert ar._classify_child_assessment_reason(child, None) is None

    def test_stale_signal_without_pending_marker_not_unserviced(self):
        """Needs the pending marker in ac_results to be unserviced."""
        child = {
            "id": "CHILD-X",
            "title": "Child",
            "status": "in_progress",
            "stage": "plan_complete",
            "child_audit_ready": False,
            "ac_results": [
                {"text": "CAC1", "verdict": "unmet", "evidence": "blocking"},
            ],
        }
        # stale signal present but no pending marker → not unserviced;
        # child_audit_ready False → not_assessed
        assert ar._classify_child_assessment_reason(
            child, {"CHILD-X"}) == ar.REASON_NOT_ASSESSED


class TestJsonPayloadDiagnostics:
    """_build_issue_json emits child_reasons and not_assessed_children."""

    def test_unserviced_with_stored_verdict_reason_in_json(self):
        stale_pending_ids = {"CHILD-UNS"}
        child = unserviced_child_with_stored_verdict()
        payload = ar._build_issue_json(
            {"id": "TEST-1"}, _PARENT_ACS, [child],
            stale_pending_ids=stale_pending_ids,
        )
        assert payload["child_reasons"]["CHILD-UNS"] == \
            ar.REASON_UNSERVICED_IN_MAIN_SLOT
        assert {"id": "CHILD-UNS",
                "reason": ar.REASON_UNSERVICED_IN_MAIN_SLOT} \
            in payload["not_assessed_children"]

    def test_unserviced_no_verdict_reason_in_json(self):
        stale_pending_ids = {"CHILD-UNS"}
        child = unserviced_child_no_verdict()
        payload = ar._build_issue_json(
            {"id": "TEST-1"}, _PARENT_ACS, [child],
            stale_pending_ids=stale_pending_ids,
        )
        assert payload["child_reasons"]["CHILD-UNS"] == \
            ar.REASON_UNSERVICED_IN_MAIN_SLOT
        assert {"id": "CHILD-UNS",
                "reason": ar.REASON_UNSERVICED_IN_MAIN_SLOT} \
            in payload["not_assessed_children"]

    def test_genuinely_not_ready_reason_in_json(self):
        child = genuinely_not_ready_child()
        payload = ar._build_issue_json(
            {"id": "TEST-1"}, _PARENT_ACS, [child],
        )
        assert payload["child_reasons"]["CHILD-NOTREADY"] == \
            ar.REASON_NOT_ASSESSED
        assert {"id": "CHILD-NOTREADY", "reason": ar.REASON_NOT_ASSESSED} \
            in payload["not_assessed_children"]

    def test_assessed_child_no_reason_in_json(self):
        child = assessed_in_review_child()
        payload = ar._build_issue_json(
            {"id": "TEST-1"}, _PARENT_ACS, [child],
        )
        assert "CHILD-OK" not in payload["child_reasons"]
        assert payload["not_assessed_children"] == []

    def test_mixed_children_reasons(self):
        stale_pending_ids = {"CHILD-UNS"}
        children = [
            unserviced_child_with_stored_verdict(),
            genuinely_not_ready_child(),
            assessed_in_review_child(),
        ]
        payload = ar._build_issue_json(
            {"id": "TEST-1"}, _PARENT_ACS, children,
            stale_pending_ids=stale_pending_ids,
        )
        assert payload["child_reasons"]["CHILD-UNS"] == \
            ar.REASON_UNSERVICED_IN_MAIN_SLOT
        assert payload["child_reasons"]["CHILD-NOTREADY"] == \
            ar.REASON_NOT_ASSESSED
        assert "CHILD-OK" not in payload["child_reasons"]
        assert len(payload["not_assessed_children"]) == 2

    def test_no_children_empty_reasons(self):
        payload = ar._build_issue_json(
            {"id": "TEST-1"}, _PARENT_ACS, [],
        )
        assert payload["child_reasons"] == {}
        assert payload["not_assessed_children"] == []

    def test_no_stale_signal_no_unserviced_reason(self):
        child = unserviced_child_with_stored_verdict()
        payload = ar._build_issue_json(
            {"id": "TEST-1"}, _PARENT_ACS, [child],
        )
        assert payload["child_reasons"] == {}


class TestMarkdownReportDiagnostics:
    """_assemble_issue_report emits a greppable diagnostic line."""

    def test_unserviced_reason_line_in_report(self):
        stale_pending_ids = {"CHILD-UNS"}
        child = unserviced_child_with_stored_verdict()
        report = ar._assemble_issue_report(
            {"id": "TEST-1"}, _PARENT_ACS, [child],
            stale_pending_ids=stale_pending_ids,
        )
        assert "Child assessment reasons:" in report
        assert "[unserviced_in_main_slot] CHILD-UNS" in report

    def test_genuinely_not_ready_reason_line_in_report(self):
        child = genuinely_not_ready_child()
        report = ar._assemble_issue_report(
            {"id": "TEST-1"}, _PARENT_ACS, [child],
        )
        assert "Child assessment reasons:" in report
        assert "[not_assessed] CHILD-NOTREADY" in report

    def test_assessed_children_no_diagnostic_line(self):
        child = assessed_in_review_child()
        report = ar._assemble_issue_report(
            {"id": "TEST-1"}, _PARENT_ACS, [child],
        )
        assert "Child assessment reasons:" not in report

    def test_mixed_children_diagnostic_line(self):
        stale_pending_ids = {"CHILD-UNS"}
        children = [
            unserviced_child_no_verdict(),
            genuinely_not_ready_child(),
        ]
        report = ar._assemble_issue_report(
            {"id": "TEST-1"}, _PARENT_ACS, children,
            stale_pending_ids=stale_pending_ids,
        )
        line = next(ln for ln in report.splitlines()
                    if ln.startswith("Child assessment reasons:"))
        assert "[unserviced_in_main_slot] CHILD-UNS" in line
        assert "[not_assessed] CHILD-NOTREADY" in line


def screened_in_run_child(child_id="CHILD-RUN"):
    """A child whose criteria were screened by THIS run (separate-process).

    Reproduces the MS-0MVQO6N34007DHP8 audit: the child-verdict reuse
    pre-pass runs before screening, so a child with no stored verdict gets
    ``child_audit_ready = False``; the screening that follows then fills
    ``ac_results`` with real (non-pending) verdicts. Four such children were
    reported ``not_assessed`` even though every criterion was ``met`` and each
    child audit was persisted in the same run with Ready to close: Yes.
    """
    return {
        "id": child_id,
        "title": f"Child {child_id}",
        "status": "completed",
        "stage": "in_review",
        "child_audit_ready": False,
        "ac_results": [
            {"text": "CAC1", "verdict": "met", "evidence": "child.py:1"},
            {"text": "CAC2", "verdict": "adjusted", "evidence": "child.py:9"},
        ],
    }


def pending_main_slot_child():
    """An in-main-slot child whose screens this run emitted but nobody ran."""
    return {
        "id": "CHILD-PENDING",
        "title": "Child CHILD-PENDING",
        "status": "in_progress",
        "stage": "in_review",
        "child_audit_ready": False,
        "ac_results": [
            {"text": "CAC1", "verdict": "partial",
             "evidence": "verdict pending main-session review."},
        ],
    }


class TestScreenedInRunChildAssessment:
    """A child screened in this run is assessed — never ``not_assessed``.

    MS-0MVQO6N34007DHP8: the diagnostic must keep meaning "never judged",
    not "judged and not ready" (the latter is the blocking-verdict path).
    """

    def test_screened_in_run_child_has_no_reason_code(self):
        assert ar._classify_child_assessment_reason(
            screened_in_run_child(), None) is None

    def test_four_screened_children_produce_no_json_reasons(self):
        children = [screened_in_run_child(f"CHILD-RUN-{i}") for i in range(4)]
        payload = ar._build_issue_json(
            {"id": "TEST-1"}, _PARENT_ACS, children,
        )
        assert payload["child_reasons"] == {}
        assert payload["not_assessed_children"] == []

    def test_screened_children_produce_no_report_diagnostic_line(self):
        children = [screened_in_run_child(f"CHILD-RUN-{i}") for i in range(4)]
        report = ar._assemble_issue_report(
            {"id": "TEST-1"}, _PARENT_ACS, children,
        )
        assert "Child assessment reasons:" not in report

    def test_pending_main_slot_child_still_not_assessed(self):
        """Unserviced screens are NOT an assessment (no stale signal needed)."""
        assert ar._classify_child_assessment_reason(
            pending_main_slot_child(), None) == ar.REASON_NOT_ASSESSED

    def test_blocking_verdict_child_still_not_assessed(self):
        child = screened_in_run_child("CHILD-BLOCKED")
        child["ac_results"] = [
            {"text": "CAC1", "verdict": "unmet", "evidence": "child.py:4"},
        ]
        assert ar._classify_child_assessment_reason(
            child, None) == ar.REASON_NOT_ASSESSED

    def test_budget_exceeded_child_still_not_assessed(self):
        child = screened_in_run_child("CHILD-BUDGET")
        child["budget_exceeded"] = True
        assert ar._classify_child_assessment_reason(
            child, None) == ar.REASON_NOT_ASSESSED
