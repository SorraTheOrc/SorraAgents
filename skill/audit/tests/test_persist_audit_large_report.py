"""Regression tests for file-based persistence of large audit reports.

WL-0MSS55LFU00973S2: ``persist_audit`` passed the full report text inline on
the ``wl`` command line (``--raw-output`` / ``--audit-text``). A report large
enough to exceed the OS per-argument limit (``MAX_ARG_STRLEN``, 128 KiB on
Linux) made ``wl`` fail with ``OSError [Errno 7] Argument list too long``
and the audit verdict was never persisted. Reports over the inline size
limit are now written to a temporary file and handed over via
``--audit-file``; the temp file is removed once the call returns.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import persist_audit as persist_mod

ISSUE_ID = "ZZ-0MSS55LFU00973S2"
LARGE_REPORT = (
    f"Audit report for work item {ISSUE_ID}\n"
    "Ready to close: No\n"
    "### Code Quality\n\n"
    + ("x" * (70 * 1024))
    + "\n"
)
SMALL_REPORT = (
    f"Audit report for work item {ISSUE_ID}\n"
    "Ready to close: No\n"
    "All ACs met.\n"
)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _RecordingRunner:
    """Records every wl call and captures any ``--audit-file`` payload."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None, str | None]] = []

    def __call__(self, cmd, **kwargs):
        path = None
        content = None
        if "--audit-file" in cmd:
            path = cmd[cmd.index("--audit-file") + 1]
            content = Path(path).read_text(encoding="utf-8")
        self.calls.append((list(cmd), content, path))
        cmd_str = " ".join(cmd)
        if "audit-set" in cmd_str:
            return _proc(stdout='{"success": true}')
        if "show" in cmd_str:
            return _proc(stdout=json.dumps({
                "success": True,
                "workItem": {"id": ISSUE_ID, "priority": "high",
                             "stage": "plan_complete"},
            }))
        return _proc(stdout='{"success": true}')

    def audit_set_calls(self):
        return [c for c in self.calls if "audit-set" in " ".join(c[0])]

    def update_calls(self):
        return [c for c in self.calls if " update " in " " + " ".join(c[0]) + " "]


def _subcommands(runner: _RecordingRunner) -> list[str]:
    return [" ".join(c[0]) for c in runner.calls]


class TestLargeReportUsesAuditFile:
    def test_audit_set_passes_large_report_via_file(self):
        runner = _RecordingRunner()
        rc = persist_mod.persist_audit(
            ISSUE_ID, LARGE_REPORT, runner=runner, worklog_dir="/tmp/wl"
        )

        assert rc == 0
        calls = runner.audit_set_calls()
        assert calls, "expected a wl audit-set call"
        cmd, content, path = calls[0]
        assert "--raw-output" not in cmd
        assert "--audit-file" in cmd
        assert content == LARGE_REPORT
        assert path is not None

    def test_audit_text_update_passes_large_report_via_file(self):
        runner = _RecordingRunner()
        rc = persist_mod.persist_audit(
            ISSUE_ID, LARGE_REPORT, runner=runner, worklog_dir="/tmp/wl"
        )

        assert rc == 0
        calls = runner.update_calls()
        assert calls, "expected a wl update call"
        cmd, content, _path = calls[0]
        assert "--audit-text" not in cmd
        assert "--audit-file" in cmd
        assert content == LARGE_REPORT

    def test_small_report_still_uses_inline_flags(self):
        runner = _RecordingRunner()
        rc = persist_mod.persist_audit(
            ISSUE_ID, SMALL_REPORT, runner=runner, worklog_dir="/tmp/wl"
        )

        assert rc == 0
        audit_set = runner.audit_set_calls()[0][0]
        update = runner.update_calls()[0][0]
        assert "--raw-output" in audit_set
        assert "--audit-text" in update
        assert "--audit-file" not in audit_set
        assert "--audit-file" not in update

    def test_temp_file_is_removed_after_persist(self):
        runner = _RecordingRunner()
        rc = persist_mod.persist_audit(
            ISSUE_ID, LARGE_REPORT, runner=runner, worklog_dir="/tmp/wl"
        )

        assert rc == 0
        paths = [c[2] for c in runner.calls if c[2] is not None]
        assert paths, "expected at least one temp file"
        for path in paths:
            assert not Path(path).exists(), f"temp file not cleaned up: {path}"

    def test_env_override_lowers_threshold(self, monkeypatch):
        monkeypatch.setenv(persist_mod.PERSIST_INLINE_SIZE_LIMIT_ENV, "10")
        runner = _RecordingRunner()
        rc = persist_mod.persist_audit(
            ISSUE_ID, SMALL_REPORT, runner=runner, worklog_dir="/tmp/wl"
        )

        assert rc == 0
        audit_set = runner.audit_set_calls()[0][0]
        assert "--audit-file" in audit_set
        assert "--raw-output" not in audit_set


class TestInlineSizeResolver:
    def test_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv(persist_mod.PERSIST_INLINE_SIZE_LIMIT_ENV, raising=False)
        assert (
            persist_mod._resolve_inline_size_limit()
            == persist_mod.PERSIST_INLINE_SIZE_LIMIT
        )

    def test_invalid_env_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv(persist_mod.PERSIST_INLINE_SIZE_LIMIT_ENV, "abc")
        assert (
            persist_mod._resolve_inline_size_limit()
            == persist_mod.PERSIST_INLINE_SIZE_LIMIT
        )
        assert "invalid AUDIT_PERSIST_INLINE_SIZE_LIMIT" in capsys.readouterr().err

    def test_negative_env_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv(persist_mod.PERSIST_INLINE_SIZE_LIMIT_ENV, "-5")
        assert (
            persist_mod._resolve_inline_size_limit()
            == persist_mod.PERSIST_INLINE_SIZE_LIMIT
        )
        assert "must be a positive integer" in capsys.readouterr().err

    def test_valid_env_is_respected(self, monkeypatch):
        monkeypatch.setenv(persist_mod.PERSIST_INLINE_SIZE_LIMIT_ENV, "123")
        assert persist_mod._resolve_inline_size_limit() == 123
