"""Regression tests for the audit-report code-quality findings cap.

WL-0MSS55LFU00973S2: when the code-quality scan emits hundreds of findings
(e.g. a linter mis-parses non-matching files), the assembled audit report
embedded every row, blowing past the OS argv limit and making persistence
fail with ``OSError [Errno 7] Argument list too long``. The report now caps
the rendered findings table at ``AUDIT_MAX_FINDINGS_IN_REPORT`` (default 50)
and emits an accurate "N additional findings omitted" note. Closure/blocking
logic is display-independent — it always uses the FULL finding list.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _finding(index: int, severity: str = "low") -> dict:
    return {
        "severity": severity,
        "file": f"file_{index}.py",
        "line": index,
        "message": f"msg-{index:04d}",
        "linter": "ruff",
        "code": "E501",
    }


def _met_ac() -> list[dict]:
    return [{"text": "AC1", "verdict": "met", "evidence": "file.py:1"}]


def _assemble(findings, **kwargs) -> str:
    return audit_runner._assemble_issue_report(
        {"id": "TEST-1"}, _met_ac(), [],
        code_quality_findings=findings,
        model="test-model", model_source="remote", **kwargs,
    )


class TestFindingsTableCap:
    def test_caps_rendered_rows_and_reports_accurate_total(self, monkeypatch):
        monkeypatch.delenv(
            audit_runner.MAX_FINDINGS_IN_REPORT_ENV, raising=False
        )
        report = _assemble([_finding(i) for i in range(1000)])

        # Exactly the cap's worth of finding rows are rendered.
        rendered = report.count("| msg-")
        assert rendered == audit_runner.MAX_FINDINGS_IN_REPORT_DEFAULT

        # The omission note names the omitted count and the true total.
        assert "950 additional findings omitted for brevity" in report
        assert "(showing 50 of 1000; cap: 50)" in report

    def test_below_cap_renders_all_without_omission_note(self, monkeypatch):
        monkeypatch.delenv(
            audit_runner.MAX_FINDINGS_IN_REPORT_ENV, raising=False
        )
        report = _assemble([_finding(i) for i in range(3)])

        assert report.count("| msg-") == 3
        assert "additional findings omitted" not in report

    def test_at_cap_renders_all_without_omission_note(self, monkeypatch):
        monkeypatch.setenv(audit_runner.MAX_FINDINGS_IN_REPORT_ENV, "5")
        report = _assemble([_finding(i) for i in range(5)])

        assert report.count("| msg-") == 5
        assert "additional findings omitted" not in report

    def test_env_override_limits_rendered_rows(self, monkeypatch):
        monkeypatch.setenv(audit_runner.MAX_FINDINGS_IN_REPORT_ENV, "5")
        report = _assemble([_finding(i) for i in range(100)])

        assert report.count("| msg-") == 5
        assert "95 additional findings omitted for brevity" in report
        assert "(showing 5 of 100; cap: 5)" in report

    def test_invalid_env_value_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv(audit_runner.MAX_FINDINGS_IN_REPORT_ENV, "not-a-number")
        report = _assemble([_finding(i) for i in range(60)])

        assert report.count("| msg-") == audit_runner.MAX_FINDINGS_IN_REPORT_DEFAULT
        assert "invalid AUDIT_MAX_FINDINGS_IN_REPORT" in capsys.readouterr().err

    def test_zero_env_value_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv(audit_runner.MAX_FINDINGS_IN_REPORT_ENV, "0")
        report = _assemble([_finding(i) for i in range(60)])

        assert report.count("| msg-") == audit_runner.MAX_FINDINGS_IN_REPORT_DEFAULT
        assert "must be a positive integer" in capsys.readouterr().err


class TestCapDoesNotAffectClosureLogic:
    """AC4: closure decision uses the full finding list, not the capped table."""

    def test_critical_finding_beyond_cap_still_blocks_closure(self, monkeypatch):
        monkeypatch.setenv(audit_runner.MAX_FINDINGS_IN_REPORT_ENV, "5")
        findings = [_finding(i) for i in range(100)]
        # The blocking finding is well beyond the rendered cap.
        findings.append(_finding(999, severity="critical"))
        report = _assemble(findings)

        assert "Ready to close: No" in report
        # Rendered table still capped at the override.
        assert report.count("| msg-") == 5
        # Critical finding is not among the rendered rows but still blocks.
        assert "| msg-0999" not in report
        assert "96 additional findings omitted for brevity" in report

    def test_low_findings_beyond_cap_do_not_block(self, monkeypatch):
        monkeypatch.setenv(audit_runner.MAX_FINDINGS_IN_REPORT_ENV, "5")
        report = _assemble([_finding(i) for i in range(100)])

        assert "Ready to close: Yes" in report


class TestFalsePositiveScreenCap:
    def test_screen_table_is_capped_too(self, monkeypatch):
        monkeypatch.setenv(audit_runner.MAX_FINDINGS_IN_REPORT_ENV, "5")
        findings = [_finding(i) for i in range(20)]
        fp_results = [
            {
                "index": i,
                "finding": findings[i],
                "classification": "uncertain",
                "justification": f"why-{i:04d}",
            }
            for i in range(20)
        ]
        report = _assemble(findings, fp_screen_results=fp_results)

        # Screen table rows are capped too (justifications are unique markers).
        assert report.count("| why-") == 5
        assert "15 additional false-positive screen entries omitted" in report
