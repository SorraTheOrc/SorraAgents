"""Unit tests for the read-only ``check-freshness`` subcommand (SA-0MUOO5W8J001DYTI).

The ship release gates call ``audit_runner.py check-freshness <id> --json`` to
consume the audit runner's persisted content fingerprint without duplicating
the fingerprint logic. These tests exercise the CLI hermetically: the runner
and the underlying freshness gate are mocked, so no live ``wl``/git/model call
is made.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner

# An explicit worklog dir short-circuits prefix-to-sibling resolution so the
# mocked runner is the only thing invoked.
_WORKLOG_DIR = "/tmp/wl-check-freshness-nonexistent"


def _completed(stdout: str, returncode: int = 0):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr="")


def _audit_show_payload(raw_output: str | None) -> str:
    audit = None
    if raw_output is not None:
        audit = {"auditedAt": "2026-09-04T10:00:00Z", "rawOutput": raw_output}
    return json.dumps({"success": True, "workItemId": "SA-1", "audit": audit})


def test_check_freshness_subcommand_registered():
    args = audit_runner.build_parser().parse_args(["check-freshness", "SA-1"])
    assert args.command == "check-freshness"
    assert args.issue_id == "SA-1"


def test_check_freshness_reports_content_fresh(capsys):
    raw = "Ready to close: Yes\nAudit content fingerprint: abc123\n"
    runner = lambda cmd: _completed(_audit_show_payload(raw))
    with mock.patch.object(audit_runner, "_default_runner", runner), \
         mock.patch.object(audit_runner, "_check_audit_freshness", return_value=raw):
        rc = audit_runner.cmd_check_freshness("SA-1", worklog_dir=_WORKLOG_DIR)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fresh"] is True
    assert out["hasFingerprint"] is True
    assert out["reason"] == "content fingerprint unchanged"
    assert out["auditedAt"] == "2026-09-04T10:00:00Z"


def test_check_freshness_reports_content_changed(capsys):
    raw = "Ready to close: Yes\nAudit content fingerprint: abc123\n"
    runner = lambda cmd: _completed(_audit_show_payload(raw))
    with mock.patch.object(audit_runner, "_default_runner", runner), \
         mock.patch.object(audit_runner, "_check_audit_freshness", return_value=None):
        audit_runner.cmd_check_freshness("SA-1", worklog_dir=_WORKLOG_DIR)

    out = json.loads(capsys.readouterr().out)
    assert out["fresh"] is False
    assert out["hasFingerprint"] is True
    assert out["reason"] == "content changed"


def test_check_freshness_reports_no_stored_audit(capsys):
    runner = lambda cmd: _completed(_audit_show_payload(None))
    with mock.patch.object(audit_runner, "_default_runner", runner), \
         mock.patch.object(audit_runner, "_check_audit_freshness", return_value=None):
        audit_runner.cmd_check_freshness("SA-1", worklog_dir=_WORKLOG_DIR)

    out = json.loads(capsys.readouterr().out)
    assert out["fresh"] is False
    assert out["hasFingerprint"] is False
    assert out["reason"] == "no stored audit"
    assert out["auditedAt"] is None


def test_check_freshness_fails_open_on_lookup_error(capsys):
    runner = lambda cmd: _completed("boom", returncode=1)
    with mock.patch.object(audit_runner, "_default_runner", runner):
        rc = audit_runner.cmd_check_freshness("SA-1", worklog_dir=_WORKLOG_DIR)

    # Always exits 0: a lookup failure is reported, not raised, so the ship
    # gate can fall back to the conservative time gate.
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fresh"] is False
    assert "freshness check failed" in out["reason"]
