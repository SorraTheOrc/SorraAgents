"""Tests for the audit runner core (F1).

These tests pin the CLI shape, ``wl`` invocation, AC extraction, and
persistence delegation of ``skill/audit/scripts/audit_runner.py``.

They were written *before* the implementation (F3) so that the implementation
is driven by a precise contract rather than being inferred from prose.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skill.audit.scripts.audit_runner import (
    build_parser,
    cmd_issue,
    cmd_project,
    main,
    _extract_acs,
    _extract_json_array,
    _run_wl,
    _load_config,
    _resolve_model_for_phase,
    _normalize_model_source,
    _deep_merge,
    DEFAULT_MODEL,
    DEFAULT_MODEL_SOURCE,
)


# Path to the audit_runner.py source file
AUDIT_RUNNER_PY = Path(__file__).resolve().parent.parent / "skill" / "audit" / "scripts" / "audit_runner.py"

# ---------------------------------------------------------------------------
# Fixtures directory
# ---------------------------------------------------------------------------
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "audit"


def _load_fixture(name: str) -> dict:
    """Load a JSON fixture from tests/fixtures/audit/."""
    with open(FIXTURE_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# CLI parsing tests
# ---------------------------------------------------------------------------

class TestCLIParsing:
    """Assert that the CLI subcommands exist and parse the expected flags."""

    def test_issue_subcommand_exists(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.command == "issue"
        assert args.issue_id == "SA-123"

    def test_issue_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.do_not_persist is False
        assert args.pi_bin == "pi"
        assert args.model is None
        assert args.model_source == DEFAULT_MODEL_SOURCE

    def test_issue_do_not_persist_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--do-not-persist"])
        assert args.do_not_persist is True

    def test_issue_pi_bin_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--pi-bin", "/usr/local/bin/pi"])
        assert args.pi_bin == "/usr/local/bin/pi"

    def test_issue_model_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--model", "custom/model"])
        assert args.model == "custom/model"

    def test_issue_debug_log_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--debug-log", "/tmp/audit.log"])
        assert args.debug_log == "/tmp/audit.log"

    def test_project_subcommand_exists(self):
        parser = build_parser()
        args = parser.parse_args(["project"])
        assert args.command == "project"

    def test_project_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["project"])
        assert args.pi_bin == "pi"
        assert args.model is None
        assert args.model_source == DEFAULT_MODEL_SOURCE

    def test_project_pi_bin_flag(self):
        parser = build_parser()
        args = parser.parse_args(["project", "--pi-bin", "/opt/pi"])
        assert args.pi_bin == "/opt/pi"

    def test_project_model_flag(self):
        parser = build_parser()
        args = parser.parse_args(["project", "--model", "other/model"])
        assert args.model == "other/model"

    def test_project_debug_log_flag(self):
        parser = build_parser()
        args = parser.parse_args(["project", "--debug-log", "/tmp/audit.log"])
        assert args.debug_log == "/tmp/audit.log"

    def test_no_subcommand_returns_2(self):
        rc = main([])
        assert rc == 2

    def test_no_subcommand_via_main(self):
        rc = main([])
        assert rc == 2

    def test_issue_model_source_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--model-source", "remote"])
        assert args.model_source == "remote"

    def test_issue_model_source_default_is_local(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.model_source == "local"

    def test_project_model_source_flag(self):
        parser = build_parser()
        args = parser.parse_args(["project", "--model-source", "remote"])
        assert args.model_source == "remote"

    def test_project_model_source_default_is_local(self):
        parser = build_parser()
        args = parser.parse_args(["project"])
        assert args.model_source == "local"


# ---------------------------------------------------------------------------
# _run_wl tests
# ---------------------------------------------------------------------------

class TestRunWl:
    """Fake ``subprocess.run`` for ``wl show --children --json`` and
    ``wl dep list --json`` and assert exact argv + JSON-decoding behaviour."""

    def test_run_wl_success(self):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            return _fake_proc(stdout='{"success": true}')

        result = _run_wl(fake_runner, ["wl", "show", "SA-123", "--children", "--json"])
        assert result == {"success": True}
        assert calls == [["wl", "show", "SA-123", "--children", "--json"]]

    def test_run_wl_nonzero_exit_raises(self):
        def fake_runner(cmd, **kwargs):
            return _fake_proc(returncode=1, stderr="not found")

        with pytest.raises(RuntimeError, match="wl command failed"):
            _run_wl(fake_runner, ["wl", "show", "SA-NOEXIST", "--json"])

    def test_run_wl_invalid_json_raises(self):
        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout="not json")

        with pytest.raises(RuntimeError, match="Invalid JSON"):
            _run_wl(fake_runner, ["wl", "dep", "list", "SA-123", "--json"])

    def test_run_wl_dep_list(self):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(list(cmd))
            return _fake_proc(stdout="[]")

        result = _run_wl(fake_runner, ["wl", "dep", "list", "SA-123", "--json"])
        assert result == []
        assert calls == [["wl", "dep", "list", "SA-123", "--json"]]


# ---------------------------------------------------------------------------
# Acceptance-criteria extraction tests
# ---------------------------------------------------------------------------

class TestExtractJsonArray:
    """Tests for _extract_json_array helper that extracts JSON array from mixed text."""

    def test_extracts_json_array_from_end_of_text(self):
        text = (
            "Here is my analysis:\n\n"
            "1. Criterion 1 is met because...\n"
            "2. Criterion 2 is met because...\n\n"
            '```json\n[\n  {"index": 0, "verdict": "met", "evidence": "file.py:10"},\n'
            '  {"index": 1, "verdict": "met", "evidence": "file.py:20"}\n]\n```'
        )
        result = _extract_json_array(text)
        assert result is not None
        assert len(result) == 2
        assert result[0]["index"] == 0
        assert result[0]["verdict"] == "met"
        assert result[1]["index"] == 1

    def test_extracts_json_array_without_code_fences(self):
        text = (
            "Analysis complete.\n\n"
            "All criteria are met.\n\n"
            '[{"index": 0, "verdict": "met", "evidence": "x:1"}]'
        )
        result = _extract_json_array(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["verdict"] == "met"

    def test_returns_none_for_empty_text(self):
        assert _extract_json_array("") is None
        assert _extract_json_array(None) is None

    def test_returns_none_for_text_without_json(self):
        text = "This is just plain text with no JSON."
        assert _extract_json_array(text) is None

    def test_returns_none_for_invalid_json(self):
        text = "Some text [not valid json]"
        assert _extract_json_array(text) is None

    def test_handles_nested_brackets_in_json(self):
        text = (
            "Analysis:\n\n"
            '[{"index": 0, "verdict": "met", "evidence": "code with [brackets]"}]'
        )
        result = _extract_json_array(text)
        assert result is not None
        assert len(result) == 1

    def test_handles_string_with_brackets(self):
        text = (
            "Analysis:\n\n"
            '[{"index": 0, "verdict": "met", "evidence": "arr[0] = x"}]'
        )
        result = _extract_json_array(text)
        assert result is not None
        assert result[0]["evidence"] == "arr[0] = x"

    def test_prefers_last_json_array(self):
        text = (
            "First mention: [1, 2, 3]\n\n"
            "Real result:\n"
            '[{"index": 0, "verdict": "met"}]'
        )
        result = _extract_json_array(text)
        assert result is not None
        assert len(result) == 1
        assert result[0]["verdict"] == "met"

    def test_handles_array_of_strings(self):
        text = 'Result: ["a", "b", "c"]'
        result = _extract_json_array(text)
        assert result == ["a", "b", "c"]

    def test_handles_array_of_numbers(self):
        text = "Result: [1, 2, 3]"
        result = _extract_json_array(text)
        assert result == [1, 2, 3]

    def test_handles_empty_array(self):
        text = "Result: []"
        result = _extract_json_array(text)
        assert result == []


class TestExtractACs:
    """AC extraction from both ``## Acceptance Criteria`` and
    ``### Acceptance Criteria`` headings, with numbered and bulleted variants."""

    def test_numbered_ac_under_h2(self):
        desc = _load_fixture("wi_with_numbered_ac.json")["workItem"]["description"]
        acs = _extract_acs(desc)
        assert len(acs) == 3
        assert "The system must handle user authentication." in acs[0]
        assert "The system must log all access attempts." in acs[1]
        assert "The system must support role-based access control." in acs[2]

    def test_bulleted_ac_under_h2(self):
        desc = _load_fixture("wi_with_bulleted_ac.json")["workItem"]["description"]
        acs = _extract_acs(desc)
        assert len(acs) == 3
        assert acs[0] == "The API must return 200 for valid requests."
        assert acs[1] == "The API must return 400 for malformed input."
        assert acs[2] == "The API must return 500 for internal errors."

    def test_numbered_ac_under_h3(self):
        desc = _load_fixture("wi_with_h3_ac.json")["workItem"]["description"]
        acs = _extract_acs(desc)
        assert len(acs) == 2
        assert "The cache must invalidate after TTL expiry." in acs[0]
        assert "The cache must support distributed locking." in acs[1]

    def test_no_ac_section(self):
        desc = _load_fixture("wi_without_ac.json")["workItem"]["description"]
        acs = _extract_acs(desc)
        assert acs == ["No acceptance criteria defined."]

    def test_no_ac_section_empty_description(self):
        acs = _extract_acs("")
        assert acs == ["No acceptance criteria defined."]

    def test_bulleted_with_asterisk(self):
        desc = (
            "## Summary\n\n## Acceptance Criteria\n"
            "* First criterion\n* Second criterion\n\n## Other\n"
        )
        acs = _extract_acs(desc)
        assert acs == ["First criterion", "Second criterion"]

    def test_stops_at_next_heading(self):
        desc = (
            "## Acceptance Criteria\n"
            "1. Must do X\n2. Must do Y\n\n## Implementation\n"
            "Some implementation details.\n"
        )
        acs = _extract_acs(desc)
        assert acs == ["Must do X", "Must do Y"]

    def test_success_criteria_synonym(self):
        desc = (
            "## Summary\n\n## Success Criteria\n"
            "1. Must be fast\n\n## Notes\n"
        )
        acs = _extract_acs(desc)
        assert acs == ["Must be fast"]


