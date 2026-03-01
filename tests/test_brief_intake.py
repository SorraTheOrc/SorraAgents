"""Tests for ampa.brief_intake — Brief-to-Epic Creator.

Covers:
- Brief parsing (title extraction, summary, acceptance criteria extraction).
- Plan construction from templates.
- dry_run mode returns plan without wl mutations.
- propose mode calls wl create with correct arguments and parent linking.
- CLI entry point.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_BRIEF = textwrap.dedent("""\
    # Build a User Authentication System

    We need a complete user authentication system that supports email/password
    login, OAuth2 integration, and role-based access control.

    ## Goals
    - Secure login and registration flow
    - Support for OAuth2 providers (Google, GitHub)
    - Role-based access control (admin, user, viewer)

    ## Acceptance Criteria
    1. Users can register with email and password.
    2. Users can log in and receive a JWT token.
    3. OAuth2 login works with at least Google and GitHub.
    4. Roles are enforced on protected endpoints.

    ## Constraints
    - Must use existing database schema patterns.
    - No external auth service dependencies.
""")

MINIMAL_BRIEF = "Add dark mode support to the dashboard"


@pytest.fixture
def templates_path(tmp_path: Path) -> str:
    """Write a minimal templates file and return its path."""
    content = textwrap.dedent("""\
        templates:
          - category: discovery
            title_prefix: "Discovery:"
            issue_type: task
            priority: high
            suggested_assignee: analysis-agent
            description_template: "Discover: {brief_title}\\n{brief_summary}"
            acceptance_criteria_template: |
              ## Acceptance Criteria
              1. Findings documented.
          - category: implementation
            title_prefix: "Implement:"
            issue_type: feature
            priority: high
            suggested_assignee: dev-agent
            description_template: "Implement: {brief_title}\\n{brief_summary}"
            acceptance_criteria_template: |
              ## Acceptance Criteria
              1. Code written and tested.
    """)
    path = tmp_path / "templates.yaml"
    path.write_text(content)
    return str(path)


# ---------------------------------------------------------------------------
# Unit tests: brief parsing
# ---------------------------------------------------------------------------


class TestBriefParsing:
    """Tests for internal parsing functions."""

    def test_extract_title_from_heading(self):
        from ampa.brief_intake import _extract_title

        assert _extract_title("# My Project\nDetails here") == "My Project"

    def test_extract_title_from_plain_text(self):
        from ampa.brief_intake import _extract_title

        assert _extract_title("Simple project brief") == "Simple project brief"

    def test_extract_title_truncates_long_lines(self):
        from ampa.brief_intake import _extract_title

        long_line = "A" * 200
        title = _extract_title(long_line)
        assert len(title) <= 120

    def test_extract_title_skips_empty_lines(self):
        from ampa.brief_intake import _extract_title

        assert _extract_title("\n\n  \n## Real Title\nBody") == "Real Title"

    def test_extract_summary_truncates(self):
        from ampa.brief_intake import _extract_summary

        long_text = "x" * 2000
        summary = _extract_summary(long_text, max_chars=100)
        assert len(summary) <= 104  # 100 + "\n..."

    def test_extract_acceptance_criteria(self):
        from ampa.brief_intake import _extract_acceptance_criteria

        ac = _extract_acceptance_criteria(SAMPLE_BRIEF)
        assert "Users can register" in ac
        assert "JWT token" in ac

    def test_extract_acceptance_criteria_missing(self):
        from ampa.brief_intake import _extract_acceptance_criteria

        ac = _extract_acceptance_criteria("No AC here")
        assert ac == ""

    def test_build_epic_description(self):
        from ampa.brief_intake import _build_epic_description

        desc = _build_epic_description(SAMPLE_BRIEF)
        assert "APMA brief intake" in desc
        assert "User Authentication" in desc


# ---------------------------------------------------------------------------
# Unit tests: plan construction
# ---------------------------------------------------------------------------


class TestPlanConstruction:
    """Tests for plan building from templates."""

    def test_build_plan_creates_children_for_each_template(self, templates_path):
        from ampa.brief_intake import _build_plan, _load_templates

        templates = _load_templates(templates_path)
        plan = _build_plan(SAMPLE_BRIEF, templates)

        assert "epic" in plan
        assert "children" in plan
        assert len(plan["children"]) == 2  # discovery + implementation

    def test_children_have_required_fields(self, templates_path):
        from ampa.brief_intake import _build_plan, _load_templates

        templates = _load_templates(templates_path)
        plan = _build_plan(SAMPLE_BRIEF, templates)

        for child in plan["children"]:
            assert "title" in child
            assert "description" in child
            assert "acceptance_criteria" in child
            assert "suggested_assignee" in child
            assert "priority" in child
            assert "issue_type" in child
            assert "category" in child

    def test_children_cover_expected_categories(self, templates_path):
        from ampa.brief_intake import _build_plan, _load_templates

        templates = _load_templates(templates_path)
        plan = _build_plan(SAMPLE_BRIEF, templates)

        categories = {c["category"] for c in plan["children"]}
        assert "discovery" in categories
        assert "implementation" in categories

    def test_epic_has_correct_fields(self, templates_path):
        from ampa.brief_intake import _build_plan, _load_templates

        templates = _load_templates(templates_path)
        plan = _build_plan(SAMPLE_BRIEF, templates)

        epic = plan["epic"]
        assert "title" in epic
        assert "description" in epic
        assert epic["issue_type"] == "epic"
        assert epic["priority"] == "high"

    def test_child_title_includes_brief_title(self, templates_path):
        from ampa.brief_intake import _build_plan, _load_templates

        templates = _load_templates(templates_path)
        plan = _build_plan(SAMPLE_BRIEF, templates)

        for child in plan["children"]:
            assert "Build a User Authentication System" in child["title"]

    def test_acceptance_criteria_present_in_each_child(self, templates_path):
        from ampa.brief_intake import _build_plan, _load_templates

        templates = _load_templates(templates_path)
        plan = _build_plan(SAMPLE_BRIEF, templates)

        for child in plan["children"]:
            assert "Acceptance Criteria" in child["acceptance_criteria"]


# ---------------------------------------------------------------------------
# Unit tests: dry_run mode
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """dry_run mode must return a plan without any wl mutations."""

    @patch("ampa.brief_intake._run_wl")
    def test_dry_run_does_not_call_wl(self, mock_wl, templates_path):
        from ampa.brief_intake import brief_to_epic

        plan = brief_to_epic(SAMPLE_BRIEF, mode="dry_run", template_path=templates_path)
        mock_wl.assert_not_called()
        assert plan["mode"] == "dry_run"

    def test_dry_run_returns_plan_with_epic_and_children(self, templates_path):
        from ampa.brief_intake import brief_to_epic

        plan = brief_to_epic(SAMPLE_BRIEF, mode="dry_run", template_path=templates_path)
        assert "epic" in plan
        assert "children" in plan
        assert len(plan["children"]) > 0

    def test_dry_run_plan_is_json_serializable(self, templates_path):
        from ampa.brief_intake import brief_to_epic

        plan = brief_to_epic(SAMPLE_BRIEF, mode="dry_run", template_path=templates_path)
        # Should not raise
        json.dumps(plan)


# ---------------------------------------------------------------------------
# Unit tests: propose mode
# ---------------------------------------------------------------------------


class TestProposeMode:
    """propose mode must call wl create for epic + children."""

    def _mock_wl_create(self, call_log: List[Dict[str, Any]]):
        """Return a side_effect function that logs calls and returns fake IDs."""
        counter = {"n": 0}

        def _side_effect(args, cwd=None, timeout=300):
            counter["n"] += 1
            call_log.append({"args": args, "cwd": cwd})
            fake_id = f"WL-FAKE-{counter['n']}"
            # wl create returns
            if args[0] == "create":
                return {"workItem": {"id": fake_id}}
            # wl comment add
            return {"success": True}

        return _side_effect

    @patch("ampa.brief_intake._run_wl")
    def test_propose_creates_epic(self, mock_wl, templates_path):
        from ampa.brief_intake import brief_to_epic

        mock_wl.side_effect = self._mock_wl_create([])
        plan = brief_to_epic(SAMPLE_BRIEF, mode="propose", template_path=templates_path)
        assert plan["mode"] == "propose"
        assert "epic_id" in plan
        assert plan["epic_id"].startswith("WL-FAKE")

    @patch("ampa.brief_intake._run_wl")
    def test_propose_creates_children_with_parent(self, mock_wl, templates_path):
        from ampa.brief_intake import brief_to_epic

        call_log: List[Dict[str, Any]] = []
        mock_wl.side_effect = self._mock_wl_create(call_log)
        plan = brief_to_epic(SAMPLE_BRIEF, mode="propose", template_path=templates_path)

        # Should have child_ids
        assert "child_ids" in plan
        assert len(plan["child_ids"]) == 2  # discovery + implementation

        # Check that create calls include --parent with the epic ID
        create_calls = [c for c in call_log if c["args"][0] == "create"]
        # First create call is the epic (no parent)
        assert "--parent" not in create_calls[0]["args"]
        # Subsequent calls are children (with parent)
        for child_call in create_calls[1:]:
            assert "--parent" in child_call["args"]
            parent_idx = child_call["args"].index("--parent")
            assert child_call["args"][parent_idx + 1] == plan["epic_id"]

    @patch("ampa.brief_intake._run_wl")
    def test_propose_posts_delegation_comment(self, mock_wl, templates_path):
        from ampa.brief_intake import brief_to_epic

        call_log: List[Dict[str, Any]] = []
        mock_wl.side_effect = self._mock_wl_create(call_log)
        brief_to_epic(SAMPLE_BRIEF, mode="propose", template_path=templates_path)

        # The last call should be a comment add
        comment_calls = [c for c in call_log if c["args"][0] == "comment"]
        assert len(comment_calls) >= 1
        comment_args = comment_calls[0]["args"]
        assert "add" in comment_args
        # Find the comment text
        comment_idx = comment_args.index("--comment")
        comment_text = comment_args[comment_idx + 1]
        assert "Delegation Plan" in comment_text
        assert "analysis-agent" in comment_text or "dev-agent" in comment_text

    @patch("ampa.brief_intake._run_wl")
    def test_propose_child_ids_match_count(self, mock_wl, templates_path):
        from ampa.brief_intake import brief_to_epic

        mock_wl.side_effect = self._mock_wl_create([])
        plan = brief_to_epic(SAMPLE_BRIEF, mode="propose", template_path=templates_path)
        assert len(plan["child_ids"]) == len(plan["children"])
        for cid in plan["child_ids"]:
            assert cid.startswith("WL-FAKE")


# ---------------------------------------------------------------------------
# Unit tests: validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Input validation tests."""

    def test_invalid_mode_raises(self, templates_path):
        from ampa.brief_intake import brief_to_epic

        with pytest.raises(ValueError, match="Invalid mode"):
            brief_to_epic(SAMPLE_BRIEF, mode="invalid", template_path=templates_path)

    def test_empty_brief_raises(self, templates_path):
        from ampa.brief_intake import brief_to_epic

        with pytest.raises(ValueError, match="empty"):
            brief_to_epic("", mode="dry_run", template_path=templates_path)

    def test_whitespace_brief_raises(self, templates_path):
        from ampa.brief_intake import brief_to_epic

        with pytest.raises(ValueError, match="empty"):
            brief_to_epic("   \n\n  ", mode="dry_run", template_path=templates_path)


