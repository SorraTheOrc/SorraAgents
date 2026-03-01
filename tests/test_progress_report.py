"""Tests for ampa.progress_report."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
from unittest import mock

import pytest

from ampa.progress_report import (
    _compute_percent_complete,
    _compute_risk_level,
    _extract_delegation_trail,
    _format_markdown_report,
    _identify_risks,
    _summarize_comment,
    generate_progress_report,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
OLD_DATE = (NOW - timedelta(days=14)).isoformat()
RECENT_DATE = (NOW - timedelta(days=1)).isoformat()


def _make_work_item(
    id: str = "SA-EPIC1",
    title: str = "Test Epic",
    description: str = "",
    status: str = "open",
    priority: str = "high",
) -> Dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
    }


def _make_child(
    id: str,
    title: str,
    status: str = "open",
    priority: str = "medium",
    risk: str = "",
    assignee: str = "",
    updated_at: str = "",
) -> Dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "status": status,
        "priority": priority,
        "risk": risk,
        "assignee": assignee,
        "updatedAt": updated_at or RECENT_DATE,
    }


def _make_wl_response(
    work_item: Dict[str, Any],
    children: list[Dict[str, Any]] | None = None,
    comments: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "success": True,
        "workItem": work_item,
        "children": children or [],
        "comments": comments or [],
    }


def _make_comment(
    id: str = "C1",
    comment: str = "Some comment",
    author: str = "someone",
    created_at: str = "",
) -> Dict[str, Any]:
    return {
        "id": id,
        "comment": comment,
        "author": author,
        "createdAt": created_at or RECENT_DATE,
    }


# ---------------------------------------------------------------------------
# Percent-complete calculation
# ---------------------------------------------------------------------------


class TestPercentComplete:
    def test_all_completed(self):
        children = [
            _make_child("C1", "A", status="completed"),
            _make_child("C2", "B", status="closed"),
        ]
        pct, counts = _compute_percent_complete(children)
        assert pct == 100.0
        assert counts["completed"] == 2

    def test_all_open(self):
        children = [
            _make_child("C1", "A", status="open"),
            _make_child("C2", "B", status="open"),
        ]
        pct, counts = _compute_percent_complete(children)
        assert pct == 0.0
        assert counts["open"] == 2

    def test_mixed_statuses(self):
        children = [
            _make_child("C1", "A", status="completed"),
            _make_child("C2", "B", status="in-progress"),
            _make_child("C3", "C", status="open"),
            _make_child("C4", "D", status="blocked"),
        ]
        pct, counts = _compute_percent_complete(children)
        # (1.0 + 0.5 + 0.0 + 0.25) / 4 * 100 = 43.75
        assert pct == 43.8  # rounded to 1 decimal
        assert counts["completed"] == 1
        assert counts["in_progress"] == 1
        assert counts["open"] == 1
        assert counts["blocked"] == 1

    def test_deleted_not_counted(self):
        children = [
            _make_child("C1", "A", status="completed"),
            _make_child("C2", "B", status="deleted"),
        ]
        pct, counts = _compute_percent_complete(children)
        assert pct == 100.0
        assert counts["deleted"] == 1

    def test_no_children(self):
        pct, counts = _compute_percent_complete([])
        assert pct == 0.0

    def test_half_done(self):
        children = [
            _make_child("C1", "A", status="completed"),
            _make_child("C2", "B", status="open"),
        ]
        pct, counts = _compute_percent_complete(children)
        assert pct == 50.0

    def test_all_in_progress(self):
        children = [
            _make_child("C1", "A", status="in-progress"),
            _make_child("C2", "B", status="in-progress"),
        ]
        pct, counts = _compute_percent_complete(children)
        assert pct == 50.0


# ---------------------------------------------------------------------------
# Risk identification
# ---------------------------------------------------------------------------


class TestIdentifyRisks:
    def test_blocked_items(self):
        children = [
            _make_child("C1", "Blocked task", status="blocked"),
        ]
        risks = _identify_risks(children, now=NOW)
        assert len(risks) == 1
        assert risks[0]["id"] == "C1"
        assert any("blocked" in r.lower() for r in risks[0]["reasons"])

    def test_explicit_risk_field(self):
        children = [
            _make_child("C1", "Risky task", risk="Data loss possible"),
        ]
        risks = _identify_risks(children, now=NOW)
        assert len(risks) == 1
        assert any("Data loss" in r for r in risks[0]["reasons"])

    def test_high_priority_open(self):
        children = [
            _make_child("C1", "Critical open", priority="critical", status="open"),
        ]
        risks = _identify_risks(children, now=NOW)
        assert len(risks) == 1
        assert any("critical" in r.lower() for r in risks[0]["reasons"])

    def test_stale_in_progress(self):
        children = [
            _make_child("C1", "Stale task", status="in-progress", updated_at=OLD_DATE),
        ]
        risks = _identify_risks(children, now=NOW)
        assert len(risks) == 1
        assert any("Stale" in r for r in risks[0]["reasons"])

    def test_no_risks_for_completed(self):
        children = [
            _make_child("C1", "Done", status="completed"),
            _make_child("C2", "Also done", status="closed"),
        ]
        risks = _identify_risks(children, now=NOW)
        assert len(risks) == 0

    def test_no_risks_for_deleted(self):
        children = [
            _make_child("C1", "Deleted", status="deleted"),
        ]
        risks = _identify_risks(children, now=NOW)
        assert len(risks) == 0

    def test_risk_sorting_critical_first(self):
        children = [
            _make_child("C1", "Low risk", priority="low", risk="Minor"),
            _make_child(
                "C2", "Blocked critical", priority="critical", status="blocked"
            ),
        ]
        risks = _identify_risks(children, now=NOW)
        assert len(risks) == 2
        assert risks[0]["id"] == "C2"  # Critical comes first

    def test_recently_updated_not_stale(self):
        children = [
            _make_child(
                "C1",
                "Active",
                status="in-progress",
                updated_at=RECENT_DATE,
            ),
        ]
        risks = _identify_risks(children, now=NOW)
        # Recently updated in-progress item should not be flagged as stale
        stale_risks = [
            r for r in risks if any("Stale" in reason for reason in r["reasons"])
        ]
        assert len(stale_risks) == 0

    def test_medium_priority_open_no_risk(self):
        children = [
            _make_child("C1", "Medium open", priority="medium", status="open"),
        ]
        risks = _identify_risks(children, now=NOW)
        assert len(risks) == 0


class TestComputeRiskLevel:
    def test_blocked_critical(self):
        assert _compute_risk_level(["Item is blocked"], "critical") == "critical"

    def test_blocked_high(self):
        assert _compute_risk_level(["Item is blocked"], "high") == "high"

    def test_stale(self):
        assert _compute_risk_level(["Stale: 14 days"], "medium") == "high"

    def test_high_priority(self):
        assert _compute_risk_level(["High-priority item"], "high") == "medium"

    def test_low_priority(self):
        assert _compute_risk_level(["Some reason"], "low") == "low"


# ---------------------------------------------------------------------------
# Delegation trail
# ---------------------------------------------------------------------------


class TestDelegationTrail:
    def test_extract_delegation_comments(self):
        comments = [
            _make_comment("C1", "# APMA Delegation Plan\nProposed tasks...", "apma"),
            _make_comment("C2", "Regular progress note", "engineer"),
            _make_comment("C3", "Delegated to dev-agent for implementation", "pm"),
        ]
        trail = _extract_delegation_trail(comments)
        assert len(trail) == 2  # C1 and C3 match
        assert trail[0]["author"] == "apma"
        assert trail[1]["author"] == "pm"

    def test_no_delegation_comments(self):
        comments = [
            _make_comment("C1", "Fixed a typo", "engineer"),
            _make_comment("C2", "Build passed", "ci"),
        ]
        trail = _extract_delegation_trail(comments)
        assert len(trail) == 0

    def test_empty_comments(self):
        trail = _extract_delegation_trail([])
        assert len(trail) == 0


class TestSummarizeComment:
    def test_short_comment(self):
        assert _summarize_comment("Hello world") == "Hello world"

    def test_long_comment_truncated(self):
        long_text = "A" * 300
        summary = _summarize_comment(long_text, max_length=200)
        assert len(summary) == 203  # 200 + "..."
        assert summary.endswith("...")

    def test_multiline_takes_first(self):
        text = "# Heading\n\nBody text here"
        assert _summarize_comment(text) == "Heading"

    def test_empty_comment(self):
        assert _summarize_comment("") == ""


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------


class TestFormatMarkdown:
    def test_contains_required_sections(self):
        report_data = {
            "work_item": {
                "id": "SA-X",
                "title": "Test",
                "status": "open",
                "priority": "high",
            },
            "percent_complete": 50.0,
            "status_counts": {
                "completed": 2,
                "in_progress": 1,
                "blocked": 0,
                "open": 1,
                "deleted": 0,
            },
            "top_risks": [],
            "delegation_trail": [],
        }
        md = _format_markdown_report(report_data)
        assert "# Progress Report" in md
        assert "## Progress" in md
        assert "50.0%" in md
        assert "## Top Risks" in md
        assert "## Delegation Audit Trail" in md

    def test_includes_risks(self):
        report_data = {
            "work_item": {
                "id": "SA-X",
                "title": "Test",
                "status": "open",
                "priority": "high",
            },
            "percent_complete": 25.0,
            "status_counts": {
                "completed": 0,
                "in_progress": 0,
                "blocked": 1,
                "open": 1,
                "deleted": 0,
            },
            "top_risks": [
                {
                    "id": "SA-C1",
                    "title": "Blocked task",
                    "status": "blocked",
                    "priority": "critical",
                    "risk_level": "critical",
                    "reasons": ["Item is blocked"],
                },
            ],
            "delegation_trail": [],
        }
        md = _format_markdown_report(report_data)
        assert "[CRITICAL]" in md
        assert "Blocked task" in md

    def test_includes_delegation_trail(self):
        report_data = {
            "work_item": {
                "id": "SA-X",
                "title": "Test",
                "status": "open",
                "priority": "high",
            },
            "percent_complete": 0.0,
            "status_counts": {
                "completed": 0,
                "in_progress": 0,
                "blocked": 0,
                "open": 1,
                "deleted": 0,
            },
            "top_risks": [],
            "delegation_trail": [
                {
                    "id": "C1",
                    "author": "apma",
                    "date": "2026-03-01T12:00:00Z",
                    "summary": "Delegation plan created",
                },
            ],
        }
        md = _format_markdown_report(report_data)
        assert "2026-03-01" in md
        assert "apma" in md
        assert "Delegation plan created" in md

    def test_progress_bar(self):
        report_data = {
            "work_item": {
                "id": "SA-X",
                "title": "Test",
                "status": "open",
                "priority": "high",
            },
            "percent_complete": 75.0,
            "status_counts": {
                "completed": 3,
                "in_progress": 0,
                "blocked": 0,
                "open": 1,
                "deleted": 0,
            },
            "top_risks": [],
            "delegation_trail": [],
        }
        md = _format_markdown_report(report_data)
        assert "Progress:" in md
        assert "█" in md


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TestGenerateProgressReport:
    def test_basic_report(self):
        wi = _make_work_item()
        children = [
            _make_child("C1", "Done", status="completed"),
            _make_child("C2", "Open", status="open"),
        ]

        report = generate_progress_report(
            "SA-EPIC1",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
            now=NOW,
        )
        assert report["percent_complete"] == 50.0
        assert report["children_count"] == 2
        assert report["work_item"]["id"] == "SA-EPIC1"
        assert "markdown" in report

    def test_report_with_comments(self):
        wi = _make_work_item()
        children = [_make_child("C1", "Task")]
        comments = [
            _make_comment("C1", "# APMA Delegation Plan", "apma"),
        ]

        report = generate_progress_report(
            "SA-EPIC1",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children, comments),
            now=NOW,
        )
        assert len(report["delegation_trail"]) == 1

    def test_report_with_custom_comment_fetcher(self):
        wi = _make_work_item()
        children = [_make_child("C1", "Task")]
        custom_comments = [
            _make_comment("C1", "Delegated to dev-agent", "pm"),
        ]

        report = generate_progress_report(
            "SA-EPIC1",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
            _comment_fetcher=lambda wid, cwd=None: custom_comments,
            now=NOW,
        )
        assert len(report["delegation_trail"]) == 1

    def test_report_json_serializable(self):
        wi = _make_work_item()
        children = [
            _make_child("C1", "Done", status="completed"),
            _make_child("C2", "Blocked", status="blocked", priority="critical"),
        ]

        report = generate_progress_report(
            "SA-EPIC1",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
            now=NOW,
        )
        serialized = json.dumps(report)
        assert serialized
        parsed = json.loads(serialized)
        assert parsed["percent_complete"] == 62.5  # (1.0 + 0.25) / 2 * 100

    def test_report_with_risks(self):
        wi = _make_work_item()
        children = [
            _make_child(
                "C1", "Blocked critical", status="blocked", priority="critical"
            ),
            _make_child("C2", "Stale item", status="in-progress", updated_at=OLD_DATE),
        ]

        report = generate_progress_report(
            "SA-EPIC1",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
            now=NOW,
        )
        assert len(report["top_risks"]) == 2
        # Critical blocked should be first
        assert report["top_risks"][0]["risk_level"] == "critical"


class TestValidation:
    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            generate_progress_report("")

    def test_whitespace_id_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            generate_progress_report("   ")

    def test_missing_work_item_raises(self):
        def mock_fetcher(wid, cwd=None):
            return {"success": True, "workItem": {}, "children": []}

        with pytest.raises(RuntimeError, match="missing"):
            generate_progress_report("SA-BAD", _wl_fetcher=mock_fetcher)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_markdown(self, capsys):
        wi = _make_work_item()
        children = [_make_child("C1", "Task", status="completed")]

        with (
            mock.patch(
                "ampa.progress_report._fetch_work_item",
                return_value=_make_wl_response(wi, children),
            ),
            mock.patch(
                "ampa.progress_report._fetch_comments",
                return_value=[],
            ),
        ):
            main(["--work-item", "SA-EPIC1"])

        output = capsys.readouterr().out
        assert "# Progress Report" in output
        assert "100.0%" in output

    def test_cli_json(self, capsys):
        wi = _make_work_item()
        children = [_make_child("C1", "Task")]

        with (
            mock.patch(
                "ampa.progress_report._fetch_work_item",
                return_value=_make_wl_response(wi, children),
            ),
            mock.patch(
                "ampa.progress_report._fetch_comments",
                return_value=[],
            ),
        ):
            main(["--work-item", "SA-EPIC1", "--format", "json"])

        output = capsys.readouterr().out
        parsed = json.loads(output)
        assert parsed["work_item"]["id"] == "SA-EPIC1"