# ---------------------------------------------------------------------------
# Persistence delegation tests
# ---------------------------------------------------------------------------

class TestPersistenceDelegation:
    """Assert default persistence delegates to ``persist_audit`` rather than
    duplicating the ``wl update --audit-text`` call."""

    def test_default_persist_delegates_to_persist_audit(self, monkeypatch):
        persisted = {}

        def fake_persist(issue_id, report_text, **kwargs):
            persisted["issue_id"] = issue_id
            persisted["report_text"] = report_text
            return 0

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        rc = cmd_issue("SA-TEST-001", runner=fake_runner)
        assert rc == 0
        assert persisted["issue_id"] == "SA-TEST-001"
        assert "Ready to close:" in persisted["report_text"]
        assert "## Acceptance Criteria Status" in persisted["report_text"]

    def test_do_not_persist_returns_zero(self, monkeypatch):
        called = {"persist": False}

        def fake_persist(issue_id, report_text, **kwargs):
            called["persist"] = True
            return 0

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        rc = cmd_issue("SA-TEST-002", persist=False, runner=fake_runner)
        assert rc == 0
        assert called["persist"] is False

    def test_persist_propagates_nonzero_from_persist_audit(self, monkeypatch):
        def fake_persist(issue_id, report_text, **kwargs):
            return 1

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.persist_audit",
            fake_persist,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_without_ac.json")),
            )

        rc = cmd_issue("SA-FAIL", persist=True, runner=fake_runner)
        assert rc == 1


