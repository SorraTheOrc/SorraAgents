"""Regression test: persist_audit.py must not pass --stage on the
wl update --audit-text call (SA-0MTHC710X003ORZM).

Root cause: ``persist_audit.py`` fetched the current work-item stage and
passed ``--stage <stage>`` to ``wl update --audit-text``.  Worklog's
``update`` command calls ``db.update()`` whenever a stage is present,
which bumps the ``updatedAt`` column.  If ``updatedAt`` advances past
``auditedAt + AUDIT_FRESHNESS_BUFFER`` (60 s), freshness checks
``_audit_time_is_fresh`` / ``isAuditFresh()`` return *false* and
passed audits show a stale icon (⏳) instead of the green check (✅).

Fix: remove the stage-fetch + --stage injection.  The audit runner's
``_apply_terminal_lifecycle`` already handles verdict-driven status
transitions.

ACs tested:

* AC1 – the ``wl update`` argv contains *no* ``--stage`` flag
* AC2 – the audit text is still persisted (``--audit-text`` present)
* AC3 – the ``audit-set`` call still runs (audit store written)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import persist_audit


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

class _CallRecorder:
    """Capture every ``subprocess.run`` call and return canned responses."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs) -> SimpleNamespace:
        self.calls.append(list(cmd))

        cmd_str = " ".join(cmd)

        # -- audit-set → success
        if "audit-set" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        # -- wl update --audit-text → success (no stage bump)
        if "update" in cmd_str and "audit-text" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        # -- wl show --json → return a work item with a stage
        if "show" in cmd_str and "--json" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "TEST-1",
                        "stage": "plan_complete",
                        "status": "open",
                    },
                }),
                stderr="",
            )

        # default: success
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"success": True}),
            stderr="",
        )


def _make_runner() -> _CallRecorder:
    return _CallRecorder()


# ------------------------------------------------------------------
# ACs
# ------------------------------------------------------------------

class TestPersistAuditNoStageBump:
    """SA-0MTHC710X003ORZM: persist_audit.py must NOT pass --stage."""

    def test_ac1_no_stage_in_audit_text_update(self):
        """AC1: the wl update --audit-text call does NOT include --stage.

        The old behaviour fetched the work-item stage and appended
        ``--stage plan_complete`` to the update command, which caused
        db.update() → updatedAt bump → stale icon.  The fix removes this
        entirely.
        """
        runner = _make_runner()
        report = (
            "Ready to close: Yes\n\n"
            "Audit report for work item TEST-1\n\n"
            "## Summary\nAll criteria acceptable."
        )
        persist_audit.persist_audit(
            issue_id="TEST-1",
            report_text=report,
            runner=runner,
        )

        # Collect the update call args
        update_calls = [
            c for c in runner.calls
            if "update" in c and "--audit-text" in c
        ]
        assert len(update_calls) == 1, (
            f"Expected exactly one --audit-text call, got {len(update_calls)}: "
            f"{update_calls}"
        )
        update_cmd = update_calls[0]

        # Verify --audit-text is present
        assert "--audit-text" in update_cmd

        # CRITICAL: --stage must NOT be present
        assert "--stage" not in update_cmd, (
            f"--stage found in update command: {update_cmd}. "
            "This will bump updatedAt and invalidate audit freshness "
            "(SA-0MTHC710X003ORZM)."
        )

    def test_ac2_audit_text_persisted(self):
        """AC2: the audit text is still written via wl update --audit-text."""
        runner = _make_runner()
        report = (
            "Ready to close: Yes\n\n"
            "Audit report for work item TEST-1\n\n"
            "## Summary\nAll criteria acceptable."
        )
        rc = persist_audit.persist_audit(
            issue_id="TEST-1",
            report_text=report,
            runner=runner,
        )
        assert rc == 0

        update_calls = [
            c for c in runner.calls
            if "update" in c and "--audit-text" in c
        ]
        assert len(update_calls) == 1
        # The report text should be passed somewhere (as --audit-text arg value
        # or via a file)
        assert any("--audit-text" in c for c in update_calls)

    def test_ac3_audit_set_still_runs(self):
        """AC3: the wl audit-set call still executes (audit store written)."""
        runner = _make_runner()
        report = (
            "Ready to close: Yes\n\n"
            "Audit report for work item TEST-1\n\n"
            "## Summary\nAll criteria acceptable."
        )
        rc = persist_audit.persist_audit(
            issue_id="TEST-1",
            report_text=report,
            runner=runner,
        )
        assert rc == 0

        audit_set_calls = [
            c for c in runner.calls if "audit-set" in c
        ]
        assert len(audit_set_calls) == 1, (
            f"Expected exactly one audit-set call, got {len(audit_set_calls)}"
        )

    def test_no_fetch_before_audit_text(self):
        """Verify persist_audit does NOT fetch work item before audit-text.

        The old code called ``wl show --json`` to fetch the stage before
        running the audit-text update.  The fix removes this fetch, so
        there should be no pre-audit-text show call in the sequence
        (there may still be a show call for other purposes like identity
        checking, but we verify no show call appears between audit-set and
        audit-text).
        """
        runner = _make_runner()
        report = (
            "Ready to close: Yes\n\n"
            "Audit report for work item TEST-1\n\n"
            "## Summary\nAll criteria acceptable."
        )
        persist_audit.persist_audit(
            issue_id="TEST-1",
            report_text=report,
            runner=runner,
        )

        # Find indices of audit-set and audit-text calls
        audit_set_idx = None
        audit_text_idx = None
        for i, c in enumerate(runner.calls):
            if "audit-set" in c:
                audit_set_idx = i
            if "update" in c and "audit-text" in c:
                audit_text_idx = i

        if audit_set_idx is not None and audit_text_idx is not None:
            # No show call between audit-set and audit-text
            between = runner.calls[audit_set_idx + 1: audit_text_idx]
            for call in between:
                call_str = " ".join(call)
                assert "show" not in call_str, (
                    f"Unexpected show call between audit-set and audit-text: "
                    f"{call_str}"
                )

    def test_regression_post_persist_comment_does_not_invalidate(self):
        """AC4: simulating the race — after persist_audit, a subsequent
        wl comment add (which bumps updatedAt) should NOT invalidate a fresh
        audit in the time-gate path.

        This test validates the fix prevents the *source* of the race
        (persist_audit adding --stage).  The downstream freshness logic
        (_audit_time_is_fresh, isAuditFresh) is tested in
        test_audit_runner_freshness.py.
        """
        runner = _make_runner()
        report = (
            "Ready to close: Yes\n\n"
            "Audit report for work item TEST-1\n\n"
            "## Summary\nAll criteria acceptable."
        )
        rc = persist_audit.persist_audit(
            issue_id="TEST-1",
            report_text=report,
            runner=runner,
        )
        assert rc == 0

        # Verify the update command that was issued does NOT include --stage.
        # If it did, the next wl comment add would bump updatedAt past
        # auditedAt, failing _audit_time_is_fresh.
        update_cmd = next(
            c for c in runner.calls
            if "update" in c and "--audit-text" in c
        )
        assert "--stage" not in update_cmd, (
            "Regression: --stage in audit-text update will cause the "
            "next wl comment add to bump updatedAt past auditedAt, "
            "invalidating the fresh audit (SA-0MTHC710X003ORZM)."
        )
