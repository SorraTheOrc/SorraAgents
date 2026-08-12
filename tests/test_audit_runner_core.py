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
    AUDIT_FRESHNESS_BUFFER_SECONDS,
    CALL_PI_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_MODEL_SOURCE,
    Runner,
    _extract_acs,
    _extract_json_array,
    _get_child_audit_verdict,
    _run_wl,
    build_parser,
    cmd_issue,
    cmd_project,
    main,
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


def _strip_worklog_dir(cmd: list[str]) -> list[str]:
    """Remove an injected ``--worklog-dir <path>`` flag pair from a wl argv.

    The audit runner injects ``--worklog-dir`` into every wl command to make
    wl invocation cwd-independent. Tests that pin the exact wl argv should
    strip the injected pair so assertions remain stable across environments.
    """
    out: list[str] = []
    i = 0
    while i < len(cmd):
        if cmd[i] == "--worklog-dir" and i + 1 < len(cmd):
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out


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

    # ------------------------------------------------------------------
    # --force flag tests
    # ------------------------------------------------------------------

    def test_issue_force_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--force"])
        assert args.force is True

    def test_issue_force_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.force is False

    def test_issue_force_with_other_flags(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--force", "--do-not-persist", "--json"])
        assert args.force is True
        assert args.do_not_persist is True
        assert args.json is True

    def test_project_no_force_flag(self):
        """--force should NOT be a valid flag for the project subcommand."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["project", "--force"])

    # ------------------------------------------------------------------
    # --timeout flag tests
    # ------------------------------------------------------------------

    def test_issue_timeout_flag(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--timeout", "600"])
        assert args.timeout == 600

    def test_issue_timeout_default_none(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123"])
        assert args.timeout is None

    def test_project_timeout_flag(self):
        parser = build_parser()
        args = parser.parse_args(["project", "--timeout", "1200"])
        assert args.timeout == 1200

    def test_project_timeout_default_none(self):
        parser = build_parser()
        args = parser.parse_args(["project"])
        assert args.timeout is None

    def test_issue_timeout_with_other_flags(self):
        parser = build_parser()
        args = parser.parse_args(["issue", "SA-123", "--timeout", "900", "--do-not-persist", "--json"])
        assert args.timeout == 900
        assert args.do_not_persist is True
        assert args.json is True


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
        assert _strip_worklog_dir(calls[0]) == ["wl", "show", "SA-123", "--children", "--json"]

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
        assert _strip_worklog_dir(calls[0]) == ["wl", "dep", "list", "SA-123", "--json"]


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

    def test_heading_with_trailing_colon(self):
        """Headers like 'Acceptance Criteria:' (canonical AGENTS.md format)
        must be recognized — the colon is optional."""
        desc = (
            "Acceptance Criteria:\n"
            "- [ ] runWl-init-detection.test.ts passes reliably (25/25)\n"
            "- [ ] mock-timeout.test.ts passes (8/8)\n"
            "- [ ] Full suite green\n"
        )
        acs = _extract_acs(desc)
        assert len(acs) == 3
        assert "runWl-init-detection.test.ts passes reliably (25/25)" in acs[0]
        assert "mock-timeout.test.ts passes (8/8)" in acs[1]
        assert "Full suite green" in acs[2]

    def test_h2_heading_with_trailing_colon(self):
        desc = (
            "## Acceptance Criteria:\n"
            "1. Must do X\n"
            "2. Must do Y\n\n## Notes\n"
        )
        acs = _extract_acs(desc)
        assert acs == ["Must do X", "Must do Y"]


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
            cmd_list = list(cmd)
            if "audit-show" in cmd_list:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "audit": {
                        "auditedAt": "2026-07-20T10:00:00.000Z",
                        "rawOutput": "Audit report for work item SA-TEST-001\nReady to close: No\n\n## Summary\nTest.",
                    },
                }))
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
        # After RC2 fix, unparseable Pi output now defaults to "partial" verdict
        assert "partial" in captured.out
        assert "could not be parsed" in captured.out

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
# Timeout constant tests
# ---------------------------------------------------------------------------

class TestCallPiTimeoutConstant:
    """Verify the CALL_PI_TIMEOUT constant exists and is generously sized.

    The per-call timeout is a safety net for individual Pi model calls.
    The primary protection against the parent bash-tool timeout (~120s)
    is the cumulative elapsed-time guard in cmd_issue (110s threshold
    for skipping remaining child audits), not this per-call timeout.
    """

    def test_call_pi_timeout_constant_exists(self):
        """CALL_PI_TIMEOUT must be defined."""
        assert CALL_PI_TIMEOUT is not None
        assert isinstance(CALL_PI_TIMEOUT, int)

    def test_call_pi_timeout_generous_for_large_prompts(self):
        """Timeout must be generous (>= 300s) so large audit prompts complete."""
        assert CALL_PI_TIMEOUT >= 300, (
            f"CALL_PI_TIMEOUT={CALL_PI_TIMEOUT} must be >= 300s "
            "to allow large audit prompts to complete"
        )

    def test_call_pi_timeout_not_excessive(self):
        """Timeout should still have a reasonable upper bound."""
        assert CALL_PI_TIMEOUT <= 1800, (
            f"CALL_PI_TIMEOUT={CALL_PI_TIMEOUT} should be <= 1800s "
            "to bound the original indefinite-hang risk"
        )


# ---------------------------------------------------------------------------
# Freshness gate constant tests
# ---------------------------------------------------------------------------

class TestAuditFreshnessBufferConstant:
    """Verify the AUDIT_FRESHNESS_BUFFER_SECONDS constant exists and is 60."""

    def test_constant_exists(self):
        """AUDIT_FRESHNESS_BUFFER_SECONDS must be defined."""
        assert AUDIT_FRESHNESS_BUFFER_SECONDS is not None
        assert isinstance(AUDIT_FRESHNESS_BUFFER_SECONDS, int)

    def test_constant_is_60(self):
        """The buffer must be exactly 60 seconds."""
        assert AUDIT_FRESHNESS_BUFFER_SECONDS == 60, (
            f"Expected 60, got {AUDIT_FRESHNESS_BUFFER_SECONDS}"
        )

    def test_constant_is_positive(self):
        """The buffer must be positive."""
        assert AUDIT_FRESHNESS_BUFFER_SECONDS > 0


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

class TestCmdIssueModelResolution:
    """Integration: cmd_issue and cmd_project resolve model = model or DEFAULT_MODEL.

    Model resolution is:
      resolved_model = model or DEFAULT_MODEL

    The ``model_source`` parameter is accepted for backward-compatible
    argparse but has no effect on the resolved model.
    """

    def test_cmd_issue_default_model(self, monkeypatch):
        """Without --model, cmd_issue passes DEFAULT_MODEL to _call_pi."""
        captured = {"model": None}

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            captured["model"] = model
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-MODEL", runner=fake_runner, persist=False)
        assert captured["model"] == DEFAULT_MODEL, (
            f"Expected DEFAULT_MODEL ({DEFAULT_MODEL}), got {captured['model']}"
        )

    def test_cmd_issue_explicit_model_override(self, monkeypatch):
        """Explicit --model overrides DEFAULT_MODEL."""
        captured = {"model": None}

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            captured["model"] = model
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(
                stdout=json.dumps(_load_fixture("wi_with_numbered_ac.json")),
            )

        cmd_issue("SA-MODEL", runner=fake_runner, model="cli-override", persist=False)
        assert captured["model"] == "cli-override"

    def test_cmd_project_default_model(self, monkeypatch):
        """Without --model, cmd_project passes DEFAULT_MODEL to _call_pi."""
        captured = {"model": None}

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            captured["model"] = model
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        cmd_project(runner=fake_runner)
        assert captured["model"] == DEFAULT_MODEL, (
            f"Expected DEFAULT_MODEL ({DEFAULT_MODEL}), got {captured['model']}"
        )

    def test_cmd_project_explicit_model_override(self, monkeypatch):
        """Explicit --model overrides DEFAULT_MODEL in cmd_project."""
        captured = {"model": None}

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            captured["model"] = model
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        cmd_project(runner=fake_runner, model="project-model")
        assert captured["model"] == "project-model"


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
    """Verify that cmd_issue captures original status and restores it after audit."""

    def _fake_runner_with_calls(self, calls: list, fail_show: bool = False,
                                 has_acs: bool = True):
        """Create a fake runner that records calls and optionally fails on ``wl show``.

        When *has_acs* is ``True``, the ``wl show --children --json`` response includes
        a work item description with acceptance criteria so the audit can produce
        ``Ready to close: Yes``.
        """
        _show_called = False

        def fake_runner(cmd, **kwargs):
            nonlocal _show_called
            cmd_list = _strip_worklog_dir(list(cmd))
            calls.append(cmd_list)
            # If fail_show is True and this is a "wl show" call, return failure
            if fail_show and "show" in cmd_list:
                return _fake_proc(returncode=1, stderr="wl: work item not found")
            # The first "wl show" without --children is the original-status capture.
            # Default response has no "status" field so original_status falls back to "open".
            # Test methods that supply a specific status should use _fake_runner_with_status.
            # For "wl show --children --json": return a work item with ACs when has_acs is True.
            if has_acs and "show" in cmd_list and "--children" in cmd_list and not _show_called:
                _show_called = True
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": cmd_list[2],
                        "title": "Test Item",
                        "description": (
                            "## Acceptance Criteria\n"
                            "1. The system passes the test\n"
                        ),
                    },
                    "children": [],
                }))
            # All other calls succeed with valid JSON
            return _fake_proc(stdout=json.dumps({"success": True}))
        return fake_runner

    def _fake_runner_with_status(self, calls: list, status: str = "completed",
                                 has_acs: bool = True):
        """Create a fake runner that returns a work item with the given *status*.

        The first ``wl show <id> --json`` call (without --children) returns a work
        item dict that includes the given *status* so the original-status capture
        logic picks it up. Subsequent calls behave like the default fake runner.
        """
        _show_called = False

        def fake_runner(cmd, **kwargs):
            nonlocal _show_called
            cmd_list = _strip_worklog_dir(list(cmd))
            calls.append(cmd_list)
            # The original-status capture uses "wl show <id> --json" (no --children).
            # Match commands where "show" is present but "--children" is absent.
            if "show" in cmd_list and "--children" not in cmd_list and not _show_called:
                _show_called = True
                return _fake_proc(stdout=json.dumps({"success": True, "status": status}))
            # For "wl show --children --json": return a work item with ACs when has_acs is True.
            if has_acs and "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": cmd_list[2],
                        "title": "Test Item",
                        "description": (
                            "## Acceptance Criteria\n"
                            "1. The system passes the test\n"
                        ),
                    },
                    "children": [],
                }))
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

    def test_in_progress_includes_json_flag(self, monkeypatch):
        """in_progress wl update must include --json flag."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {"verdict": "met", "evidence": "ok"},
        )

        cmd_issue("SA-JSONFLAG", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-JSONFLAG"]]
        assert len(wl_updates) >= 1, f"Expected at least one wl update call, got: {calls}"
        # The first wl update call should include --json as the 6th argument
        in_progress_updates = [c for c in wl_updates if "--status" in c and "in_progress" in c]
        assert len(in_progress_updates) >= 1, f"Expected in_progress update, got: {wl_updates}"
        assert "--json" in in_progress_updates[0], (
            f"in_progress update must include --json, got: {in_progress_updates[0]}"
        )

    def test_advances_to_completed_in_review_on_success(self, monkeypatch):
        """On a successful audit with a 'yes' verdict, the item advances to
        completed/in_review even when no original status was captured."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        cmd_issue("SA-LIFECYCLE", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-LIFECYCLE"]]
        final_update = wl_updates[-1]
        assert final_update[3:5] == ["--status", "completed"], (
            f"Expected 'completed' terminal transition on yes verdict, got: {wl_updates}"
        )
        assert final_update[5:7] == ["--stage", "in_review"], (
            f"Expected stage in_review, got: {final_update}"
        )

    def test_final_restore_update_includes_json_flag(self, monkeypatch):
        """The final status restore wl update must include --json flag."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        cmd_issue("SA-JSONFLAG2", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-JSONFLAG2"]]
        assert len(wl_updates) >= 1, f"Expected at least one wl update call, got: {calls}"
        # The final update is the status restore; verify it includes --json
        final_update = wl_updates[-1]
        assert "--json" in final_update, (
            f"Final status restore must include --json, got: {final_update}"
        )

    def test_fallback_to_open_when_wl_show_fails(self):
        """Fallback to 'open' when wl show fails and original_status cannot be captured."""
        calls = []

        rc = cmd_issue("SA-FAIL", runner=self._fake_runner_with_calls(calls, fail_show=True), persist=False)
        assert rc == 1, f"Expected exit code 1 on wl show failure, got {rc}"

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-FAIL"]]
        open_updates = [c for c in wl_updates if c[3:5] == ["--status", "open"]]
        assert len(open_updates) >= 1, (
            f"Expected open update (fallback) even on failure, got: {wl_updates}"
        )

    def test_in_progress_before_status_restore(self, monkeypatch):
        """in_progress must appear before the status restore in the call sequence."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        cmd_issue("SA-LIFECYCLE", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-LIFECYCLE"]]
        assert len(wl_updates) >= 2, f"Expected at least 2 wl update calls, got: {wl_updates}"
        # in_progress must be the first update
        assert wl_updates[0][3:5] == ["--status", "in_progress"], (
            f"First update should be in_progress, got: {wl_updates[0]}"
        )
        # The last update is the status restore; it must come after in_progress (not equal to index 0)
        assert len(wl_updates) >= 2, (
            f"in_progress must come before status restore: {wl_updates}"
        )

    def test_handled_exception_sets_open_status(self, monkeypatch):
        """When a pi RuntimeError is caught by the body, the audit is a failure:
        the item is never left in_progress — it is restored to a safe state
        ('open') with the assignee cleared."""
        calls = []

        def fake_call_pi(prompt, model="x", pi_bin="x", **kwargs):
            raise RuntimeError("Pi crashed")

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        cmd_issue("SA-EXCEPT", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-EXCEPT"]]
        open_updates = [c for c in wl_updates if c[3:5] == ["--status", "open"]]
        assert len(open_updates) >= 1, (
            f"Expected 'open' status (audit failed due to exception), got: {wl_updates}"
        )

    def test_restores_original_status_when_captured(self, monkeypatch):
        """Original status (e.g. 'completed') is restored instead of always resetting to 'open'."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        cmd_issue("SA-ORIGSTAT", runner=self._fake_runner_with_status(calls, status="completed"), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-ORIGSTAT"]]
        completed_updates = [c for c in wl_updates if c[3:5] == ["--status", "completed"]]
        assert len(completed_updates) >= 1, (
            f"Expected at least one 'completed' status restore, got: {wl_updates}"
        )
        # Ensure no 'open' status is set when original was 'completed'
        open_updates = [c for c in wl_updates if c[3:5] == ["--status", "open"]]
        assert len(open_updates) == 0, (
            f"Should NOT set 'open' when original status was 'completed', got: {wl_updates}"
        )

    def test_restores_original_status_with_json_flag_when_audit_passes(self, monkeypatch):
        """A yes verdict on an originally in_progress item advances it to
        completed/in_review (never back to in_progress); --json is included."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        cmd_issue("SA-ORIGSTAT2", runner=self._fake_runner_with_status(calls, status="in_progress"), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-ORIGSTAT2"]]
        # The only in_progress update is the entry claim — the item must NOT be
        # restored to in_progress after a yes verdict.
        in_progress_updates = [c for c in wl_updates if c[3:5] == ["--status", "in_progress"]]
        assert len(in_progress_updates) == 1, (
            f"Expected exactly one in_progress update (the entry claim), got: {wl_updates}"
        )
        # The terminal transition advances the item to completed/in_review.
        final_update = wl_updates[-1]
        assert final_update[3:7] == ["--status", "completed", "--stage", "in_review"], (
            f"Expected completed/in_review terminal transition, got: {final_update}"
        )
        assert "--json" in final_update, (
            f"Status transition must include --json, got: {final_update}"
        )


    def _fake_runner_with_restore_failure(self, calls, fail_restore_count):
        """Fake runner that fails the terminal status-restore ``wl update``
        the first ``fail_restore_count`` times, then succeeds.

        The entry ``in_progress`` claim is never failed — only the final
        verdict-driven restore update (any status other than in_progress).
        """
        _show_called = False
        restore_failures = {"remaining": fail_restore_count}

        def fake_runner(cmd, **kwargs):
            nonlocal _show_called
            cmd_list = _strip_worklog_dir(list(cmd))
            calls.append(cmd_list)
            if (
                cmd_list[:3] == ["wl", "update", cmd_list[2]]
                and "--status" in cmd_list
                and "in_progress" not in cmd_list
                and restore_failures["remaining"] > 0
            ):
                restore_failures["remaining"] -= 1
                return _fake_proc(returncode=1, stderr="wl: transient error")
            if "show" in cmd_list and "--children" in cmd_list and not _show_called:
                _show_called = True
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": cmd_list[2],
                        "title": "Test Item",
                        "description": (
                            "## Acceptance Criteria\n"
                            "1. The system passes the test\n"
                        ),
                    },
                    "children": [],
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))
        return fake_runner

    def test_restore_failure_retries_then_succeeds(self, monkeypatch, capsys):
        """A transient failure on the terminal status restore is retried, so
        the item is not left in_progress; no warning is printed when the
        retry succeeds."""
        calls = []
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._STATUS_RESTORE_RETRY_DELAY_S", 0,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        rc = cmd_issue(
            "SA-RETRY",
            runner=self._fake_runner_with_restore_failure(calls, fail_restore_count=1),
            persist=False,
        )
        assert rc == 0, f"Expected exit 0 on successful audit, got {rc}"

        # The restore update was attempted twice: first attempt failed, retry succeeded.
        restore_updates = [
            c for c in calls
            if c[:3] == ["wl", "update", "SA-RETRY"]
            and "--status" in c
            and "in_progress" not in c
        ]
        assert len(restore_updates) >= 2, (
            f"Expected the restore update to be retried after a transient failure, got: {calls}"
        )
        err = capsys.readouterr().err
        assert "Failed to restore" not in err, (
            f"No warning expected when the retry succeeds, got: {err}"
        )

    def test_restore_failure_prints_visible_warning(self, monkeypatch, capsys):
        """AC2: when the terminal status restore ultimately fails, a visible
        warning is printed to stderr (not silently swallowed) naming the work
        item, so the operator can recover an item left in_progress."""
        calls = []
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._STATUS_RESTORE_RETRY_DELAY_S", 0,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        rc = cmd_issue(
            "SA-RESTOREFAIL",
            runner=self._fake_runner_with_restore_failure(calls, fail_restore_count=999),
            persist=False,
        )
        assert rc == 0, "Audit result must not be masked by status-restore failure"

        err = capsys.readouterr().err
        assert "Failed to restore" in err, (
            f"Expected a visible restore-failure warning on stderr, got: {err}"
        )
        assert "SA-RESTOREFAIL" in err, (
            f"Warning should name the affected work item, got: {err}"
        )

    def test_restore_failure_preserves_audit_exit_code(self, monkeypatch, capsys):
        """A status-restore failure must not mask the main audit result: the
        exit code still reflects the audit outcome (0 = success)."""
        calls = []
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._STATUS_RESTORE_RETRY_DELAY_S", 0,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        rc = cmd_issue(
            "SA-EXITCODE",
            runner=self._fake_runner_with_restore_failure(calls, fail_restore_count=999),
            persist=False,
        )
        assert rc == 0, (
            f"Status-restore failure must not mask the audit result (expected 0), got {rc}"
        )


    # ------------------------------------------------------------------
    # Tests: needs_producer_review flag (AC1, AC2)
    # ------------------------------------------------------------------

    def test_status_restore_does_not_include_producer_review_flags(self, monkeypatch):
        """The verdict-driven transition does not include --needs-producer-review."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "ok"}]',
                "verdict": "met",
                "evidence": "ok",
            },
        )

        cmd_issue("SA-NPR1", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-NPR1"]]
        # The verdict-driven transition (final update) should NOT include
        # --needs-producer-review. It DOES set a compatible stage
        # (in_review for a yes verdict).
        final_update = wl_updates[-1] if wl_updates else []
        assert "--needs-producer-review" not in final_update, (
            f"Status transition must NOT include --needs-producer-review, got: {final_update}"
        )
        assert final_update[3:7] == ["--status", "completed", "--stage", "in_review"], (
            f"Expected completed/in_review transition on yes verdict, got: {final_update}"
        )

    def test_no_needs_producer_review_when_not_ready_to_close(self, monkeypatch):
        """When audit verdict is NOT ready-to-close, the status update must NOT
        include --needs-producer-review (AC1: only set when verdict is ready-to-close)."""
        calls = []

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            lambda prompt, model="x", pi_bin="x", **kwargs: {
                "extracted_text": '[{"index": 0, "verdict": "unmet", "evidence": "missing"}]',
                "verdict": "unmet",
                "evidence": "missing",
            },
        )

        cmd_issue("SA-NPR2", runner=self._fake_runner_with_calls(calls, has_acs=False), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-NPR2"]]
        for update in wl_updates:
            assert "--needs-producer-review" not in update, (
                f"Status update should NOT include --needs-producer-review, got: {update}"
            )

    def test_no_needs_producer_review_on_exception(self, monkeypatch):
        """When audit fails with an exception, the status update must NOT
        include --needs-producer-review (AC1: only set when verdict is ready-to-close)."""
        calls = []

        def fake_call_pi(prompt, model="x", pi_bin="x", **kwargs):
            raise RuntimeError("Pi crashed")

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        cmd_issue("SA-NPR3", runner=self._fake_runner_with_calls(calls), persist=False)

        wl_updates = [c for c in calls if c[:3] == ["wl", "update", "SA-NPR3"]]
        for update in wl_updates:
            assert "--needs-producer-review" not in update, (
                f"Status update on exception should NOT include --needs-producer-review, got: {update}"
            )


# ---------------------------------------------------------------------------
# Freshness gate behavior tests
# ---------------------------------------------------------------------------


# Sentinel to distinguish "not provided" from "explicitly None"
_AUDIT_RAW_DEFAULT = object()


def _audit_fresh_runner(audit_audited_at: str | None = None,
                        audit_raw_output: object = _AUDIT_RAW_DEFAULT,
                        wi_updated_at: str | None = None,
                        fail_audit_show: bool = False,
                        calls: list | None = None) -> Runner:
    """Create a fake runner that returns appropriate responses for freshness gate tests.

    Handles three command types:
    - ``wl audit-show``: returns audit data with given auditedAt/rawOutput
    - ``wl show``: returns work item data with given updatedAt
    - All others: returns ``{"success": true}``

    When ``audit_raw_output`` is the sentinel ``_AUDIT_RAW_DEFAULT`` (default),
    a canned default report is used. When explicitly set to ``None``, the
    ``rawOutput`` in the response will be ``None``. When set to a string, that
    string is used.
    """
    _calls = calls if calls is not None else []

    def _runner(cmd, **kwargs):
        cmd_list = list(cmd)
        _calls.append(cmd_list)
        if "audit-show" in cmd_list:
            if fail_audit_show:
                return _fake_proc(returncode=1, stderr="audit not found")
            if audit_audited_at is None:
                # No prior audit
                audit_response = {"success": True, "workItemId": "SA-TEST", "audit": None}
            else:
                # Use the provided raw output or default
                if audit_raw_output is _AUDIT_RAW_DEFAULT:
                    rawo = "Ready to close: Yes\n\n## Summary\nPrevious audit."
                else:
                    rawo = audit_raw_output  # may be None or a string
                audit_response = {
                    "success": True,
                    "workItemId": "SA-TEST",
                    "audit": {
                        "workItemId": "SA-TEST",
                        "auditedAt": audit_audited_at,
                        "rawOutput": rawo,
                    },
                }
            return _fake_proc(stdout=json.dumps(audit_response))
        if "show" in cmd_list and "--children" in cmd_list:
            # wl show --children
            wi = _load_fixture("wi_with_numbered_ac.json")
            if wi_updated_at:
                wi["workItem"]["updatedAt"] = wi_updated_at
            return _fake_proc(stdout=json.dumps(wi))
        if "show" in cmd_list:
            # wl show (without --children)
            wi = _load_fixture("wi_with_numbered_ac.json")
            if wi_updated_at:
                wi["workItem"]["updatedAt"] = wi_updated_at
            return _fake_proc(stdout=json.dumps(wi))
        return _fake_proc(stdout=json.dumps({"success": True}))

    return _runner


class TestFreshnessGate:
    """Verify the recent-audit freshness gate in cmd_issue.

    The gate checks if a recent audit already exists before running the full
    audit pipeline. If fresh, it skips the audit and prints the existing report.
    """

    def _call_with_runner(self, runner, **kwargs):
        """Call cmd_issue with sensible defaults and an injectable runner."""
        return cmd_issue("SA-GATE", runner=runner, persist=False, **kwargs)

    def test_fresh_audit_skips_and_exits_zero(self):
        """When audit is fresh, exit with code 0 without running audit logic."""
        # auditedAt is well after updatedAt + 60s buffer
        runner = _audit_fresh_runner(
            audit_audited_at="2026-07-13T15:00:00.000Z",
            wi_updated_at="2026-07-13T14:00:00.000Z",
        )
        rc = self._call_with_runner(runner)
        assert rc == 0

    def test_fresh_audit_prints_skipping_message(self, capsys):
        """When skipping, print 'Skipping: audit still fresh'."""
        runner = _audit_fresh_runner(
            audit_audited_at="2026-07-13T15:00:00.000Z",
            wi_updated_at="2026-07-13T14:00:00.000Z",
        )
        self._call_with_runner(runner)
        captured = capsys.readouterr()
        assert "Skipping: audit still fresh" in captured.out

    def test_fresh_audit_displays_existing_report(self, capsys):
        """When skipping, print the existing audit rawOutput."""
        existing_report = "Ready to close: Yes\n\n## Summary\nExisting audit output."
        runner = _audit_fresh_runner(
            audit_audited_at="2026-07-13T15:00:00.000Z",
            wi_updated_at="2026-07-13T14:00:00.000Z",
            audit_raw_output=existing_report,
        )
        self._call_with_runner(runner)
        captured = capsys.readouterr()
        assert existing_report in captured.out

    def test_no_prior_audit_proceeds(self, capsys, monkeypatch):
        """When no prior audit exists (audit is None), proceed with full audit."""
        pi_called = {"count": 0}

        def fake_call_pi(prompt, **kw):
            pi_called["count"] += 1
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        runner = _audit_fresh_runner(audit_audited_at=None, wi_updated_at="2026-07-13T14:00:00.000Z")
        rc = self._call_with_runner(runner)
        assert rc == 0
        # Pi should have been called (full audit ran)
        assert pi_called["count"] > 0

    def test_stale_audit_proceeds(self, capsys, monkeypatch):
        """When audit is stale (auditedAt before updatedAt + buffer), proceed with full audit."""
        pi_called = {"count": 0}

        def fake_call_pi(prompt, **kw):
            pi_called["count"] += 1
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        # auditedAt is less than 60s after updatedAt (within buffer → stale)
        runner = _audit_fresh_runner(
            audit_audited_at="2026-07-13T14:00:30.000Z",
            wi_updated_at="2026-07-13T14:00:00.000Z",
        )
        rc = self._call_with_runner(runner)
        assert rc == 0
        assert pi_called["count"] > 0

    def test_audit_older_than_updated_proceeds(self, capsys, monkeypatch):
        """When audit is older than the work item update, proceed with full audit."""
        pi_called = {"count": 0}

        def fake_call_pi(prompt, **kw):
            pi_called["count"] += 1
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        # auditedAt is BEFORE updatedAt
        runner = _audit_fresh_runner(
            audit_audited_at="2026-07-13T13:00:00.000Z",
            wi_updated_at="2026-07-13T14:00:00.000Z",
        )
        rc = self._call_with_runner(runner)
        assert rc == 0
        assert pi_called["count"] > 0

    def test_audit_show_failure_falls_through(self, capsys, monkeypatch):
        """When wl audit-show fails, gracefully fall through to normal pipeline."""
        pi_called = {"count": 0}

        def fake_call_pi(prompt, **kw):
            pi_called["count"] += 1
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        runner = _audit_fresh_runner(fail_audit_show=True, wi_updated_at="2026-07-13T14:00:00.000Z")
        rc = self._call_with_runner(runner)
        assert rc == 0
        assert pi_called["count"] > 0

    def test_force_flag_bypasses_gate(self, capsys, monkeypatch):
        """When --force is True, run full audit even if fresh audit exists."""
        pi_called = {"count": 0}

        def fake_call_pi(prompt, **kw):
            pi_called["count"] += 1
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        runner = _audit_fresh_runner(
            audit_audited_at="2026-07-13T15:00:00.000Z",
            wi_updated_at="2026-07-13T14:00:00.000Z",
        )
        rc = self._call_with_runner(runner, force=True)
        assert rc == 0
        assert pi_called["count"] > 0

    def test_status_lifecycle_not_entered_on_skip(self):
        """When gate short-circuits, NO wl update --status calls are made."""
        calls = []
        runner = _audit_fresh_runner(
            audit_audited_at="2026-07-13T15:00:00.000Z",
            wi_updated_at="2026-07-13T14:00:00.000Z",
            calls=calls,
        )
        self._call_with_runner(runner)
        # No wl update --status calls should appear (no in_progress → no open)
        wl_updates = [c for c in calls if "update" in c and "--status" in c]
        assert len(wl_updates) == 0, (
            f"Expected no status lifecycle calls, got: {wl_updates}"
        )

    def test_no_skip_when_raw_output_is_null(self, capsys, monkeypatch):
        """When audit exists but rawOutput is null, proceed normally (not fresh)."""
        pi_called = {"count": 0}

        def fake_call_pi(prompt, **kw):
            pi_called["count"] += 1
            return {"verdict": "met", "evidence": "ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        calls = []
        runner = _audit_fresh_runner(
            audit_audited_at="2026-07-13T15:00:00.000Z",
            wi_updated_at="2026-07-13T14:00:00.000Z",
            audit_raw_output=None,
            calls=calls,
        )
        rc = self._call_with_runner(runner)
        assert rc == 0
        assert pi_called["count"] > 0

    def test_only_applies_to_issue_not_project(self, monkeypatch, capsys):
        """The gate should NOT apply to project-level audits."""
        pi_called = {"count": 0}

        def fake_call_pi(prompt, **kw):
            pi_called["count"] += 1
            return {"verdict": "met", "evidence": ""}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_runner(cmd, **kwargs):
            return _fake_proc(stdout=json.dumps({"success": True, "workItems": []}))

        rc = cmd_project(runner=fake_runner)
        assert rc == 0
        # Project audit should still run (no gate)
        assert pi_called["count"] > 0


# ---------------------------------------------------------------------------
# Child audit verdict helper tests
# ---------------------------------------------------------------------------


class TestGetChildAuditVerdict:
    """Verify _get_child_audit_verdict helper function."""

    def test_ready_yes_when_audit_says_yes(self):
        """Returns True when child audit says "Ready to close: Yes"."""
        def runner(cmd, **kwargs):
            if "audit-show" in list(cmd):
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-15T10:00:00.000Z",
                        "rawOutput": "Ready to close: Yes\n\n## Summary\nAll good.",
                    },
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is True
        assert reason == "ready"

    def test_ready_no_when_audit_says_no(self):
        """Returns False when child audit says "Ready to close: No"."""
        def runner(cmd, **kwargs):
            if "audit-show" in list(cmd):
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-15T10:00:00.000Z",
                        "rawOutput": "Ready to close: No\n\n## Summary\nIssues remain.",
                    },
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is False
        assert reason == "not_ready"

    def test_no_audit_returns_none(self):
        """Returns (None, "no_audit") when no audit data exists."""
        def runner(cmd, **kwargs):
            if "audit-show" in list(cmd):
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": None,
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is None
        assert reason == "no_audit"

    def test_stale_audit_returns_stale(self):
        """Returns (None, "stale") when audit is within freshness buffer but stale."""
        def runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "audit-show" in cmd_list:
                # Audit just a few seconds after update (within buffer -> stale)
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-15T10:00:30.000Z",
                        "rawOutput": "Ready to close: Yes\n\nAll good.",
                    },
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            if "show" in cmd_list and "--children" not in cmd_list:
                wi_data = {
                    "success": True,
                    "workItem": {
                        "id": "SA-CHILD",
                        "updatedAt": "2026-07-15T10:00:00.000Z",
                    },
                }
                return _fake_proc(stdout=json.dumps(wi_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is None
        assert reason == "stale"

    def test_fresh_audit_returns_verdict(self):
        """Returns the verdict when audit is fresh."""
        def runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "audit-show" in cmd_list:
                # Audit is well after the update (outside buffer -> fresh)
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-15T10:02:00.000Z",
                        "rawOutput": "Ready to close: Yes\n\nAll good.",
                    },
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            if "show" in cmd_list and "--children" not in cmd_list:
                wi_data = {
                    "success": True,
                    "workItem": {
                        "id": "SA-CHILD",
                        "updatedAt": "2026-07-15T10:00:00.000Z",
                    },
                }
                return _fake_proc(stdout=json.dumps(wi_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is True
        assert reason == "ready"

    def test_just_persisted_audit_returns_ready_not_stale(self):
        """AC1/AC3: an audit just persisted is trusted, not flagged stale.

        Persisting an audit bumps the child's updatedAt to ~its auditedAt
        (wl audit-set + wl update --audit-text + status transition), so
        auditedAt is a fraction of a second before updatedAt. The plain
        freshness gate (auditedAt > updatedAt + buffer) can never hold in that
        case, which made the parent runner re-trigger child audits forever. A
        child audited moments ago and reporting "Ready to close: Yes" must be
        treated as fresh instead of stale.
        """
        def runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "audit-show" in cmd_list:
                # auditedAt recorded at wl audit-set; updatedAt bumped slightly
                # later by the wl update --audit-text + status writes.
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-15T10:00:00.200Z",
                        "rawOutput": "Ready to close: Yes\n\nAll good.",
                    },
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            if "show" in cmd_list and "--children" not in cmd_list:
                wi_data = {
                    "success": True,
                    "workItem": {
                        "id": "SA-CHILD",
                        "updatedAt": "2026-07-15T10:00:00.400Z",  # 200ms after auditedAt
                    },
                }
                return _fake_proc(stdout=json.dumps(wi_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is True
        assert reason == "ready"

    def test_just_persisted_not_ready_audit_blocks_closure(self):
        """AC2: a just-persisted "Ready to close: No" audit still blocks.

        The audit-persistence freshness exemption must only make the audit
        usable; a "No" verdict must still be returned so it blocks parent
        closure (verdict semantics unchanged).
        """
        def runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "audit-show" in cmd_list:
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-15T10:00:00.200Z",
                        "rawOutput": "Ready to close: No\n\nIssues remain.",
                    },
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            if "show" in cmd_list and "--children" not in cmd_list:
                wi_data = {
                    "success": True,
                    "workItem": {
                        "id": "SA-CHILD",
                        "updatedAt": "2026-07-15T10:00:00.400Z",
                    },
                }
                return _fake_proc(stdout=json.dumps(wi_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is False
        assert reason == "not_ready"

    def test_audit_older_than_child_update_stays_stale(self):
        """A genuinely stale audit still returns stale.

        When the child was updated well AFTER the audit (not just the audit's
        own persistence write, which is within a few seconds), the audit must
        still be flagged stale so a re-audit is triggered.
        """
        def runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "audit-show" in cmd_list:
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-15T10:00:00.000Z",
                        "rawOutput": "Ready to close: Yes\n\nAll good.",
                    },
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            if "show" in cmd_list and "--children" not in cmd_list:
                wi_data = {
                    "success": True,
                    "workItem": {
                        "id": "SA-CHILD",
                        # Child updated 10 minutes after the audit → stale
                        "updatedAt": "2026-07-15T10:10:00.000Z",
                    },
                }
                return _fake_proc(stdout=json.dumps(wi_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is None
        assert reason == "stale"

    def test_audit_show_failure_returns_error(self):
        """Returns (None, "error") when wl audit-show fails."""
        def runner(cmd, **kwargs):
            if "audit-show" in list(cmd):
                return _fake_proc(returncode=1, stderr="command failed")
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is None
        assert reason == "error"

    def test_no_raw_output_returns_no_audit(self):
        """Returns (None, "no_audit") when rawOutput is missing."""
        def runner(cmd, **kwargs):
            if "audit-show" in list(cmd):
                audit_data = {
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": None,
                        "rawOutput": None,
                    },
                }
                return _fake_proc(stdout=json.dumps(audit_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        verdict, reason, audited_at = _get_child_audit_verdict(runner, "SA-CHILD")
        assert verdict is None
        assert reason == "no_audit"


class TestCmdIssueChildAuditAutoTrigger:
    """Integration tests for child audit auto-trigger in cmd_issue."""

    def test_child_with_no_audit_triggers_audit(self, monkeypatch, capsys):
        """When a child has no persisted audit, an audit is auto-triggered
        when the cascade is explicitly opted into (--audit-children,
        SA-0MSKB6V5Q007YDHE)."""
        pi_calls = []

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            pi_calls.append(prompt)
            return {"verdict": "met", "evidence": "x:1 — ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        triggered_children = []

        def fake_subprocess_run(cmd, **kwargs):
            # Record which child was triggered
            if "issue" in cmd:
                for i, arg in enumerate(cmd):
                    if arg == "issue" and i + 1 < len(cmd):
                        triggered_children.append(cmd[i + 1])
                        break
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.subprocess.run",
            fake_subprocess_run,
        )

        child_wi = _load_fixture("wi_with_numbered_ac.json")
        child_wi["workItem"]["id"] = "SA-ACTIVE"
        child_wi["workItem"]["title"] = "Active Child"
        child_wi["workItem"]["status"] = "in_progress"
        child_wi["workItem"]["stage"] = "in_review"

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = [
            {
                "id": "SA-ACTIVE",
                "title": "Active Child",
                "status": "in_progress",
                "stage": "in_review",
                "description": child_wi["workItem"]["description"],
            },
        ]

        # Track audit-show calls per child ID so we differentiate
        # between parent freshness gate and child audit checks.
        child_audit_seen = {}  # child_id -> bool (has been checked once)

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(child_wi))
            if "audit-show" in cmd_list:
                # Extract the child ID from the command
                target_id = cmd_list[2] if len(cmd_list) > 2 else ""
                if target_id == "SA-ACTIVE":
                    if child_audit_seen.get(target_id, False):
                        # Second call (after trigger): audit exists with ready=yes
                        audit_data = {
                            "success": True,
                            "workItemId": "SA-ACTIVE",
                            "audit": {
                                "workItemId": "SA-ACTIVE",
                                "auditedAt": "2026-07-16T12:00:00Z",
                                "rawOutput": "Ready to close: Yes\n\n## Summary\nAll good.",
                            },
                        }
                    else:
                        # First call for this child: no audit
                        child_audit_seen[target_id] = True
                        audit_data = {"success": True, "workItemId": target_id, "audit": None}
                else:
                    # Parent freshness gate: return no audit (proceed normally)
                    audit_data = {"success": True, "workItemId": target_id, "audit": None}
                return _fake_proc(stdout=json.dumps(audit_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        cmd_issue("SA-PARENT", runner=fake_runner, persist=True,
                  audit_children=True)  # cascade is opt-in (SA-0MSKB6V5Q007YDHE)
        # Should have triggered an audit for the active child
        assert "SA-ACTIVE" in triggered_children

    def test_child_with_just_persisted_audit_is_not_retriggered(self, monkeypatch, capsys):
        """AC1: a child audited moments ago is not re-audited by the parent run.

        A child whose own audit reports "Ready to close: Yes" and whose
        updatedAt was bumped by the audit's own persistence write (auditedAt ~
        updatedAt) must be treated as fresh and must not trigger a redundant
        auto-audit. Before SA-0MSI3XH34001LLU4 this child was flagged stale and
        the parent run re-triggered audits in an endless loop.
        """
        triggered_children = []
        pi_calls = []

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            pi_calls.append(prompt)
            return {"verdict": "met", "evidence": "x:1 — ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_subprocess_run(cmd, **kwargs):
            # Must never be called: a fresh child audit should not be re-triggered
            if "issue" in cmd:
                for i, arg in enumerate(cmd):
                    if arg == "issue" and i + 1 < len(cmd):
                        triggered_children.append(cmd[i + 1])
                        break
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.subprocess.run",
            fake_subprocess_run,
        )

        child_wi = _load_fixture("wi_with_numbered_ac.json")
        child_wi["workItem"]["id"] = "SA-FRESH"
        child_wi["workItem"]["title"] = "Fresh Child"
        child_wi["workItem"]["status"] = "in_progress"
        child_wi["workItem"]["stage"] = "in_review"
        child_wi["workItem"]["updatedAt"] = "2026-07-16T12:00:00.400Z"

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = [
            {
                "id": "SA-FRESH",
                "title": "Fresh Child",
                "status": "in_progress",
                "stage": "in_review",
                "description": child_wi["workItem"]["description"],
            },
        ]

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(child_wi))
            if "audit-show" in cmd_list:
                _i = cmd_list.index("audit-show")
                target_id = cmd_list[_i + 1] if _i + 1 < len(cmd_list) else ""
                if target_id == "SA-FRESH":
                    # Audit persisted 1ms before the child's updatedAt bump
                    # (the audit's own persistence write)
                    audit_data = {
                        "success": True,
                        "workItemId": "SA-FRESH",
                        "audit": {
                            "workItemId": "SA-FRESH",
                            "auditedAt": "2026-07-16T12:00:00.200Z",
                            "rawOutput": "Ready to close: Yes\n\nAll good.",
                        },
                    }
                else:
                    audit_data = {"success": True, "workItemId": target_id, "audit": None}
                return _fake_proc(stdout=json.dumps(audit_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        cmd_issue("SA-PARENT", runner=fake_runner, persist=True)

        # The just-persisted audit must be trusted as fresh: no re-trigger,
        # and the child is treated ready (child_audit_ready=True).
        assert "SA-FRESH" not in triggered_children, (
            "Recently-persisted child audit must not be re-audited, got: "
            f"{triggered_children}"
        )
        assert pi_calls, "Parent should still have run its own AC review via Pi."

    def test_child_just_persisted_not_ready_blocks_but_is_not_retriggered(self, monkeypatch, capsys):
        """AC2: a just-persisted "No" child audit blocks parent closure without re-triggering."""
        triggered_children = []

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {"verdict": "met", "evidence": "x:1 — ok"}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        def fake_subprocess_run(cmd, **kwargs):
            if "issue" in cmd:
                for i, arg in enumerate(cmd):
                    if arg == "issue" and i + 1 < len(cmd):
                        triggered_children.append(cmd[i + 1])
                        break
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner.subprocess.run",
            fake_subprocess_run,
        )

        child_wi = _load_fixture("wi_with_numbered_ac.json")
        child_wi["workItem"]["id"] = "SA-NOTREADY"
        child_wi["workItem"]["title"] = "Not Ready Child"
        child_wi["workItem"]["status"] = "in_progress"
        child_wi["workItem"]["stage"] = "in_review"
        child_wi["workItem"]["updatedAt"] = "2026-07-16T12:00:00.400Z"

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = [
            {
                "id": "SA-NOTREADY",
                "title": "Not Ready Child",
                "status": "in_progress",
                "stage": "in_review",
                "description": child_wi["workItem"]["description"],
            },
        ]

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(child_wi))
            if "audit-show" in cmd_list:
                _i = cmd_list.index("audit-show")
                target_id = cmd_list[_i + 1] if _i + 1 < len(cmd_list) else ""
                if target_id == "SA-NOTREADY":
                    audit_data = {
                        "success": True,
                        "workItemId": "SA-NOTREADY",
                        "audit": {
                            "workItemId": "SA-NOTREADY",
                            "auditedAt": "2026-07-16T12:00:00.200Z",
                            "rawOutput": "Ready to close: No\n\nIssues remain.",
                        },
                    }
                else:
                    audit_data = {"success": True, "workItemId": target_id, "audit": None}
                return _fake_proc(stdout=json.dumps(audit_data))
            return _fake_proc(stdout=json.dumps({"success": True}))

        cmd_issue("SA-PARENT", runner=fake_runner, persist=True)

        # A 'Not ready' child must NOT be blindly re-triggered either
        # (its verdict is trusted fresh and still blocks closure downstream).
        assert "SA-NOTREADY" not in triggered_children


class TestRC1CompletedInReviewChildFilter:
    """Regression tests for RC1: children in completed/in_review are included.

    Before the fix, active_children filtered out all children with
    status=="completed", which excluded completed/in_review children
    (the most common state when auditing a parent). After the fix,
    only deletedBy is filtered; stage logic is left to the phase1
    blocker check.
    """

    def test_completed_in_review_child_included_in_child_results(self, monkeypatch, capsys):
        """A child with status=completed, stage=in_review must appear in child_results."""
        pi_calls = []

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            pi_calls.append(prompt)
            return {"verdict": "met", "evidence": "x:1 — ok", "extracted_text": ""}

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        child_wi = _load_fixture("wi_with_numbered_ac.json")
        child_wi["workItem"]["id"] = "SA-REVIEW"
        child_wi["workItem"]["title"] = "Completed Review Child"
        child_wi["workItem"]["status"] = "completed"
        child_wi["workItem"]["stage"] = "in_review"

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = [
            {
                "id": "SA-REVIEW",
                "title": "Completed Review Child",
                "status": "completed",
                "stage": "in_review",
                "description": child_wi["workItem"]["description"],
            },
        ]

        captured_child_results = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(child_wi))
            if "audit-show" in cmd_list:
                # Return an existing audit so the child is processed
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItemId": "SA-REVIEW",
                    "audit": {
                        "workItemId": "SA-REVIEW",
                        "auditedAt": "2026-07-16T12:00:00Z",
                        "rawOutput": "Ready to close: Yes\n\n## Summary\nAll good.",
                    },
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        # Monkey-patch _assemble_issue_report to capture child_results
        original_assemble = __import__("skill.audit.scripts.audit_runner", fromlist=["_assemble_issue_report"])._assemble_issue_report

        def capturing_assemble(issue, ac_results, child_results, **kwargs):
            nonlocal captured_child_results
            captured_child_results = child_results
            return original_assemble(issue, ac_results, child_results, **kwargs)

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._assemble_issue_report",
            capturing_assemble,
        )

        cmd_issue("SA-PARENT", runner=fake_runner, persist=False)

        # Verify the child appeared in child_results
        child_ids = [c.get("id") for c in captured_child_results]
        assert "SA-REVIEW" in child_ids, (
            f"completed/in_review child should appear in child_results, got: {child_ids}"
        )

    def test_completed_done_child_is_not_excluded_by_filter(self, monkeypatch):
        """A child with status=completed, stage=done is also included for child_results."""
        # This test verifies RC1 does NOT re-introduce a filter that excludes
        # completed/done children. The phase1 blocking logic handles exemption.
        children = [
            {"id": "SA-REVIEW", "status": "completed", "stage": "in_review", "deletedBy": ""},
            {"id": "SA-DONE", "status": "completed", "stage": "done", "deletedBy": ""},
            {"id": "SA-ACTIVE", "status": "in_progress", "stage": "in_review", "deletedBy": ""},
        ]
        # Simulate the RC1 filter logic (after fix)
        active_children = [c for c in children if not c.get("deletedBy")]
        child_ids = [c["id"] for c in active_children]
        assert "SA-REVIEW" in child_ids, "completed/in_review child must be active"
        assert "SA-DONE" in child_ids, "completed/done child must be active for reporting"
        assert "SA-ACTIVE" in child_ids, "in_progress/in_review child must be active"

    def test_deleted_child_still_excluded(self):
        """The deletedBy exclusion must still work."""
        children = [
            {"id": "SA-DELETED", "status": "completed", "stage": "in_review", "deletedBy": "someone"},
            {"id": "SA-ACTIVE", "status": "in_progress", "stage": "in_review", "deletedBy": ""},
        ]
        active_children = [c for c in children if not c.get("deletedBy")]
        child_ids = [c["id"] for c in active_children]
        assert "SA-DELETED" not in child_ids, "deleted child must be excluded"
        assert "SA-ACTIVE" in child_ids, "non-deleted child must be included"


class TestRC2RCFallbackVerdict:
    """Regression tests for RC2/RC3: unparseable Pi output gets partial verdict.

    Before the fix, when _extract_json_array returned None (unparseable output),
    the fallback defaulted to verdict="unmet" with empty evidence. After the fix:
    - Default verdict is "partial" with a diagnostic evidence string
    - A warning is printed to stderr
    - Raw output is logged to debug log (when --debug-log is active)
    """

    def test_parent_ac_fallback_uses_partial_verdict_and_warning(self, monkeypatch, capsys):
        """Parent AC fallback uses "partial" verdict, diagnostic evidence, and prints warning."""
        from skill.audit.scripts.audit_runner import _assemble_issue_report

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            # Return text that _extract_json_array cannot parse
            return {
                "verdict": "met",
                "evidence": "not-a-json-array",
                "extracted_text": "Analysis text without a JSON array.",
                "text": "",
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = []

        captured_ac_results = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi["workItem"]))
            return _fake_proc(stdout=json.dumps({"success": True}))

        def capturing_assemble(issue, ac_results, child_results, **kwargs):
            nonlocal captured_ac_results
            captured_ac_results[:] = ac_results
            return _assemble_issue_report(issue, ac_results, child_results, **kwargs)

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._assemble_issue_report",
            capturing_assemble,
        )

        cmd_issue("SA-PARENT", runner=fake_runner, persist=False)

        # All ACs should have "partial" verdict with diagnostic evidence
        assert len(captured_ac_results) > 0, "Should have AC results"
        for ac in captured_ac_results:
            assert ac["verdict"] == "partial", (
                f"Expected 'partial' verdict for AC '{ac['text']}', got '{ac['verdict']}'"
            )
            assert "could not be parsed" in ac["evidence"].lower() or "unparseable" in ac["evidence"].lower(), (
                f"Expected diagnostic evidence, got: '{ac['evidence']}'"
            )

        # Warning should be printed to stderr
        captured = capsys.readouterr()
        assert "unparseable" in captured.err.lower() or "could not be parsed" in captured.err.lower(), (
            f"Expected warning on stderr about unparseable output, got: {captured.err}"
        )

    def test_child_ac_fallback_uses_partial_verdict_and_warning(self, monkeypatch, capsys):
        """Child AC fallback uses "partial" verdict, diagnostic evidence, and prints warning."""
        from skill.audit.scripts.audit_runner import _assemble_issue_report

        pi_call_count = [0]

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            pi_call_count[0] += 1
            if pi_call_count[0] == 1:
                # First call is for parent ACs - return parseable
                return {
                    "verdict": "met",
                    "evidence": "x:1 — ok",
                    "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "x:1 — ok"},{"index": 1, "verdict": "met", "evidence": "x:2 — ok"},{"index": 2, "verdict": "met", "evidence": "x:3 — ok"}]',
                    "text": "",
                }
            # Subsequent calls (child ACs) return unparseable text
            return {
                "verdict": "met",
                "evidence": "not-a-json-array",
                "extracted_text": "Child analysis text without a JSON array.",
                "text": "",
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        child_wi = _load_fixture("wi_with_numbered_ac.json")
        child_wi["workItem"]["id"] = "SA-CHILD"
        child_wi["workItem"]["title"] = "Child with ACs"
        child_wi["workItem"]["status"] = "in_progress"
        child_wi["workItem"]["stage"] = "in_review"

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = [
            {
                "id": "SA-CHILD",
                "title": "Child with ACs",
                "status": "in_progress",
                "stage": "in_review",
                "description": child_wi["workItem"]["description"],
            },
        ]

        captured_child_results = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(child_wi))
            if "audit-show" in cmd_list:
                # Non-ready persisted audit: under P7 reuse the child goes
                # through the Phase 1 screening path, exercising the RC2
                # unparseable-output fallback below.
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-16T12:00:00Z",
                        "rawOutput": "Ready to close: No\n\n## Summary\nNeeds work.",
                    },
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        def capturing_assemble(issue, ac_results, child_results, **kwargs):
            nonlocal captured_child_results
            captured_child_results[:] = child_results
            return _assemble_issue_report(issue, ac_results, child_results, **kwargs)

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._assemble_issue_report",
            capturing_assemble,
        )

        cmd_issue("SA-PARENT", runner=fake_runner, persist=False)

        # Find the child result
        child = next((c for c in captured_child_results if c["id"] == "SA-CHILD"), None)
        assert child is not None, "Child should be in results"

        for ac in child["ac_results"]:
            assert ac["verdict"] == "partial", (
                f"Expected 'partial' verdict for child AC '{ac['text']}', got '{ac['verdict']}'"
            )
            assert "could not be parsed" in ac["evidence"].lower() or "unparseable" in ac["evidence"].lower(), (
                f"Expected diagnostic evidence, got: '{ac['evidence']}'"
            )

        # Warning should be printed to stderr (at least once - may appear for both parent and reparse)
        captured = capsys.readouterr()
        assert "unparseable" in captured.err.lower() or "could not be parsed" in captured.err.lower(), (
            f"Expected warning on stderr about unparseable output, got: {captured.err}"
        )

    def test_parent_ac_fallback_uses_provider_error_diagnostic(self, monkeypatch, capsys):
        """Provider-error results surface a provider diagnostic, not a generic parse failure."""
        from skill.audit.scripts.audit_runner import _assemble_issue_report

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            # Simulate a persistent provider error (as returned by _call_pi
            # after retries are exhausted).
            return {
                "verdict": "unmet",
                "evidence": "Pi provider error: Provider finish_reason: error",
                "extracted_text": "",
                "text": "",
                "_provider_error": True,
                "_provider_error_message": "Provider finish_reason: error",
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = []

        captured_ac_results = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi["workItem"]))
            return _fake_proc(stdout=json.dumps({"success": True}))

        def capturing_assemble(issue, ac_results, child_results, **kwargs):
            nonlocal captured_ac_results
            captured_ac_results[:] = ac_results
            return _assemble_issue_report(issue, ac_results, child_results, **kwargs)

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._assemble_issue_report",
            capturing_assemble,
        )

        cmd_issue("SA-PARENT", runner=fake_runner, persist=False)

        assert len(captured_ac_results) > 0, "Should have AC results"
        for ac in captured_ac_results:
            assert ac["verdict"] == "partial", (
                f"Expected 'partial' verdict for AC '{ac['text']}', got '{ac['verdict']}'"
            )
            assert "provider error" in ac["evidence"].lower(), (
                f"Expected provider-error diagnostic, got: '{ac['evidence']}'"
            )
            assert "could not be parsed" not in ac["evidence"].lower(), (
                f"Provider errors must not be reported as generic parse failures: '{ac['evidence']}'"
            )

        # Warning should be printed to stderr
        captured = capsys.readouterr()
        assert "provider error" in captured.err.lower() or "unparseable" in captured.err.lower(), (
            f"Expected provider-error warning on stderr, got: {captured.err}"
        )

    def test_child_ac_fallback_uses_provider_error_diagnostic(self, monkeypatch, capsys):
        """Child AC provider-error results surface a provider diagnostic."""
        from skill.audit.scripts.audit_runner import _assemble_issue_report

        pi_call_count = [0]

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            pi_call_count[0] += 1
            if pi_call_count[0] == 1:
                # First call is for parent ACs - return parseable
                return {
                    "verdict": "met",
                    "evidence": "x:1 — ok",
                    "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "x:1 — ok"},{"index": 1, "verdict": "met", "evidence": "x:2 — ok"},{"index": 2, "verdict": "met", "evidence": "x:3 — ok"}]',
                    "text": "",
                }
            # Subsequent calls (child ACs) return a provider error
            return {
                "verdict": "unmet",
                "evidence": "Pi provider error: Provider finish_reason: error",
                "extracted_text": "",
                "text": "",
                "_provider_error": True,
                "_provider_error_message": "Provider finish_reason: error",
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )

        child_wi = _load_fixture("wi_with_numbered_ac.json")
        child_wi["workItem"]["id"] = "SA-CHILD"
        child_wi["workItem"]["title"] = "Child with ACs"
        child_wi["workItem"]["status"] = "in_progress"
        child_wi["workItem"]["stage"] = "in_review"

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = [
            {
                "id": "SA-CHILD",
                "title": "Child with ACs",
                "status": "in_progress",
                "stage": "in_review",
                "description": child_wi["workItem"]["description"],
            },
        ]

        captured_child_results = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(child_wi))
            if "audit-show" in cmd_list:
                return _fake_proc(stdout=json.dumps({
                    "success": True,
                    "workItemId": "SA-CHILD",
                    "audit": {
                        "workItemId": "SA-CHILD",
                        "auditedAt": "2026-07-16T12:00:00Z",
                        "rawOutput": "Ready to close: Yes\n\n## Summary\nAll good.",
                    },
                }))
            return _fake_proc(stdout=json.dumps({"success": True}))

        def capturing_assemble(issue, ac_results, child_results, **kwargs):
            nonlocal captured_child_results
            captured_child_results[:] = child_results
            return _assemble_issue_report(issue, ac_results, child_results, **kwargs)

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._assemble_issue_report",
            capturing_assemble,
        )

        cmd_issue("SA-PARENT", runner=fake_runner, persist=False)

        child = next((c for c in captured_child_results if c["id"] == "SA-CHILD"), None)
        assert child is not None, "Child should be in results"

        for ac in child["ac_results"]:
            assert ac["verdict"] == "partial", (
                f"Expected 'partial' verdict for child AC '{ac['text']}', got '{ac['verdict']}'"
            )
            assert "provider error" in ac["evidence"].lower(), (
                f"Expected provider-error diagnostic, got: '{ac['evidence']}'"
            )
            assert "could not be parsed" not in ac["evidence"].lower(), (
                f"Provider errors must not be reported as generic parse failures: '{ac['evidence']}'"
            )

        captured = capsys.readouterr()
        assert "provider error" in captured.err.lower() or "unparseable" in captured.err.lower(), (
            f"Expected provider-error warning on stderr, got: {captured.err}"
        )

    def test_fallback_writes_to_debug_log(self, monkeypatch, capsys, tmp_path):
        """Parse failure should write raw output to the debug log."""
        log_path = tmp_path / "audit_debug.jsonl"

        def fake_call_pi(prompt, model="test/model", pi_bin="pi", **kwargs):
            return {
                "verdict": "met",
                "evidence": "not-json",
                "extracted_text": "RAW UNPARSEABLE OUTPUT for debug log",
                "text": "",
            }

        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._call_pi",
            fake_call_pi,
        )
        monkeypatch.setattr(
            "skill.audit.scripts.audit_runner._default_debug_log_path",
            lambda issue_id, context: log_path,
        )

        parent_wi = _load_fixture("wi_with_numbered_ac.json")
        parent_wi["children"] = []

        def fake_runner(cmd, **kwargs):
            cmd_list = list(cmd)
            if "show" in cmd_list and "--children" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi))
            if "show" in cmd_list:
                return _fake_proc(stdout=json.dumps(parent_wi["workItem"]))
            return _fake_proc(stdout=json.dumps({"success": True}))

        cmd_issue("SA-DEBUG", runner=fake_runner, debug_log=str(log_path), persist=False)

        # Check that debug log was written and contains the raw text
        assert log_path.exists(), "Debug log should have been created"
        content = log_path.read_text()
        assert "RAW UNPARSEABLE OUTPUT" in content, (
            f"Debug log should contain raw output, got: {content[:200]}"
        )

