"""Tests for verify_context_reduction.py (SA-0MSISKM8F004NW1U).

Covers the deterministic, no-model logic: session-file token extraction,
timing-line parsing, verdict parsing, the flag-off runner copy transform,
and report/report-dir emission. The model-driven ``reaudit-sample`` path is
exercised only through its pure helpers (parse + comparison logic); the
full end-to-end run is an operational step documented in the script's
module docstring (AC3).

These tests must not touch the network, the model, or the real pi sessions
directory.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)
import verify_context_reduction as vcr

# ---------------------------------------------------------------------------
# Timing-line parsing (input_tokens capture format)
# ---------------------------------------------------------------------------

class TestTimingLineParsing:
    def test_parses_full_timing_line_with_tokens(self):
        line = (
            "Per-call timing: issue_id=SA-0MSISKM8F004NW1U "
            "context=parent elapsed_seconds=12.34 input_tokens=3561"
        )
        m = vcr.TIMING_LINE_RE.search(line)
        assert m is not None
        assert m.group(1) == "SA-0MSISKM8F004NW1U"
        assert m.group(2) == "parent"
        assert m.group(3) == "12.34"
        assert m.group(4) == "3561"

    def test_parses_timing_line_without_tokens(self):
        line = (
            "Per-call timing: issue_id=SA-0MSISKM8F004NW1U "
            "context=phase2 elapsed_seconds=5.00"
        )
        m = vcr.TIMING_LINE_RE.search(line)
        assert m is not None
        assert m.group(4) is None

    def test_does_not_match_other_lines(self):
        assert vcr.TIMING_LINE_RE.search("some other log line") is None
        assert vcr.TIMING_LINE_RE.search("input_tokens=99") is None


class TestVerdictParsing:
    def test_parses_yes(self):
        proc = type("P", (), {"stdout": "Ready to close: Yes\n", "stderr": ""})()
        verdict, tokens = vcr._parse_verdict(proc)
        assert verdict == "Yes"
        assert tokens == []

    def test_parses_no(self):
        proc = type("P", (), {"stdout": "", "stderr": "Ready to close: No\n"})()
        verdict, _ = vcr._parse_verdict(proc)
        assert verdict == "No"

    def test_no_verdict_when_missing(self):
        proc = type("P", (), {"stdout": "no verdict here", "stderr": ""})()
        verdict, _ = vcr._parse_verdict(proc)
        assert verdict is None

    def test_collects_input_tokens_from_timing_lines(self):
        out = (
            "Per-call timing: issue_id=X context=parent elapsed_seconds=1.0 "
            "input_tokens=410\n"
            "Per-call timing: issue_id=X context=phase2 elapsed_seconds=2.0\n"
            "Per-call timing: issue_id=X context=verdict elapsed_seconds=3.0 "
            "input_tokens=99\n"
        )
        proc = type("P", (), {"stdout": "", "stderr": out})()
        _, tokens = vcr._parse_verdict(proc)
        assert tokens == [410, 99]


# ---------------------------------------------------------------------------
# Session-file token extraction
# ---------------------------------------------------------------------------

class TestSessionTokenExtraction:
    def _make_session(self, tmp_path, fname, audit=True, input_tokens=1000,
                      item_id="SA-0MSISKM8F004NW1A"):
        lines = [
            {"type": "message",
             "message": {"role": "user",
                         "content": [{"type": "text",
                                      "text": ("[READ-ONLY AUDIT] You are "
                                               "performing a read-only audit. "
                                               f"work item {item_id}")}]}},
            {"type": "message",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input": input_tokens}}},
        ]
        p = tmp_path / fname
        p.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
        return p

    def test_extracts_audit_sessions_only(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        # Audit session (marker) and a non-audit session (implement skill).
        self._make_session(sess_dir, "2026-08-10T10-00-00-000Z_1.jsonl",
                           audit=True, input_tokens=410)
        self._make_session(sess_dir, "2026-08-10T10-01-00-000Z_2.jsonl",
                           audit=False, input_tokens=99999)
        monkeypatch.setattr(vcr, "_find_session_dirs", lambda: [sess_dir])
        samples = vcr.collect_session_tokens(min_items=1, max_items=None)
        assert len(samples) == 1
        assert samples[0].input_tokens == 410

    def test_requires_min_items(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        self._make_session(sess_dir, "2026-08-10T10-00-00-000Z_1.jsonl")
        monkeypatch.setattr(vcr, "_find_session_dirs", lambda: [sess_dir])
        with pytest.raises(RuntimeError, match="need at least 5"):
            vcr.collect_session_tokens(min_items=5, max_items=None)

    def test_since_filter_excludes_old_sessions(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        self._make_session(sess_dir, "2026-08-06T10-00-00-000Z_old.jsonl")
        self._make_session(sess_dir, "2026-08-08T10-00-00-000Z_new.jsonl")
        monkeypatch.setattr(vcr, "_find_session_dirs", lambda: [sess_dir])
        samples = vcr.collect_session_tokens(
            min_items=1, max_items=None, since="2026-08-07T11:15")
        assert len(samples) == 1
        assert samples[0].session_file.endswith("new.jsonl")

    def test_ignores_corrupt_lines(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        p = tmp_path / "2026-08-10T10-00-00-000Z_1.jsonl"
        p.write_text("{not json\n", encoding="utf-8")
        monkeypatch.setattr(vcr, "_find_session_dirs", lambda: [sess_dir])
        with pytest.raises(RuntimeError, match="need at least 5"):
            vcr.collect_session_tokens(min_items=5, max_items=None)

    def test_check_sessions_report_shape(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        ids = ["SA-0MSISKM8F004NW1A", "SA-0MSISKM8F004NW1B",
               "SA-0MSISKM8F004NW1C", "SA-0MSISKM8F004NW1D",
               "SA-0MSISKM8F004NW1E"]
        for i, item_id in enumerate(ids):
            self._make_session(
                sess_dir, f"2026-08-10T10-0{i}-00-000Z_{i}.jsonl",
                input_tokens=100 + i, item_id=item_id)
        monkeypatch.setattr(vcr, "_find_session_dirs", lambda: [sess_dir])
        report = vcr.check_sessions(min_items=5, max_items=None)
        assert report["passed"] is True
        assert report["distinct_items"] == 5
        assert report["violations"] == []

    def test_violation_detected(self, tmp_path, monkeypatch):
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        self._make_session(sess_dir, "2026-08-10T10-00-00-000Z_1.jsonl",
                           input_tokens=20000)
        monkeypatch.setattr(vcr, "_find_session_dirs", lambda: [sess_dir])
        report = vcr.check_sessions(min_items=1, max_items=None)
        assert report["passed"] is False
        assert report["violations"] == ["SA-0MSISKM8F004NW1A"]


# ---------------------------------------------------------------------------
# Flag-off runner copy
# ---------------------------------------------------------------------------

class TestFlagOffRunnerCopy:
    SAMPLE = (
        "_SKILLS_ROOT = Path(__file__).resolve().parents[2]\n"
        "if str(_SKILLS_ROOT) not in sys.path:\n"
        "    sys.path.insert(0, str(_SKILLS_ROOT))\n"
        "    cmd.extend([\"--no-context-files\", \"--no-skills\"])\n"
        "print('done')\n"
    )

    def test_removes_flags_line(self, tmp_path):
        src = tmp_path / "audit_runner.py"
        src.write_text(self.SAMPLE, encoding="utf-8")
        copy = vcr._flag_off_runner_copy(src, Path("/repo"), tmp_path)
        text = copy.read_text(encoding="utf-8")
        assert "--no-context-files" not in text
        assert "--no-skills" not in text
        assert "print('done')" in text

    def test_pins_repo_root(self, tmp_path):
        src = tmp_path / "audit_runner.py"
        src.write_text(self.SAMPLE, encoding="utf-8")
        copy = vcr._flag_off_runner_copy(src, Path("/repo/root"), tmp_path)
        text = copy.read_text(encoding="utf-8")
        assert "_SKILLS_ROOT = Path('/repo/root')" in text
        assert "parents[2]" not in text


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

class TestReport:
    def test_sessions_markdown_contains_rows(self):
        report = {
            "check": "sessions",
            "passed": True,
            "distinct_items": 2,
            "min_items": 1,
            "since": "2026-08-07",
            "samples": [
                {"item_id": "SA-0X", "input_tokens": 410, "under_10k": True,
                 "session_file": "/tmp/x.jsonl"},
                {"item_id": "SA-0Y", "input_tokens": 900, "under_10k": True,
                 "session_file": "/tmp/y.jsonl"},
            ],
            "violations": [],
        }
        md = vcr._markdown(report)
        assert "SA-0X" in md
        assert "410" in md
        assert "Passed: **True**" in md

    def test_static_markdown_reports_variants(self):
        report = {
            "check": "static",
            "passed": True,
            "without_flags": {"bytes": 14890, "skills": 17},
            "with_flags": {"bytes": 1721, "skills": 0},
            "reduction_bytes": 13169,
            "reduction_pct": 88.4,
            "token_estimate_with_flags": 430.2,
            "bytes_per_token_estimate": 4.0,
            "bound_tokens": 10000,
        }
        md = vcr._markdown(report)
        assert "14890" in md
        assert "1721" in md
        assert "88.4" in md


# ---------------------------------------------------------------------------
# Sampler and staging
# ---------------------------------------------------------------------------

class TestSamplePreference:
    def _fake_list(self, monkeypatch, items):
        import subprocess as _sp
        seen = []
        def fake_run(cmd, *a, **kw):
            # The sampler now runs per-status `wl list --status <s>` queries
            # piped through jq via bash -c (SA-0MSLVQMKF000ESPZ); the mock
            # emits the projected shape ({id, auditedAt, description} per
            # item) that jq would produce, once per status loop (deduped).
            out = json.dumps([
                {k: it.get(k) for k in ("id", "auditedAt", "description")}
                for it in items
            ])
            seen.append(cmd)
            return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        monkeypatch.setattr(_sp, "run", fake_run)

    def test_prefers_items_with_acs(self, monkeypatch):
        import subprocess as _sp
        items = [
            {"id": "SA-0MSISKM8F004NW01", "description": "d" * 400,
             "auditedAt": "2026-07-28"},
            {"id": "SA-0MSISKM8F004NW99",
             "description": "Goal\nAcceptance Criteria:\n1. x",
             "auditedAt": "2026-07-28"},
        ]
        # Only the second item has AC markers; give it a runner verdict too.
        def fake_run(cmd, *a, **kw):
            if "list --status" in (" ".join(cmd) if isinstance(cmd, list) else str(cmd)):
                out = json.dumps([
                    {k: it.get(k) for k in ("id", "auditedAt", "description")}
                    for it in items
                ])
            else:
                # wl show may carry --worklog-dir flags; find the id after 'show'
                try:
                    iid = cmd[cmd.index("show") + 1]
                except ValueError:
                    iid = ""
                verdict_text = ("Ready to close: Yes"
                                if iid == "SA-0MSISKM8F004NW99" else "manual")
                out = json.dumps({"workItem": {
                    "id": iid,
                    "audit": {"text": verdict_text, "time": "x"},
                    "status": "completed", "stage": "done",
                }})
            return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        monkeypatch.setattr(_sp, "run", fake_run)
        got = vcr._sample_audited_items(1, 1, Path("/repo"))
        assert got == ["SA-0MSISKM8F004NW99"]

    def test_stage_for_audit_moves_closed_item(self, monkeypatch):
        import subprocess as _sp
        calls = []
        def fake_run(cmd, *a, **kw):
            calls.append(cmd)
            return type("P", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        monkeypatch.setattr(_sp, "run", fake_run)
        item = vcr.AuditItem(item_id="SA-0X", pre_status="completed",
                             pre_stage="done")
        vcr._stage_for_audit(item)
        assert any("in_progress" in c for c in calls)
        assert any("SA-0X" in c for c in calls)

    def test_stage_for_audit_skips_open_items(self, monkeypatch):
        import subprocess as _sp
        calls = []
        def fake_run(cmd, *a, **kw):
            calls.append(cmd)
            return type("P", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        monkeypatch.setattr(_sp, "run", fake_run)
        item = vcr.AuditItem(item_id="SA-0X", pre_status="open",
                             pre_stage="plan_complete")
        vcr._stage_for_audit(item)
        assert calls == []


# ---------------------------------------------------------------------------
# Reaudit result comparison
# ---------------------------------------------------------------------------

class TestReauditComparison:
    def test_controlled_verdicts_equal_passes(self):
        res = vcr.ReauditResult(item_id="SA-0X", persisted_verdict=None,
                                flags_on_verdict="Yes")
        res.flags_off_verdict = "Yes"
        res.input_tokens = [410, 99]
        res.divergence = None
        res.passed = True
        assert res.passed is True

    def test_controlled_divergence_detected(self):
        res = vcr.ReauditResult(item_id="SA-0X", persisted_verdict=None,
                                flags_on_verdict="Yes")
        res.flags_off_verdict = "No"
        res.divergence = (
            "verdict differs with/without flags: on=Yes off=No"
        )
        res.passed = False
        assert res.passed is False
