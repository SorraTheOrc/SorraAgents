"""Audit Conclusion gate — a failed or missing audit must never claim success.

Regression for SA-0MWIIOMY10054VJI. Before this fix the report renderer derived
the Conclusion verdict from the **session's own** acceptance criteria only, so an
audit session whose work item's audit failed still ended with
``✅ Ready to ship``. The renderer already knew the audit verdict (it drives the
Meta-Data ``Audit:`` line); the fix gates the Conclusion and header on that
verdict for the ``audit`` skill while leaving every other skill byte-identical.

Invariant: ``✅ Ready to ship`` may only be rendered when the audit passed
(``auditResult.readyToClose == true`` / legacy ``Ready to close: Yes``).
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

import pytest

# Ensure the scripts directory is on the path so we can import the renderer.
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, SCRIPTS_DIR)

ITEM_ID = "AH-0MWHU5AV800969NA"
ITEM_TITLE = "Update Hangar War time budget: 5-15 minutes per hex"

READY_TO_SHIP = "\u2705 Ready to ship"
NOT_READY_TO_SHIP = "\U0001f6ab Not ready to ship"

#: The audit session's own ACs (audit was performed, verdict recorded) — all met
#: in the observed payload that produced the bug.
SESSION_ACS_MET = [
    ("1", "Work item assessed against acceptance criteria", "audit report", "met"),
    ("2", "Audit verdict determined and recorded", "Ready to close line", "met"),
    ("3", "Ready-to-close line added", "persisted audit report", "met"),
]

#: Same session ACs with one unmet (audit was not fully performed).
SESSION_ACS_UNMET = [
    ("1", "Work item assessed against acceptance criteria", "audit report", "met"),
    ("2", "Audit verdict determined and recorded", "Ready to close line", "unmet"),
]


def _envelope(verdict):
    """Build a ``wl show --json --children``-shaped envelope for the AH item.

    ``verdict`` is ``"yes"``, ``"no"`` or ``"unknown"`` (no persisted audit at
    all). The structured top-level ``auditResult`` mirrors the legacy
    ``workItem.audit`` text, exactly like the observed production payload.
    """
    work_item = {
        "id": ITEM_ID,
        "title": ITEM_TITLE,
        "status": "in-progress",
        "priority": "high",
        "issueType": "bug",
        "stage": "in_review",
        "risk": "medium",
        "effort": "Medium",
        "auditResult": None,
    }
    audit_result = None
    if verdict in ("yes", "no"):
        ready = verdict == "yes"
        verdict_line = "Ready to close: Yes" if ready else "Ready to close: No"
        work_item["audit"] = {
            "time": "2026-11-16T00:12:36Z",
            "author": "audit",
            "text": f"{verdict_line}\n\nAudit details.",
            "status": "Complete",
        }
        audit_result = {
            "workItemId": ITEM_ID,
            "readyToClose": ready,
            "auditedAt": "2026-11-16T00:12:36Z",
            "summary": "audit summary",
            "rawOutput": None,
            "author": "audit",
            "fingerprint": None,
        }
    return {
        "success": True,
        "workItem": work_item,
        "auditResult": audit_result,
        "comments": [],
    }


def _render_from_workitem(envelope, *, skill_name="audit", acs=None):
    """Render via the library entry point shared by every skill."""
    from render_report import render_report_from_workitem

    return render_report_from_workitem(
        work_item=envelope["workItem"],
        skill_name=skill_name,
        headline="Audit performed.",
        acceptance_criteria=list(SESSION_ACS_MET if acs is None else acs),
        children=envelope.get("children", []),
        audit_result=envelope.get("auditResult"),
        colour=False,
    )


def _render_from_wl(envelope, *, skill_name="audit", acs=None):
    """Render through the ``wl``-backed entry point with ``wl show`` mocked."""
    import render_report as rr

    fake = mock.Mock(returncode=0, stdout=json.dumps(envelope), stderr="")
    with mock.patch.object(rr.subprocess, "run", return_value=fake), \
            mock.patch.object(rr, "worklog_dir_flag", return_value=[]):
        return rr.render_from_wl(
            skill_name=skill_name,
            work_item_id=ITEM_ID,
            headline="Audit performed.",
            acceptance_criteria=list(SESSION_ACS_MET if acs is None else acs),
            colour=False,
        )


def _render_json_stdin(envelope, *, skill_name="audit", acs=None):
    """Render through the ``--json`` stdin CLI path."""
    import render_report as rr

    argv = ["render_report.py", "--json", "--skill-name", skill_name,
            "--no-colour"]
    for _num, desc, metric, verdict in (SESSION_ACS_MET if acs is None else acs):
        argv += ["--ac", f"{desc}|{metric}|{verdict}"]
    with mock.patch.object(rr.sys, "argv", argv), \
            mock.patch.object(rr.sys, "stdin",
                              io.StringIO(json.dumps(envelope))), \
            mock.patch.object(rr, "update_pane_title",
                              return_value={"updated": False}):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rr.main()
    return buffer.getvalue()


def _header(report):
    return report.splitlines()[0]


def _conclusion(report):
    return report.split("## Conclusion", 1)[1]


def _audit_reason_line(report):
    """Return the Conclusion's audit-verdict reason line (or ``None``)."""
    for line in _conclusion(report).splitlines():
        if line.startswith("Audit verdict:"):
            return line.strip()
    return None


