"""Tests for the audit waiver helper (``skill/audit/scripts/audit_waiver.py``).

SA-0MVZNECSD005XB42 AC3 — an accepted false positive is recorded ONCE and then
triggers neither chore creation nor blocking. The worklog's ``auditWaiver``
column is a single-slot audit-gap waiver object with no CLI flag for a
per-finding list, so the waiver is recorded as an ``AUDIT-FP-WAIVER: <json>``
comment that the audit runner parses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner, audit_waiver


def _runner(stdout: str = '{"success": true}', returncode: int = 0):
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    _run.calls = calls
    return _run


class TestWaiverComment:
    def test_comment_round_trips_through_the_runner_parser(self):
        comment = audit_waiver.waiver_comment(
            "F401", "src/app.py", 12, "deliberate re-export", "Map",
        )
        assert comment.startswith(audit_runner.AUDIT_WAIVER_MARKER)
        parsed = audit_runner._parse_audit_waivers([{"comment": comment}])
        assert parsed == [{
            "linter": "ruff",
            "rule": "F401",
            "file": "src/app.py",
            "line": 12,
            "justification": "deliberate re-export",
            "author": "Map",
        }]

    def test_comment_without_line_is_a_file_level_waiver(self):
        comment = audit_waiver.waiver_comment("F401", "src/app.py", None, "ok")
        parsed = audit_runner._parse_audit_waivers([{"comment": comment}])
        assert parsed[0]["line"] == 0


class TestAddWaiver:
    def test_add_posts_a_marked_comment(self):
        runner = _runner()
        waiver = audit_waiver.add_waiver(
            "SA-1", rule="F401", file="src/app.py", line=12,
            justification="re-export", author="Map",
            worklog_dir="/owning/.worklog", runner=runner,
        )
        cmd = " ".join(runner.calls[0])
        assert cmd.startswith("wl ")
        assert "comment add SA-1" in cmd
        assert audit_runner.AUDIT_WAIVER_MARKER in cmd
        assert "--worklog-dir /owning/.worklog" in cmd
        assert "--author Map" in cmd
        assert "--json" in cmd
        assert waiver["rule"] == "F401"
        assert waiver["line"] == 12

    def test_add_requires_rule_and_file(self):
        with pytest.raises(ValueError):
            audit_waiver.add_waiver("SA-1", rule="", file="src/app.py",
                                    runner=_runner())

    def test_add_surfaces_a_wl_failure(self):
        runner = _runner(stdout="", returncode=1)
        with pytest.raises(RuntimeError):
            audit_waiver.add_waiver("SA-1", rule="F401", file="src/app.py",
                                    runner=runner)


class TestListWaivers:
    def test_list_parses_the_items_comments(self):
        comment = audit_waiver.waiver_comment("F401", "src/app.py", 3, "fine")
        payload = json.dumps({
            "success": True,
            "workItem": {"id": "SA-1"},
            "comments": [{"comment": comment}, {"comment": "unrelated"}],
        })
        waivers = audit_waiver.list_waivers("SA-1", runner=_runner(payload))
        assert [(w["rule"], w["file"], w["line"]) for w in waivers] == [
            ("F401", "src/app.py", 3),
        ]

    def test_list_returns_empty_without_waivers(self):
        payload = json.dumps({"success": True, "comments": []})
        assert audit_waiver.list_waivers("SA-1", runner=_runner(payload)) == []


class TestCli:
    def test_add_emits_json_and_exits_zero(self, capsys, monkeypatch):
        runner = _runner()
        monkeypatch.setattr(audit_waiver, "_default_runner", runner)
        rc = audit_waiver.main([
            "add", "SA-1", "--rule", "F401", "--file", "src/app.py",
            "--line", "7", "--justification", "fine", "--json",
        ])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is True
        assert out["waiver"]["line"] == 7

    def test_list_emits_json_and_exits_zero(self, capsys, monkeypatch):
        comment = audit_waiver.waiver_comment("F401", "src/app.py", 7, "fine")
        payload = json.dumps({"success": True, "comments": [{"comment": comment}]})
        monkeypatch.setattr(audit_waiver, "_default_runner", _runner(payload))
        rc = audit_waiver.main(["list", "SA-1", "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["waivers"][0]["rule"] == "F401"

    def test_wl_failure_exits_non_zero_with_json_error(self, capsys, monkeypatch):
        monkeypatch.setattr(
            audit_waiver, "_default_runner", _runner(stdout="", returncode=2),
        )
        rc = audit_waiver.main(["list", "SA-1", "--json"])
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["success"] is False