# ---------------------------------------------------------------------------
# Unit tests: default templates
# ---------------------------------------------------------------------------


class TestDefaultTemplates:
    """Tests using the real default task_templates.yaml."""

    def test_default_templates_load(self):
        from ampa.brief_intake import _load_templates

        templates = _load_templates()
        assert len(templates) >= 5  # discovery, design, implementation, testing, docs

    def test_default_plan_covers_all_categories(self):
        from ampa.brief_intake import _build_plan, _load_templates

        templates = _load_templates()
        plan = _build_plan(SAMPLE_BRIEF, templates)
        categories = {c["category"] for c in plan["children"]}
        assert "discovery" in categories
        assert "design" in categories
        assert "implementation" in categories
        assert "testing" in categories
        assert "documentation" in categories

    def test_default_dry_run_produces_complete_plan(self):
        from ampa.brief_intake import brief_to_epic

        plan = brief_to_epic(SAMPLE_BRIEF, mode="dry_run")
        assert len(plan["children"]) == 5
        for child in plan["children"]:
            assert child["acceptance_criteria"]
            assert child["suggested_assignee"]


# ---------------------------------------------------------------------------
# Unit tests: CLI
# ---------------------------------------------------------------------------


class TestCLI:
    """Tests for the CLI entry point."""

    def test_cli_dry_run(self, capsys, templates_path):
        from ampa.brief_intake import main

        main(
            [
                "--brief",
                SAMPLE_BRIEF,
                "--mode",
                "dry_run",
                "--templates",
                templates_path,
            ]
        )
        captured = capsys.readouterr()
        plan = json.loads(captured.out)
        assert plan["mode"] == "dry_run"
        assert "children" in plan

    def test_cli_reads_from_file(self, tmp_path, capsys, templates_path):
        from ampa.brief_intake import main

        brief_file = tmp_path / "brief.txt"
        brief_file.write_text(SAMPLE_BRIEF)

        main(
            [
                "--brief",
                f"@{brief_file}",
                "--mode",
                "dry_run",
                "--templates",
                templates_path,
            ]
        )
        captured = capsys.readouterr()
        plan = json.loads(captured.out)
        assert "Build a User Authentication System" in plan["epic"]["title"]
