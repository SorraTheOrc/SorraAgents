"""Tests for ampa.delegation_planner."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import pytest

from ampa.delegation_planner import (
    _extract_ac_section,
    _get_agent_groups,
    _get_category_keywords,
    _get_category_rules,
    _infer_category,
    _load_config,
    _suggest_assignee,
    _build_delegation_plan,
    _format_delegation_comment,
    build_delegation_plan,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CONFIG: Dict[str, Any] = {
    "agent_groups": [
        {
            "name": "analysis-agent",
            "display_name": "Analysis Agent",
            "capabilities": ["discovery", "research", "analysis"],
            "description": "Performs discovery and analysis tasks.",
            "max_concurrent": 2,
        },
        {
            "name": "dev-agent",
            "display_name": "Development Agent",
            "capabilities": ["implementation", "coding", "feature"],
            "description": "Implements production code.",
            "max_concurrent": 3,
        },
        {
            "name": "test-agent",
            "display_name": "Test Agent",
            "capabilities": ["testing", "quality"],
            "description": "Creates and maintains tests.",
            "max_concurrent": 2,
        },
        {
            "name": "docs-agent",
            "display_name": "Documentation Agent",
            "capabilities": ["documentation"],
            "description": "Authors documentation.",
            "max_concurrent": 2,
        },
    ],
    "category_rules": {
        "discovery": "analysis-agent",
        "research": "analysis-agent",
        "analysis": "analysis-agent",
        "implementation": "dev-agent",
        "feature": "dev-agent",
        "bug": "dev-agent",
        "testing": "test-agent",
        "documentation": "docs-agent",
    },
    "category_keywords": {
        "discovery": ["discover", "research", "spike", "analys"],
        "implementation": ["implement", "build", "creat", "feature", "refactor"],
        "testing": ["test", "validat", "verif", "coverage"],
        "documentation": ["doc(?:ument)?", "readme", "runbook"],
    },
}


def _make_work_item(
    id: str = "SA-TEST1",
    title: str = "Test Work Item",
    description: str = "A test work item description",
    status: str = "open",
    priority: str = "high",
    issue_type: str = "epic",
) -> Dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "issueType": issue_type,
    }


def _make_child(
    id: str,
    title: str,
    description: str = "",
    status: str = "open",
    priority: str = "medium",
    issue_type: str = "task",
    assignee: str = "",
) -> Dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "issueType": issue_type,
        "assignee": assignee,
    }


def _make_wl_response(
    work_item: Dict[str, Any],
    children: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "success": True,
        "workItem": work_item,
        "children": children or [],
        "comments": [],
    }


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_load_default_config(self):
        """Default config file loads successfully."""
        config = _load_config()
        assert "agent_groups" in config
        assert "category_rules" in config
        assert "category_keywords" in config

    def test_load_missing_config_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _load_config(str(tmp_path / "nonexistent.yaml"))

    def test_get_agent_groups(self):
        groups = _get_agent_groups(SAMPLE_CONFIG)
        assert "analysis-agent" in groups
        assert "dev-agent" in groups
        assert groups["analysis-agent"]["display_name"] == "Analysis Agent"

    def test_get_category_rules(self):
        rules = _get_category_rules(SAMPLE_CONFIG)
        assert rules["discovery"] == "analysis-agent"
        assert rules["implementation"] == "dev-agent"
        assert rules["testing"] == "test-agent"

    def test_get_category_keywords(self):
        keywords = _get_category_keywords(SAMPLE_CONFIG)
        assert "discovery" in keywords
        assert "implement" in keywords.get("implementation", [])


# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------


class TestCategoryInference:
    def test_infer_from_issue_type(self):
        cat = _infer_category(
            "Some title",
            "Some desc",
            "bug",
            SAMPLE_CONFIG["category_keywords"],
            SAMPLE_CONFIG["category_rules"],
        )
        assert cat == "bug"

    def test_infer_from_title_keywords(self):
        cat = _infer_category(
            "Discovery & Analysis: scope review",
            "review the scope",
            "task",
            SAMPLE_CONFIG["category_keywords"],
            SAMPLE_CONFIG["category_rules"],
        )
        assert cat == "discovery"

    def test_infer_from_description_keywords(self):
        cat = _infer_category(
            "Write unit tests",
            "Validate all edge cases and verify coverage",
            "task",
            SAMPLE_CONFIG["category_keywords"],
            SAMPLE_CONFIG["category_rules"],
        )
        assert cat == "testing"

    def test_infer_implementation_keywords(self):
        cat = _infer_category(
            "Implement the feature",
            "Build the new module",
            "task",
            SAMPLE_CONFIG["category_keywords"],
            SAMPLE_CONFIG["category_rules"],
        )
        assert cat == "implementation"

    def test_infer_documentation_keywords(self):
        cat = _infer_category(
            "Write documentation",
            "Create the readme and runbook",
            "task",
            SAMPLE_CONFIG["category_keywords"],
            SAMPLE_CONFIG["category_rules"],
        )
        assert cat == "documentation"

    def test_infer_defaults_to_implementation(self):
        cat = _infer_category(
            "Something vague",
            "No meaningful keywords here",
            "task",
            SAMPLE_CONFIG["category_keywords"],
            SAMPLE_CONFIG["category_rules"],
        )
        assert cat == "implementation"


class TestSuggestAssignee:
    def test_suggest_known_category(self):
        groups = _get_agent_groups(SAMPLE_CONFIG)
        rules = _get_category_rules(SAMPLE_CONFIG)
        name, rationale = _suggest_assignee("discovery", rules, groups)
        assert name == "analysis-agent"
        assert "discovery" in rationale
        assert "analysis-agent" in rationale

    def test_suggest_unknown_category_defaults_to_dev(self):
        groups = _get_agent_groups(SAMPLE_CONFIG)
        rules = _get_category_rules(SAMPLE_CONFIG)
        name, rationale = _suggest_assignee("unknown-category", rules, groups)
        assert name == "dev-agent"

    def test_suggest_testing_category(self):
        groups = _get_agent_groups(SAMPLE_CONFIG)
        rules = _get_category_rules(SAMPLE_CONFIG)
        name, rationale = _suggest_assignee("testing", rules, groups)
        assert name == "test-agent"


# ---------------------------------------------------------------------------
# AC extraction
# ---------------------------------------------------------------------------


class TestExtractAC:
    def test_extract_ac_section(self):
        desc = textwrap.dedent("""\
            Some description.

            ## Acceptance Criteria
            1. Criterion one.
            2. Criterion two.

            ## Notes
            Some notes.
        """)
        ac = _extract_ac_section(desc)
        assert "Criterion one" in ac
        assert "Criterion two" in ac
        assert "Notes" not in ac

    def test_extract_ac_missing(self):
        assert _extract_ac_section("No acceptance criteria here.") == ""

    def test_extract_ac_empty_desc(self):
        assert _extract_ac_section("") == ""
        assert _extract_ac_section(None) == ""


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


class TestBuildDelegationPlan:
    def test_plan_with_open_children(self):
        wi = _make_work_item()
        children = [
            _make_child("SA-C1", "Discovery: scope"),
            _make_child("SA-C2", "Implementation: build it", assignee="dev-agent"),
            _make_child("SA-C3", "Testing: verify", status="completed"),
        ]
        plan = _build_delegation_plan(wi, children, SAMPLE_CONFIG)

        assert plan["work_item"]["id"] == "SA-TEST1"
        assert plan["existing_children_count"] == 3
        # Only 2 open children should have delegations
        assert len(plan["proposed_delegations"]) == 2
        assert plan["summary"]["open_children"] == 2
        assert plan["summary"]["completed_children"] == 1

    def test_plan_with_no_children(self):
        wi = _make_work_item()
        plan = _build_delegation_plan(wi, [], SAMPLE_CONFIG)
        assert plan["existing_children_count"] == 0
        assert len(plan["proposed_delegations"]) == 0
        assert plan["summary"]["open_children"] == 0

    def test_plan_identifies_unassigned(self):
        wi = _make_work_item()
        children = [
            _make_child("SA-C1", "Task A"),
            _make_child("SA-C2", "Task B", assignee="someone"),
        ]
        plan = _build_delegation_plan(wi, children, SAMPLE_CONFIG)
        assert plan["summary"]["unassigned_children"] == 1

    def test_plan_includes_rationale(self):
        wi = _make_work_item()
        children = [_make_child("SA-C1", "Discovery: analyse scope")]
        plan = _build_delegation_plan(wi, children, SAMPLE_CONFIG)
        delegation = plan["proposed_delegations"][0]
        assert delegation["rationale"]
        assert delegation["suggested_assignee"]

    def test_plan_skips_deleted_children(self):
        wi = _make_work_item()
        children = [
            _make_child("SA-C1", "Open task"),
            _make_child("SA-C2", "Deleted task", status="deleted"),
        ]
        plan = _build_delegation_plan(wi, children, SAMPLE_CONFIG)
        assert len(plan["proposed_delegations"]) == 1

    def test_plan_categories_and_agents(self):
        wi = _make_work_item()
        children = [
            _make_child("SA-C1", "Discovery: scope review"),
            _make_child("SA-C2", "Write tests for the module"),
        ]
        plan = _build_delegation_plan(wi, children, SAMPLE_CONFIG)
        cats = plan["summary"]["categories_covered"]
        agents = plan["summary"]["agent_groups_suggested"]
        assert len(cats) >= 1
        assert len(agents) >= 1

    def test_plan_preserves_child_details(self):
        wi = _make_work_item()
        desc = "## Acceptance Criteria\n1. Do the thing."
        children = [_make_child("SA-C1", "Task A", description=desc, priority="high")]
        plan = _build_delegation_plan(wi, children, SAMPLE_CONFIG)
        d = plan["proposed_delegations"][0]
        assert d["child_id"] == "SA-C1"
        assert d["priority"] == "high"
        assert "Do the thing" in d["acceptance_criteria"]


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------


class TestFormatComment:
    def test_format_includes_required_sections(self):
        plan = {
            "work_item": {
                "id": "SA-X",
                "title": "Test",
                "status": "open",
                "priority": "high",
            },
            "proposed_delegations": [
                {
                    "child_id": "SA-C1",
                    "title": "Task A",
                    "category": "implementation",
                    "issue_type": "task",
                    "priority": "medium",
                    "current_assignee": "",
                    "current_status": "open",
                    "suggested_assignee": "dev-agent",
                    "rationale": "Implementation maps to dev-agent.",
                    "needs_assignment": True,
                    "acceptance_criteria": "1. Do something.",
                },
            ],
            "summary": {
                "work_item_id": "SA-X",
                "work_item_title": "Test",
                "total_children": 1,
                "open_children": 1,
                "completed_children": 0,
                "unassigned_children": 1,
                "categories_covered": ["implementation"],
                "agent_groups_suggested": ["dev-agent"],
            },
        }
        comment = _format_delegation_comment(plan)
        assert "# APMA Delegation Plan" in comment
        assert "SA-X" in comment
        assert "Task A" in comment
        assert "dev-agent" in comment
        assert "## Proposed Delegations" in comment
        assert "## Summary" in comment

    def test_format_handles_multiple_delegations(self):
        plan = {
            "work_item": {
                "id": "SA-X",
                "title": "Test",
                "status": "open",
                "priority": "high",
            },
            "proposed_delegations": [
                {
                    "child_id": f"SA-C{i}",
                    "title": f"Task {i}",
                    "category": "implementation",
                    "issue_type": "task",
                    "priority": "medium",
                    "current_assignee": "",
                    "current_status": "open",
                    "suggested_assignee": "dev-agent",
                    "rationale": "Reason.",
                    "needs_assignment": True,
                    "acceptance_criteria": "",
                }
                for i in range(3)
            ],
            "summary": {
                "work_item_id": "SA-X",
                "work_item_title": "Test",
                "total_children": 3,
                "open_children": 3,
                "completed_children": 0,
                "unassigned_children": 3,
                "categories_covered": ["implementation"],
                "agent_groups_suggested": ["dev-agent"],
            },
        }
        comment = _format_delegation_comment(plan)
        assert comment.count("### Task") == 3


# ---------------------------------------------------------------------------
# Public API: dry_run mode
# ---------------------------------------------------------------------------


class TestBuildDelegationPlanDryRun:
    def test_dry_run_does_not_call_wl(self):
        wi = _make_work_item()
        children = [_make_child("SA-C1", "Test task")]
        wl_response = _make_wl_response(wi, children)

        fetcher_calls = []

        def mock_fetcher(wid, cwd=None):
            fetcher_calls.append(wid)
            return wl_response

        plan = build_delegation_plan(
            "SA-TEST1",
            mode="dry_run",
            _wl_fetcher=mock_fetcher,
        )
        assert plan["mode"] == "dry_run"
        assert "comment_posted" not in plan
        assert len(fetcher_calls) == 1

    def test_dry_run_returns_complete_plan(self):
        wi = _make_work_item()
        children = [
            _make_child("SA-C1", "Discovery: scope"),
            _make_child("SA-C2", "Build feature", status="completed"),
        ]

        plan = build_delegation_plan(
            "SA-TEST1",
            mode="dry_run",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
        )
        assert plan["work_item"]["id"] == "SA-TEST1"
        assert plan["existing_children_count"] == 2
        assert len(plan["proposed_delegations"]) == 1
        assert plan["summary"]["completed_children"] == 1

    def test_dry_run_plan_is_json_serializable(self):
        wi = _make_work_item()
        children = [_make_child("SA-C1", "Task")]

        plan = build_delegation_plan(
            "SA-TEST1",
            mode="dry_run",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
        )
        serialized = json.dumps(plan)
        assert serialized
        parsed = json.loads(serialized)
        assert parsed["mode"] == "dry_run"


# ---------------------------------------------------------------------------
# Public API: propose mode
# ---------------------------------------------------------------------------


class TestBuildDelegationPlanPropose:
    def test_propose_posts_comment(self):
        wi = _make_work_item()
        children = [_make_child("SA-C1", "Task A")]
        comment_calls = []

        def mock_commenter(wid, comment, cwd=None):
            comment_calls.append((wid, comment))
            return True

        plan = build_delegation_plan(
            "SA-TEST1",
            mode="propose",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
            _wl_commenter=mock_commenter,
        )
        assert plan["mode"] == "propose"
        assert plan["comment_posted"] is True
        assert len(comment_calls) == 1
        assert comment_calls[0][0] == "SA-TEST1"
        assert "APMA Delegation Plan" in comment_calls[0][1]

    def test_propose_handles_comment_failure(self):
        wi = _make_work_item()
        children = [_make_child("SA-C1", "Task A")]

        plan = build_delegation_plan(
            "SA-TEST1",
            mode="propose",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
            _wl_commenter=lambda wid, comment, cwd=None: False,
        )
        assert plan["comment_posted"] is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid mode"):
            build_delegation_plan("SA-X", mode="execute")

    def test_empty_work_item_id_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            build_delegation_plan("")

    def test_whitespace_work_item_id_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            build_delegation_plan("   ")

    def test_missing_work_item_in_response_raises(self):
        def mock_fetcher(wid, cwd=None):
            return {"success": True, "workItem": {}, "children": []}

        with pytest.raises(RuntimeError, match="missing"):
            build_delegation_plan(
                "SA-BAD",
                mode="dry_run",
                _wl_fetcher=mock_fetcher,
            )


# ---------------------------------------------------------------------------
# Default config integration
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    def test_default_config_has_all_required_sections(self):
        config = _load_config()
        assert "agent_groups" in config
        assert "category_rules" in config
        assert "category_keywords" in config
        assert len(config["agent_groups"]) >= 4

    def test_default_config_groups_are_referenced_in_rules(self):
        config = _load_config()
        group_names = {g["name"] for g in config["agent_groups"]}
        rule_agents = set(config["category_rules"].values())
        # All agents referenced in rules should be defined as groups
        assert rule_agents.issubset(group_names), (
            f"Rules reference undefined groups: {rule_agents - group_names}"
        )

    def test_default_plan_with_realistic_data(self):
        wi = _make_work_item(
            id="SA-EPIC1",
            title="Build auth system",
            description="Implement user authentication with OAuth2.",
        )
        children = [
            _make_child("SA-C1", "Discovery & Analysis: auth requirements"),
            _make_child("SA-C2", "Implementation: OAuth2 integration"),
            _make_child("SA-C3", "Testing: auth flows", assignee="test-agent"),
            _make_child("SA-C4", "Documentation: auth guide", status="completed"),
        ]

        plan = build_delegation_plan(
            "SA-EPIC1",
            mode="dry_run",
            _wl_fetcher=lambda wid, cwd=None: _make_wl_response(wi, children),
        )
        assert plan["existing_children_count"] == 4
        assert len(plan["proposed_delegations"]) == 3  # 3 open
        assert plan["summary"]["completed_children"] == 1
        assert plan["summary"]["unassigned_children"] == 2  # C3 has assignee


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_dry_run(self, capsys):
        wi = _make_work_item()
        children = [_make_child("SA-C1", "Task A")]

        with mock.patch(
            "ampa.delegation_planner._fetch_work_item",
            return_value=_make_wl_response(wi, children),
        ):
            main(["--work-item", "SA-TEST1", "--mode", "dry_run"])

        output = capsys.readouterr().out
        parsed = json.loads(output)
        assert parsed["mode"] == "dry_run"
        assert parsed["work_item"]["id"] == "SA-TEST1"

    def test_cli_propose(self, capsys):
        wi = _make_work_item()
        children = [_make_child("SA-C1", "Task A")]

        with (
            mock.patch(
                "ampa.delegation_planner._fetch_work_item",
                return_value=_make_wl_response(wi, children),
            ),
            mock.patch(
                "ampa.delegation_planner._add_comment",
                return_value=True,
            ),
        ):
            main(["--work-item", "SA-TEST1", "--mode", "propose"])

        output = capsys.readouterr().out
        parsed = json.loads(output)
        assert parsed["mode"] == "propose"
        assert parsed["comment_posted"] is True