class TestAuditFailureNeverClaimsReady(unittest.TestCase):
    """AC1: a failed audit must render ``🚫 Not ready to ship`` + a reason."""

    def test_failed_audit_renders_not_ready_with_reason(self):
        report = _render_from_workitem(_envelope("no"))
        conclusion = _conclusion(report)
        self.assertIn(NOT_READY_TO_SHIP, conclusion)
        # No success claim anywhere in the report — in particular the Conclusion.
        self.assertNotIn(READY_TO_SHIP, report)
        self.assertNotIn("Ready to ship", report)
        # Reason line names the verdict and the exact command to read the report.
        self.assertIn("Audit verdict: Ready to close: No", conclusion)
        self.assertIn(f"wl audit-show {ITEM_ID} --json", conclusion)

    def test_failed_audit_never_renders_completion_sentence(self):
        report = _render_from_workitem(_envelope("no"))
        self.assertNotIn("This completes the audit process", report)


class TestAuditPassStillClaimsReady(unittest.TestCase):
    """AC2: no regression to the passing case."""

    def test_passed_audit_renders_ready_to_ship(self):
        report = _render_from_workitem(_envelope("yes"))
        conclusion = _conclusion(report)
        self.assertIn(READY_TO_SHIP, conclusion)
        self.assertNotIn(NOT_READY_TO_SHIP, report)
        # No audit reason line when the audit passed.
        self.assertIsNone(_audit_reason_line(report))

    def test_passed_audit_renders_completion_sentence(self):
        report = _render_from_workitem(_envelope("yes"))
        self.assertIn("This completes the audit process", report)


class TestMissingAuditFailsClosed(unittest.TestCase):
    """AC3: no stored audit ⇒ not ready (fail closed), with a 'not run' reason."""

    def test_missing_audit_renders_not_ready(self):
        report = _render_from_workitem(_envelope("unknown"))
        conclusion = _conclusion(report)
        self.assertIn(NOT_READY_TO_SHIP, conclusion)
        self.assertNotIn(READY_TO_SHIP, report)
        self.assertIn("Audit verdict: not run", conclusion)
        self.assertIn(f"no persisted audit for {ITEM_ID}", conclusion)

    def test_legacy_audit_absent_and_audit_result_null_is_not_run(self):
        envelope = _envelope("unknown")
        self.assertIsNone(envelope["workItem"].get("auditResult"))
        self.assertNotIn("audit", envelope["workItem"])
        self.assertIsNone(envelope["auditResult"])


@pytest.mark.parametrize("verdict", ["yes", "no", "unknown"])
@pytest.mark.parametrize(
    "acs_label,acs",
    [("met", SESSION_ACS_MET), ("unmet", SESSION_ACS_UNMET)],
)
def test_header_and_conclusion_agree(verdict, acs_label, acs):
    """AC4: header and Conclusion never disagree (matrix of verdicts × ACs)."""
    report = _render_from_workitem(_envelope(verdict), acs=acs)
    header = _header(report)
    conclusion = _conclusion(report)
    ready = verdict == "yes" and acs_label == "met"
    if ready:
        assert header == "# Completed audit"
        assert READY_TO_SHIP in conclusion
        assert NOT_READY_TO_SHIP not in conclusion
    else:
        assert header == "# Incomplete audit"
        assert NOT_READY_TO_SHIP in conclusion
        assert READY_TO_SHIP not in conclusion


class TestBothEntryPointsAgree(unittest.TestCase):
    """AC5: the ``--json`` stdin path and ``render_from_wl`` agree."""

    def test_json_stdin_and_wl_paths_render_same_verdict(self):
        envelope = _envelope("no")
        json_report = _render_json_stdin(envelope)
        wl_report = _render_from_wl(envelope)

        self.assertIn(NOT_READY_TO_SHIP, _conclusion(json_report))
        self.assertIn(NOT_READY_TO_SHIP, _conclusion(wl_report))
        self.assertNotIn(READY_TO_SHIP, json_report)
        self.assertNotIn(READY_TO_SHIP, wl_report)
        self.assertEqual(
            _audit_reason_line(json_report),
            _audit_reason_line(wl_report),
        )


class TestNonAuditSkillsUnchanged(unittest.TestCase):
    """AC6: non-audit skills ignore the audit verdict (byte-identical output)."""

    def test_non_audit_skill_with_failed_audit_still_ready(self):
        report = _render_from_workitem(_envelope("no"), skill_name="implement")
        self.assertEqual(_header(report), "# Completed implement")
        self.assertIn(READY_TO_SHIP, _conclusion(report))
        self.assertNotIn(NOT_READY_TO_SHIP, report)
        self.assertIsNone(_audit_reason_line(report))

    def test_non_audit_skill_with_missing_audit_still_ready(self):
        report = _render_from_workitem(_envelope("unknown"), skill_name="implement")
        self.assertIn(READY_TO_SHIP, _conclusion(report))
        self.assertNotIn(NOT_READY_TO_SHIP, report)

    def test_other_non_audit_skills_still_ready(self):
        for skill_name in ("plan", "intake", "test", "refactor"):
            report = _render_from_workitem(_envelope("no"), skill_name=skill_name)
            self.assertIn(READY_TO_SHIP, _conclusion(report))


@pytest.mark.parametrize("skill_name", ["audit", "Audit", "AUDIT"])
def test_failed_audit_gate_is_case_insensitive(skill_name):
    """The gate applies to ``--skill-name audit`` in any casing."""
    report = _render_from_workitem(_envelope("no"), skill_name=skill_name)
    assert NOT_READY_TO_SHIP in _conclusion(report)
    assert READY_TO_SHIP not in report