# ---------------------------------------------------------------------------
# Report structure tests (issue mode)
# ---------------------------------------------------------------------------

class TestReportStructure:
    """Validate the assembled report format for issue mode."""

    def test_report_starts_with_ready_to_close(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-STRUCT", runner=fake_runner, persist=False)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close:")

    def test_report_contains_section_headings(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-STRUCT", runner=fake_runner, persist=False)
        captured = capsys.readouterr()
        assert "## Summary" in captured.out
        assert "## Acceptance Criteria Status" in captured.out
        assert "## Children Status" in captured.out

    def test_report_contains_ac_table(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_bulleted_ac.json")),
            )

        cmd_issue("SA-STRUCT", runner=fake_runner, persist=False)
        captured = capsys.readouterr()
        assert "| # | Criterion | Verdict | Evidence |" in captured.out
        assert "The API must return 200 for valid requests." in captured.out
        assert "unmet" in captured.out

    def test_report_no_ac_fallback(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "unmet", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_without_ac.json")),
            )

        cmd_issue("SA-STRUCT", runner=fake_runner, persist=False)
        captured = capsys.readouterr()
        assert "No acceptance criteria defined." in captured.out


# ---------------------------------------------------------------------------
# Debug logging tests
# ---------------------------------------------------------------------------

class TestDebugLogging:
    """Verify audit runner debug log behavior."""

    def test_parse_failure_writes_default_debug_log(self, monkeypatch, tmp_path):
        log_path = tmp_path / "audit_debug.jsonl"

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {
                "verdict": "met",
                "evidence": "not-json",
                "raw_stdout": "RAW",
                "raw_stderr": "ERR",
                "extracted_text": "not-json",
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._default_debug_log_path",
            lambda issue_id, context: log_path,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-DEBUG", runner=fake_runner)
        assert log_path.exists()
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["reason"] == "parse_failure"
        assert entry["raw_stdout"] == "RAW"
        assert entry["raw_stderr"] == "ERR"
        assert entry["context"].startswith("parent")

    def test_debug_log_flag_writes_output(self, monkeypatch, tmp_path):
        log_path = tmp_path / "audit_debug.jsonl"

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {
                "verdict": "met",
                "evidence": json.dumps([
                    {"index": 0, "verdict": "met", "evidence": "x:1 — ok"},
                    {"index": 1, "verdict": "met", "evidence": "y:2 — ok"},
                    {"index": 2, "verdict": "met", "evidence": "z:3 — ok"},
                ]),
                "raw_stdout": "RAW",
                "raw_stderr": "",
                "extracted_text": "[]",
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-DEBUG", runner=fake_runner, debug_log=str(log_path))
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert lines
        entry = json.loads(lines[0])
        assert entry["reason"] == "debug_log"
        assert entry["raw_stdout"] == "RAW"


# ---------------------------------------------------------------------------
# Project-mode report tests
# ---------------------------------------------------------------------------

class TestProjectMode:
    """Validate project-mode report structure."""

    def test_project_report_starts_with_ready_to_close(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        cmd_project(runner=fake_runner)
        captured = capsys.readouterr()
        assert captured.out.startswith("Ready to close:")

    def test_project_report_has_summary_and_recommendation(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": ""},
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        cmd_project(runner=fake_runner)
        captured = capsys.readouterr()
        assert "## Summary" in captured.out
        assert "## Recommendation" in captured.out
        # Project mode should NOT have AC or children sections
        assert "## Acceptance Criteria Status" not in captured.out
        assert "## Children Status" not in captured.out


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------

class TestExitCodes:
    """Assert correct exit codes for various failure modes."""

    def test_issue_wl_failure_returns_1(self, capsys):
        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            # Let status updates succeed, only fail on wl show
            if "--status" in cmd_list:
                return _fake_proc(stdout=json.dumps({"success": True}))
            return _fake_proc(returncode=1, stderr="work item not found")

        rc = cmd_issue("SA-MISSING", runner=fake_runner)
        assert rc == 1

    def test_project_wl_failure_returns_1(self):
        def fake_runner(cmd, **kwargs):
            return _fake_proc(returncode=1, stderr="wl error")

        rc = cmd_project(runner=fake_runner)
        assert rc == 1

    def test_no_subcommand_returns_2(self):
        assert main([]) == 2


# ---------------------------------------------------------------------------
# Model resolution tests
# ---------------------------------------------------------------------------

class TestDeepMerge:
    """Test the deep-merge helper."""

    def test_deep_merge_simple(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_deep_merge_nested(self):
        base = {"model": {"remote": {"audit": "m1"}, "local": {"audit": "m2"}}}
        override = {"model": {"local": {"audit": "m3"}}}
        result = _deep_merge(base, override)
        assert result["model"]["remote"]["audit"] == "m1"
        assert result["model"]["local"]["audit"] == "m3"

    def test_deep_merge_empty_override(self):
        base = {"a": 1}
        result = _deep_merge(base, {})
        assert result == {"a": 1}


class TestLoadConfig:
    """Test config loading with fallback."""

    def test_load_config_returns_dict(self):
        """_load_config must always return a dict, even when no config file exists."""
        config = _load_config()
        assert isinstance(config, dict)

    def test_load_config_has_model_key(self):
        """When CWD has a .ralph.json, its model config should be reflected."""
        config = _load_config()
        assert "model" in config
        assert isinstance(config["model"], dict)


class TestNormalizeModelSource:
    """Test model source normalization."""

    def test_normalize_remote(self):
        assert _normalize_model_source("remote") == "remote"

    def test_normalize_local(self):
        assert _normalize_model_source("local") == "local"

    def test_normalize_case_insensitive(self):
        assert _normalize_model_source("REMOTE") == "remote"

    def test_normalize_unknown_falls_back_to_local(self):
        assert _normalize_model_source("unknown") == "local"

    def test_normalize_none_falls_back_to_local(self):
        assert _normalize_model_source(None) == "local"


class TestResolveModelForPhase:
    """Test model resolution with CLI override > config > default."""

    def test_cli_model_overrides_config(self):
        """Explicit --model flag overrides everything."""
        config = {
            "model": {
                "local": {"audit": "config-local-model"},
                "remote": {"audit": "config-remote-model"},
            }
        }
        model = _resolve_model_for_phase("audit", config, "local", cli_model="cli-override")
        assert model == "cli-override"

    def test_config_model_with_local_source(self):
        """With model_source=local, resolve the local variant from config."""
        config = {
            "model": {
                "local": {"audit": "local-model"},
                "remote": {"audit": "remote-model"},
            }
        }
        model = _resolve_model_for_phase("audit", config, "local", cli_model=None)
        assert model == "local-model"

    def test_config_model_with_remote_source(self):
        """With model_source=remote, resolve the remote variant from config."""
        config = {
            "model": {
                "local": {"audit": "local-model"},
                "remote": {"audit": "remote-model"},
            }
        }
        model = _resolve_model_for_phase("audit", config, "remote", cli_model=None)
        assert model == "remote-model"

    def test_fallback_to_default_when_no_config(self):
        """When config has no model.audit for the given source, fall back to DEFAULT_MODEL."""
        config = {}
        model = _resolve_model_for_phase("audit", config, "local", cli_model=None)
        assert model == DEFAULT_MODEL

    def test_fallback_to_default_when_config_missing_audit_key(self):
        """When config has model but no audit key for the source, fall back."""
        config = {
            "model": {
                "local": {"intake": "intake-model"},
            }
        }
        model = _resolve_model_for_phase("audit", config, "local", cli_model=None)
        assert model == DEFAULT_MODEL

    def test_config_model_flat_string(self):
        """When model.audit is a flat string (not source-mapped), it's used directly."""
        config = {
            "model": {
                "audit": "flat-audit-model",
            }
        }
        model = _resolve_model_for_phase("audit", config, "local", cli_model=None)
        assert model == "flat-audit-model"


class TestCmdIssueModelResolution:
    """Integration: cmd_issue should resolve the model from config based on model_source."""

    def test_cmd_issue_passes_resolved_model_to_pi(self, monkeypatch):
        """cmd_issue should resolve model from config+model_source and pass to _call_pi."""
        captured = {"model": None}

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            captured["model"] = model
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        # Fake the config loading to return a known model config
        def fake_load_config():
            return {
                "model": {
                    "local": {"audit": "from-config-local"},
                    "remote": {"audit": "from-config-remote"},
                }
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._load_config",
            fake_load_config,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-MODEL", runner=fake_runner, model_source="local", persist=False)
        assert captured["model"] == "from-config-local"

    def test_cmd_issue_cli_model_overrides_config(self, monkeypatch):
        """Explicit --model should override config even when model_source differs."""
        captured = {"model": None}

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            captured["model"] = model
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_load_config():
            return {
                "model": {
                    "local": {"audit": "from-config"},
                }
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._load_config",
            fake_load_config,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-MODEL", runner=fake_runner, model="cli-override", model_source="local", persist=False)
        assert captured["model"] == "cli-override"

    def test_cmd_project_passes_resolved_model_to_pi(self, monkeypatch):
        """cmd_project should also resolve model from config+model_source."""
        captured = {"model": None}

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            captured["model"] = model
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_load_config():
            return {
                "model": {
                    "remote": {"audit": "remote-audit-model"},
                }
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._load_config",
            fake_load_config,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        cmd_project(runner=fake_runner, model_source="remote")
        assert captured["model"] == "remote-audit-model"


# ---------------------------------------------------------------------------
# Pi prompt safety instruction tests
# ---------------------------------------------------------------------------

class TestPiPromptSafetyInstructions:
    """Assert that all Pi invocation prompts in audit_runner.py contain
    safety instructions to prevent models from modifying work items."""

    SOURCE = AUDIT_RUNNER_PY.read_text(encoding="utf-8")

    def test_parent_ac_prompt_has_read_only_designation(self):
        """Parent AC review prompt must contain [READ-ONLY AUDIT]."""
        assert "[READ-ONLY AUDIT]" in self.SOURCE

    def test_parent_ac_prompt_has_prohibition(self):
        """Parent AC review prompt must prohibit modifying work items."""
        assert "Do NOT close, modify, create, or delete any work items" in self.SOURCE

    def test_parent_ac_prompt_has_no_wl_git_commands(self):
        """Parent AC review prompt must prohibit wl/git state-modifying commands."""
        assert "Do NOT execute any wl, git, or other state-modifying commands" in self.SOURCE

    def test_child_ac_prompt_has_read_only_designation(self):
        """Child AC review prompt must contain [READ-ONLY AUDIT]."""
        # Count occurrences: at least 2 (parent + child) or all 3 prompts
        count = self.SOURCE.count("[READ-ONLY AUDIT]")
        assert count >= 2, f"Expected at least 2 [READ-ONLY AUDIT] occurrences, found {count}"

    def test_child_ac_prompt_has_structured_array_instruction(self):
        """Child AC review prompt must instruct to return structured JSON array."""
        assert "Return ONLY a structured JSON array" in self.SOURCE

    def test_prompt_has_adjusted_verdict_option(self):
        """Both parent and child prompts must include 'adjusted' as a valid verdict."""
        assert "adjusted" in self.SOURCE
        # The verdict enumeration must include adjusted
        assert "one of: met, unmet, partial, adjusted" in self.SOURCE

    def test_project_prompt_has_read_only_designation(self):
        """Project summary prompt must contain [READ-ONLY AUDIT]."""
        count = self.SOURCE.count("[READ-ONLY AUDIT]")
        assert count >= 3, f"Expected at least 3 [READ-ONLY AUDIT] occurrences (parent, child, project), found {count}"

    def test_project_prompt_has_structured_object_instruction(self):
        """Project summary prompt must instruct to return structured JSON object."""
        assert "Return ONLY a structured JSON object" in self.SOURCE


# ---------------------------------------------------------------------------
# Status lifecycle tests
# ---------------------------------------------------------------------------

class TestStatusLifecycle:
    """Verify that cmd_issue manages work item status (in_progress -> completed)."""

    def _fake_runner_with_calls(self, calls: list, fail_show: bool = False):
        """Create a fake runner that records calls and optionally fails on ``wl show``."""
        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            calls.append(cmd_list)
            # If fail_show is True and this is a "wl show" call, return failure
            if fail_show and "show" in cmd_list:
                return _fake_proc(returncode=1, stderr="wl: work item not found")
            # All other calls succeed with valid JSON
            return _fake_proc(stdout=json.dumps({"success": True}))
        return fake_runner

    def test_sets_in_progress_before_audit(self, monkeypatch):
        """in_progress status must be set before wl show (first operation)."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": "ok"},
        )

        cmd_issue("SA-LIFECYCLE", runner=self._fake_runner_with_calls(calls), persist=False)

        # The first wl update call should be for status in_progress
        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-LIFECYCLE"]]
        assert len(wl_updates) >= 1, f"Expected at least one wl update call, got: {calls}"
        assert wl_updates[0][:5] == ["wl", "update", "SA-LIFECYCLE", "--status", "in_progress"], (
            f"First update should be in_progress, got: {wl_updates[0]}"
        )

    def test_sets_completed_after_audit(self, monkeypatch):
        """completed status must be set at the end of a successful audit."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": "ok"},
        )

        cmd_issue("SA-LIFECYCLE", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-LIFECYCLE"]]
        completed_updates = [c for c in wl_updates if c[3:5] == ["--status", "completed"]]
        assert len(completed_updates) >= 1, (
            f"Expected at least one completed status update, got: {wl_updates}"
        )

    def test_sets_completed_on_wl_show_failure(self):
        """completed status must be set even when wl show fails."""
        calls = []

        rc = cmd_issue("SA-FAIL", runner=self._fake_runner_with_calls(calls, fail_show=True), persist=False)
        assert rc == 1, f"Expected exit code 1 on wl show failure, got {rc}"

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-FAIL"]]
        completed_updates = [c for c in wl_updates if c[3:5] == ["--status", "completed"]]
        assert len(completed_updates) >= 1, (
            f"Expected completed update even on failure, got: {wl_updates}"
        )

    def test_in_progress_before_completed(self, monkeypatch):
        """in_progress must appear before completed in the call sequence."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": "ok"},
        )

        cmd_issue("SA-LIFECYCLE", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-LIFECYCLE"]]
        statuses = [" ".join(c[3:]) for c in wl_updates]
        in_progress_idx = next(i for i, s in enumerate(statuses) if "in_progress" in s)
        completed_idx = next(i for i, s in enumerate(statuses) if "completed" in s)
        assert in_progress_idx < completed_idx, (
            f"in_progress (index {in_progress_idx}) must come before completed (index {completed_idx}): {statuses}"
        )

    def test_sets_completed_on_exception_in_main_logic(self, monkeypatch):
        """completed must be set when an unhandled exception occurs in the main logic."""
        calls = []

        def fake_call_pi(prompt, model="x", pi_bin="x", **kwargs):
            raise RuntimeError("Pi crashed")

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        cmd_issue("SA-EXCEPT", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-EXCEPT"]]
        completed_updates = [c for c in wl_updates if c[3:5] == ["--status", "completed"]]
        assert len(completed_updates) >= 1, (
            f"Expected completed update after exception, got: {wl_updates}"
        )
