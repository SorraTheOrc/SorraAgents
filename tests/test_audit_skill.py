import io
import json
import sys
from types import SimpleNamespace

from skill.audit.scripts.persist_audit import main as persist_main
from skill.audit.scripts.persist_audit import persist_audit


class FakeProc(SimpleNamespace):
    pass


def test_persist_audit_calls_wl_update_with_report():
    calls = []

    def _strip_flags(cmd):
        # persist_audit injects a resolved --worklog-dir <value> pair at
        # position 1 (SA-0MSKQERKH002IBLG); drop it before keying on the
        # wl subcommand tokens.
        if len(cmd) >= 3 and cmd[1] == "--worklog-dir":
            return cmd[:1] + cmd[3:]
        return cmd

    def fake_runner(cmd, check=False, text=True, capture_output=True):
        # record the command
        calls.append(list(cmd))
        return FakeProc(returncode=0, stdout=json.dumps({"success": True}), stderr="")

    rc = persist_audit("SA-TEST", "Ready to close: Yes\nDetails", runner=fake_runner)
    assert rc == 0
    # persist_audit now does four wl calls for a 'Ready to close: Yes'
    # report: show (priority check) + audit-set + show (stage) +
    # update --audit-text
    assert len(calls) == 4
    # first call: wl show for the priority check
    cmd0 = calls[0]
    assert _strip_flags(cmd0)[:3] == ["wl", "show", "SA-TEST"]
    assert "--json" in cmd0
    # second call: wl audit-set
    cmd = calls[1]
    assert _strip_flags(cmd)[:3] == ["wl", "audit-set", "SA-TEST"]
    assert "--ready-to-close" in cmd
    assert "yes" in cmd
    assert "--raw-output" in cmd
    raw_idx = cmd.index("--raw-output")
    assert cmd[raw_idx + 1] == "Ready to close: Yes\nDetails"
    assert cmd[-1] == "--json"
    # ensure wl show was invoked for stage preservation
    cmd2 = calls[2]
    assert _strip_flags(cmd2)[:3] == ["wl", "show", "SA-TEST"]
    assert "--json" in cmd2
    # ensure wl update --audit-text was invoked
    cmd3 = calls[3]
    assert _strip_flags(cmd3)[:3] == ["wl", "update", "SA-TEST"]
    assert "--audit-text" in cmd3
    assert cmd3[-1] == "--json"


def test_persist_audit_returns_nonzero_on_wl_failure():
    def fake_runner(cmd, check=False, text=True, capture_output=True):
        return FakeProc(returncode=1, stdout="", stderr="something went wrong")

    rc = persist_audit("SA-FAIL", "Ready to close: No\n", runner=fake_runner)
    assert rc != 0


def test_cli_reads_stdin_and_exits_zero(monkeypatch, capsys):
    # Patch subprocess.run used in the module
    def fake_runner(cmd, check=False, text=True, capture_output=True):
        return FakeProc(returncode=0, stdout=json.dumps({"success": True}), stderr="")

    monkeypatch.setattr("skill.audit.scripts.persist_audit.subprocess.run", fake_runner)

    # Simulate piped stdin
    monkeypatch.setattr(sys, "stdin", io.StringIO("Ready to close: Yes\nAll good"))

    rc = persist_main(["--issue-id", "SA-CLI"])
    assert rc == 0


def test_cli_errors_on_empty_input(monkeypatch):
    # Simulate empty stdin
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = persist_main(["--issue-id", "SA-EMPTY"])
    assert rc != 0
