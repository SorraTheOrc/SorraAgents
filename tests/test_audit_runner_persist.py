"""Tests for audit persistence robustness.

These tests verify that:
1. persist_audit.py correctly handles the --fail flag
2. audit_runner.py prints the report to stdout even when persistence fails
3. Exit codes are correct for success and failure cases

All tests use injectable runners to avoid real subprocess calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


# Ensure repo root is on path
REPO_ROOT = Path(__file__).resolve().parent / ".."
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts.audit_runner import cmd_issue  # noqa: E402
from skill.audit.scripts.persist_audit import persist_audit  # noqa: E402


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "audit"


def _load_fixture(name: str) -> dict:
    with open(FIXTURE_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_pi_result(ac_count: int = 3, verdict: str = "met") -> dict:
    """Build a mock Pi result that returns a valid JSON array for AC review."""
    items = []
    for i in range(ac_count):
        items.append({"index": i, "verdict": verdict, "evidence": f"file:{i}.py:1 — {verdict}"})
    return {"verdict": "met", "evidence": json.dumps(items), "extracted_text": json.dumps(items)}


# Fixture with children for testing child persistence
WI_WITH_CHILDREN = {
    "success": True,
    "workItem": {
        "id": "SA-PARENT",
        "title": "Parent work item",
        "description": "## Summary\nParent item.\n\n## Acceptance Criteria\n1. First AC.\n2. Second AC.",
        "status": "open",
        "stage": "in_progress"
    },
    "children": [
        {
            "id": "SA-CHILD-1",
            "title": "Child item 1",
            "description": "## Summary\nChild.\n\n## Acceptance Criteria\n1. Child AC.",
            "status": "open",
            "stage": "in_progress"
        }
    ]
}


# ---------------------------------------------------------------------------
# Test: persist_audit.py --fail flag
# ---------------------------------------------------------------------------

class TestPersistAuditFailFlag:
    """Tests for the --fail flag in persist_audit.py."""

    def test_persist_audit_fail_returns_1(self, monkeypatch):
        """When _fail is set, persist_audit returns 1."""
        report_text = "Ready to close: Yes\n\n## Summary\nTest passed."
        wl_calls = []

        def fake_runner(cmd, **kwargs):
            wl_calls.append(list(cmd))
            return _fake_proc(stdout='{"success": true}')

        rc = persist_audit("SA-TEST", report_text, wl_bin="wl", runner=fake_runner, _fail=True)
        assert rc == 1
        # wl should NOT have been called when --fail is set
        assert len(wl_calls) == 0

    def test_persist_audit_fail_prints_report_to_stdout(self, monkeypatch, capsys):
        """When _fail is set, the report text is printed to stdout."""
        report_text = "Ready to close: Yes\n\n## Summary\nTest passed."

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout='{"success": true}')

        rc = persist_audit("SA-TEST", report_text, wl_bin="wl", runner=fake_runner, _fail=True)
        assert rc == 1
        output = capsys.readouterr()
        assert report_text in output.out

    def test_persist_audit_normal_operation_unchanged(self, monkeypatch):
        """When _fail is NOT set, normal operation works."""
        report_text = "Ready to close: Yes"
        persist_calls = []

        def fake_runner(cmd, **kwargs):
            persist_calls.append(list(cmd))
            return _fake_proc(stdout='{"success": true}')

        rc = persist_audit("SA-TEST", report_text, wl_bin="wl", runner=fake_runner, _fail=False)
        assert rc == 0
        assert len(persist_calls) == 1
        assert "audit-set" in persist_calls[0]
        assert "SA-TEST" in persist_calls[0]

    def test_persist_audit_normal_failure_returns_1(self, monkeypatch):
        """When wl update fails normally, return 1."""
        report_text = "Ready to close: Yes"

        def fake_runner(cmd, **kwargs):
            return _fake_proc(returncode=1, stderr="wl error")

        rc = persist_audit("SA-TEST", report_text, wl_bin="wl", runner=fake_runner, _fail=False)
        assert rc == 1


# ---------------------------------------------------------------------------
# Test: audit_runner.py prints report even on persist failure
# ---------------------------------------------------------------------------

class TestAuditRunnerReportOnPersistFailure:
    """Tests that audit_runner.py prints the report to stdout even when
    persistence fails."""

    def test_report_printed_on_persist_failure(self, capsys, monkeypatch):
        """When persist_audit returns non-zero, the report is still printed."""

        def fake_persist(issue_id, report_text, **kwargs):
            return 1  # Simulate failure

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: _fake_pi_result(),
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")))

        rc = cmd_issue("SA-FAIL", runner=fake_runner)
        assert rc == 1  # Persist failed

        # But the report should still be in stdout
        captured = capsys.readouterr()
        assert "Ready to close:" in captured.out
        assert "## Summary" in captured.out
        assert "## Acceptance Criteria Status" in captured.out

    def test_report_printed_on_persist_success(self, capsys, monkeypatch):
        """When persist_audit succeeds, the report is printed."""
        def fake_persist(issue_id, report_text, **kwargs):
            return 0

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: _fake_pi_result(),
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")))

        rc = cmd_issue("SA-SUCCESS", runner=fake_runner, persist=True)
        assert rc == 0

        captured = capsys.readouterr()
        assert "Ready to close:" in captured.out

    def test_persist_true_calls_child_persist(self, capsys, monkeypatch):
        """When persist=True, child persist calls are made."""
        persist_calls = []

        def fake_persist(issue_id, report_text, **kwargs):
            persist_calls.append(issue_id)
            return 0

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: _fake_pi_result(ac_count=1),
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps(WI_WITH_CHILDREN))

        rc = cmd_issue("SA-PARENT", runner=fake_runner, persist=True)
        assert rc == 0
        # Parent and child both should be persisted
        assert "SA-PARENT" in persist_calls
        assert "SA-CHILD-1" in persist_calls
        assert len(persist_calls) == 2

    def test_no_persist_flag_skips_persist_call(self, capsys, monkeypatch):
        """When persist=False, no persist call is made and report is printed."""
        persist_called = []

        def fake_persist(issue_id, report_text, **kwargs):
            persist_called.append(True)
            return 0

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: _fake_pi_result(),
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")))

        rc = cmd_issue("SA-SKIP", runner=fake_runner, persist=False)
        assert rc == 0
        assert len(persist_called) == 0

        captured = capsys.readouterr()
        assert "Ready to close:" in captured.out

    def test_wl_failure_returns_1(self):
        """When wl show fails, cmd_issue returns 1."""
        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            # Let status lifecycle updates succeed
            if "--status" in cmd_list:
                return _fake_proc(stdout=json.dumps({"success": True}))
            return _fake_proc(returncode=1, stderr="work item not found")

        rc = cmd_issue("SA-MISSING", runner=fake_runner)
        assert rc == 1

    def test_no_persist_skips_child_persist_too(self, capsys, monkeypatch):
        """When persist=False, both parent and child persist calls are skipped."""
        persist_calls = []

        def fake_persist(issue_id, report_text, **kwargs):
            persist_calls.append(issue_id)
            return 0

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: _fake_pi_result(ac_count=1),
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps(WI_WITH_CHILDREN))

        rc = cmd_issue("SA-PARENT", runner=fake_runner, persist=False)
        assert rc == 0
        # Both parent and child persist should be skipped
        assert len(persist_calls) == 0, f"persist_audit called unexpectedly for: {persist_calls}"

        captured = capsys.readouterr()
        assert "Ready to close:" in captured.out


# ---------------------------------------------------------------------------
# Test: Exit codes are correct
# ---------------------------------------------------------------------------

class TestExitCodes:
    """Verify exit codes for various scenarios."""

    def test_success_returns_0(self, monkeypatch, capsys):
        def fake_persist(*a, **kw):
            return 0
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: _fake_pi_result(),
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")))

        rc = cmd_issue("SA-OK", runner=fake_runner)
        assert rc == 0

    def test_persist_failure_returns_1(self, monkeypatch, capsys):
        def fake_persist(*a, **kw):
            return 1
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: _fake_pi_result(),
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")))

        rc = cmd_issue("SA-FAIL", runner=fake_runner)
        assert rc == 1

    def test_wl_failure_returns_1(self):
        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            # Let status lifecycle updates succeed
            if "--status" in cmd_list:
                return _fake_proc(stdout=json.dumps({"success": True}))
            return _fake_proc(returncode=1, stderr="wl not found")
        rc = cmd_issue("SA-MISSING", runner=fake_runner)
        assert rc == 1
