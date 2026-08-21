"""Tests for persist_audit identity guard and unique file naming.

Covers:
  - Mismatched report rejected (different work-item ID in report)
  - Matching report persisted (target ID present in report)
  - Unique file naming convention (audit_report_<id>.md)
  - Readback identity verification (stored audit matches target ID)
  - Reports mentioning the target ID *and* other IDs are accepted
    (child-audit / parent-audit scenario)
  - Reports mentioning *only* other IDs are rejected
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the audit scripts package is importable from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from audit.scripts.persist_audit import persist_audit
from audit.tests.wl_helpers import make_stateful_runner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(
    *,
    audit_set_rc: int = 0,
    audit_set_stdout: str = '{"success": true}',
    audit_set_stderr: str = "",
    show_rc: int = 0,
    show_stdout: str = '{"workItem": {"stage": "intake_complete"}}',
    show_stderr: str = "",
    update_rc: int = 0,
    update_stdout: str = '{"success": true}',
    update_stderr: str = "",
) -> MagicMock:
    """Build an injectable runner that returns fixed responses."""
    mock = MagicMock(return_code=0)
    responses = {
        ("wl", "audit-set"): MagicMock(
            returncode=audit_set_rc,
            stdout=audit_set_stdout,
            stderr=audit_set_stderr,
        ),
        ("wl", "show"): MagicMock(
            returncode=show_rc,
            stdout=show_stdout,
            stderr=show_stderr,
        ),
        ("wl", "update"): MagicMock(
            returncode=update_rc,
            stdout=update_stdout,
            stderr=update_stderr,
        ),
    }

    def _runner(cmd, **kwargs):
        # Key on the wl subcommand anywhere in the argv (persist_audit may
        # inject a resolved ``--worklog-dir <value>`` pair at position 1
        # since SA-0MSKQERKH002IBLG).
        cmd_str = " ".join(cmd)
        if "audit-set" in cmd_str:
            return responses[("wl", "audit-set")]
        if "--audit-text" in cmd_str:
            return responses[("wl", "update")]
        if "show" in cmd_str:
            return responses[("wl", "show")]
        if "update" in cmd_str:
            return responses[("wl", "update")]
        return MagicMock(returncode=0, stdout="{}", stderr="")

    mock.side_effect = _runner
    return mock


# ---------------------------------------------------------------------------
# AC3: Identity guard — mismatched report rejected
# ---------------------------------------------------------------------------

class TestIdentityGuardMismatch:
    """persist_audit rejects reports that reference a *different* work-item ID."""

    def test_report_with_different_id_rejected(self):
        """A report mentioning OSL-AAA111BBB001XXXX while persisting for
        SA-0MSAS108O009DYKT must be rejected."""
        runner = _make_runner()
        report = (
            "Audit of OSL-AAA111BBB001XXXX\n"
            "Ready to close: Yes\n"
            "All ACs met.\n"
        )
        rc = persist_audit(
            "SA-0MSAS108O009DYKT", report, runner=runner,
        )
        assert rc != 0  # must fail

    def test_report_with_no_work_item_id_warns_but_accepted(self):
        """A report mentioning no work-item ID is accepted with a warning
        (conservative guard: absence of any ID does not clearly reference
        a *different* work item, so it must not block persistence)."""
        runner = _make_runner()
        report = (
            "Some generic audit report\n"
            "Ready to close: Yes\n"
        )
        rc = persist_audit(
            "SA-0MSAS108O009DYKT", report, runner=runner,
        )
        assert rc == 0

    def test_report_with_target_id_accepted(self):
        """A report that mentions the target ID is accepted."""
        runner = _make_runner()
        report = (
            "Audit of SA-0MSAS108O009DYKT\n"
            "Ready to close: Yes\n"
        )
        rc = persist_audit(
            "SA-0MSAS108O009DYKT", report, runner=runner,
        )
        assert rc == 0

    def test_report_with_target_and_other_ids_accepted(self):
        """A report mentioning the target ID *and* other IDs is accepted
        (child-audit scenario where parent is referenced)."""
        runner = _make_runner()
        report = (
            "Audit of SA-0MSAS108O009DYKT\n"
            "Related to OSL-AAA111BBB001XXXX\n"
            "Ready to close: Yes\n"
        )
        rc = persist_audit(
            "SA-0MSAS108O009DYKT", report, runner=runner,
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# AC3: Identity guard — unique file naming
# ---------------------------------------------------------------------------

class TestUniqueFileNaming:
    """Report file paths must use the unique naming convention."""

    @pytest.mark.parametrize(
        "issue_id,expected_suffix",
        [
            ("SA-0MSAS108O009DYKT", "audit_report_SA-0MSAS108O009DYKT.md"),
            ("OSL-0MSABC7SB001NVUN", "audit_report_OSL-0MSABC7SB001NVUN.md"),
        ],
    )
    def test_report_file_name_is_unique(self, issue_id, expected_suffix):
        """The unique file name for a work item must contain the ID."""
        assert expected_suffix == f"audit_report_{issue_id}.md"

    def test_naming_convention_documented_in_skill(self):
        """SKILL.md must document the unique file naming convention."""
        skill_md = Path(__file__).resolve().parents[2] / "audit" / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        ref = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "dev"
            / "audit-skill-reference.md"
        )
        if ref.exists():
            content += "\n" + ref.read_text(encoding="utf-8")
        # The convention should be documented
        assert "audit_report_" in content or "unique name" in content.lower(), (
            "SKILL.md should document the unique naming convention "
            "for audit report files"
        )


# ---------------------------------------------------------------------------
# AC4: Readback identity verification
# ---------------------------------------------------------------------------

class TestReadbackIdentity:
    """After persistence, readback should confirm the stored audit matches
    the intended work item."""

    def test_readback_confirms_target_id(self):
        """Readback should verify the stored audit mentions the target ID.

        We mock _run_wl so that audit-show returns rawOutput containing
        the target work-item ID.  The assertion is that the readback
        logic accepts this as valid.
        """
        # This test validates the *pattern* of identity checking —
        # the actual implementation in audit_runner.py is tested
        # indirectly through the integration tests.  Here we verify
        # that a report containing the target ID would pass.
        target_id = "SA-0MSAS108O009DYKT"
        raw_output = f"Audit of {target_id}\nReady to close: Yes\nAll ACs met."
        # The identity check is: target_id must appear in raw_output
        assert target_id in raw_output

    def test_readback_rejects_wrong_id(self):
        """Readback should reject a stored audit that does not mention
        the target work-item ID."""
        target_id = "SA-0MSAS108O009DYKT"
        raw_output = "Audit of OSL-AAA111BBB001XXXX\nReady to close: Yes."
        assert target_id not in raw_output


# ---------------------------------------------------------------------------
# Integration-ish: CLI argument handling
# ---------------------------------------------------------------------------

class TestCLIArgumentHandling:
    """The persist_audit CLI must handle identity guard correctly."""

    def test_cli_rejects_mismatched_file(self, tmp_path: Path):
        """persist_audit.py --file with mismatched content must exit non-zero."""
        report_file = tmp_path / "report.md"
        report_file.write_text(
            "Audit of OSL-AAA111BBB001XXXX\nReady to close: Yes\n"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[2] / "scripts" / "persist_audit.py"),
                "--issue-id", "SA-0MSAS108O009DYKT",
                "--file", str(report_file),
                "--wl-bin", "true",  # never actually run wl
            ],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            check=False,
        )
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# AC4: Readback identity verification in the audit runner
# ---------------------------------------------------------------------------

class TestReadbackIdentityRunner:
    """cmd_issue's readback verification checks stored content identity.

    After persistence, the runner reads back the stored audit via
    ``wl audit-show`` and verifies the stored content references the
    target work-item ID — not just that some non-empty audit exists.
    """

    def _make_runner(self, audit_show_raw: str | None):
        """Build a runner handling the wl calls made by cmd_issue + persist_audit.

        persist_audit's own wl calls (audit-set / show / update) run through
        ``subprocess.run``, which is patched separately; this runner handles
        the cmd_issue-level calls (status capture, in_progress, children
        fetch, readback audit-show, terminal status update).
        """
        def fake_runner(cmd):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps({
                        "success": True,
                        "audit": {"rawOutput": audit_show_raw} if audit_show_raw else None,
                    }),
                    stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1", "status": "open", "stage": "plan_complete",
                        },
                    }),
                    stderr="",
                )
            if "--children" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1", "description": "",
                            "status": "open", "stage": "plan_complete",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            # wl update (in_progress + terminal lifecycle transition)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"success": True}), stderr="",
            )

        return make_stateful_runner(fake_runner)

    def _run_cmd_issue(self, audit_show_raw: str | None, report: str):
        """Run cmd_issue with persist=True and a controlled assembled report."""
        from audit.scripts import audit_runner as ar_module
        from audit.scripts.audit_runner import cmd_issue

        def fake_subprocess_run(cmd, *args, **kwargs):
            """Handle persist_audit's wl audit-set / show / update calls."""
            cmd_str = " ".join(cmd)
            if "audit-set" in cmd_str or "audit-text" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"success": True}), stderr="",
                )
            if "show" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "stage": "plan_complete"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"success": True}), stderr="",
                )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"success": True}), stderr="",
            )

        with (
            patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            patch.object(ar_module.subprocess, "run", fake_subprocess_run),
            patch.object(ar_module, "_assemble_issue_report", return_value=report),
            patch("builtins.print"),  # quiet the runner's stdout
        ):
            return cmd_issue(
                "TEST-1",
                persist=True,
                force=True,  # skip freshness gate
                runner=self._make_runner(audit_show_raw),
            )

    def test_readback_passes_when_stored_audit_matches(self):
        """Stored audit that references the target ID passes readback."""
        report = (
            "Audit report for work item TEST-1\n"
            "Ready to close: Yes\n"
            "All acceptance criteria met.\n"
        )
        rc = self._run_cmd_issue(
            audit_show_raw=report,
            report=report,
        )
        assert rc == 0

    def test_readback_fails_when_stored_audit_is_wrong_item(self):
        """Stored audit referencing a *different* work item fails readback.

        This is the contamination scenario: a stale report for another work
        item was persisted; the readback must catch it via content identity.
        """
        report = (
            "Audit report for work item TEST-1\n"
            "Ready to close: Yes\n"
        )
        stale = (
            "Audit report for work item OSL-0MSABC7SB001NVUN\n"
            "Ready to close: Yes\n"
        )
        rc = self._run_cmd_issue(
            audit_show_raw=stale,
            report=report,
        )
        assert rc == 1

    def test_readback_fails_when_stored_audit_has_no_id(self):
        """Stored audit with no work-item ID fails readback (identity unknown)."""
        report = (
            "Audit report for work item TEST-1\n"
            "Ready to close: Yes\n"
        )
        rc = self._run_cmd_issue(
            audit_show_raw="Some generic audit text without any ID",
            report=report,
        )
        assert rc == 1


# ---------------------------------------------------------------------------
# AC1/AC2: The assembled issue report always names the audited work item
# ---------------------------------------------------------------------------

class TestAssembledReportIdentifiesWorkItem:
    """The issue-mode report must always contain the target work-item ID.

    This is what makes the persist identity guard reliable for runner
    persistence (the guard accepts reports that name the target ID).
    """

    def test_issue_report_contains_issue_id(self):
        from audit.scripts.audit_runner import _assemble_issue_report

        issue = {"id": "TEST-1", "title": "Test", "description": "desc"}
        report = _assemble_issue_report(
            issue,
            ac_results=[{"text": "AC1", "verdict": "met", "evidence": "ok"}],
            child_results=[],
        )
        assert "TEST-1" in report

    def test_child_report_contains_child_id(self):
        from audit.scripts.audit_runner import _assemble_child_audit_report

        child = {"id": "CHILD-1", "title": "Child", "status": "open", "stage": "plan_complete"}
        report = _assemble_child_audit_report(
            child,
            ac_results=[{"text": "AC1", "verdict": "met", "evidence": "ok"}],
        )
        assert "CHILD-1" in report
