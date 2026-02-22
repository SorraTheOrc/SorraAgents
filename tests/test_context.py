"""Tests for ampa.engine.context — context assembler.

Covers:
- Acceptance criteria extraction (AC section, checkboxes, mixed)
- Comment extraction (multiple formats)
- Parent context extraction (ancestors, parentId fallback)
- Child item extraction
- ContextAssembler end-to-end: full context, no parent, no children, no AC
- Shell command building for each action (intake, plan, implement)
- Stage-to-action mapping
"""

from __future__ import annotations

from typing import Any

import pytest

from ampa.engine.context import (
    ChildItem,
    Comment,
    ContextAssembler,
    DelegationContext,
    ParentContext,
    build_dispatch_command,
    extract_acceptance_criteria,
    stage_to_action,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class FakeWorkItemFetcher:
    """Fake WorkItemFetcher returning canned responses."""

    def __init__(
        self, responses: dict[str, dict[str, Any] | None] | None = None
    ) -> None:
        self._responses = responses or {}

    def fetch(self, work_item_id: str) -> dict[str, Any] | None:
        return self._responses.get(work_item_id)


def _make_payload(
    id: str = "WL-1",
    title: str = "Test item",
    description: str = "A work item description.",
    status: str = "open",
    stage: str = "plan_complete",
    priority: str = "medium",
    tags: list[str] | None = None,
    assignee: str = "",
    parentId: str = "",
    comments: list[dict[str, Any]] | None = None,
    children: list[dict[str, Any]] | None = None,
    ancestors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a wl show --children --json style payload."""
    wi: dict[str, Any] = {
        "id": id,
        "title": title,
        "description": description,
        "status": status,
        "stage": stage,
        "priority": priority,
        "assignee": assignee,
    }
    if tags is not None:
        wi["tags"] = tags
    if parentId:
        wi["parentId"] = parentId

    payload: dict[str, Any] = {
        "workItem": wi,
    }
    if comments is not None:
        payload["comments"] = comments
    if children is not None:
        payload["children"] = children
    if ancestors is not None:
        payload["ancestors"] = ancestors
    return payload


# ---------------------------------------------------------------------------
# extract_acceptance_criteria tests
# ---------------------------------------------------------------------------


class TestExtractAcceptanceCriteria:
    def test_ac_section_with_checkboxes(self):
        desc = """\
## Summary

Some summary text.

## Acceptance Criteria

- [ ] First criterion
- [x] Second criterion is done
- [ ] Third criterion

## References

Some refs.
"""
        criteria = extract_acceptance_criteria(desc)
        assert len(criteria) == 3
        assert "First criterion" in criteria[0]
        assert "Second criterion is done" in criteria[1]
        assert "Third criterion" in criteria[2]

    def test_ac_section_without_checkboxes(self):
        desc = """\
## Acceptance Criteria

- Users can log in
- Dashboard loads in under 2 seconds
- Error messages are clear
"""
        criteria = extract_acceptance_criteria(desc)
        assert len(criteria) == 3
        assert "Users can log in" in criteria[0]

    def test_checkboxes_without_ac_section(self):
        desc = """\
## Requirements

- [ ] Must support Python 3.10+
- [ ] Must have unit tests
- [x] Must use existing deps
"""
        criteria = extract_acceptance_criteria(desc)
        assert len(criteria) == 3
        assert "Must support Python 3.10+" in criteria[0]

    def test_no_acceptance_criteria(self):
        desc = "Just a plain description with no criteria."
        criteria = extract_acceptance_criteria(desc)
        assert len(criteria) == 0

    def test_empty_description(self):
        criteria = extract_acceptance_criteria("")
        assert criteria == ()

    def test_ac_section_at_end_of_description(self):
        desc = """\
## Summary

Work item.

## Acceptance Criteria

- [ ] Only criterion
"""
        criteria = extract_acceptance_criteria(desc)
        assert len(criteria) == 1
        assert "Only criterion" in criteria[0]

    def test_mixed_heading_levels(self):
        desc = """\
# Title

### Acceptance Criteria

- [ ] AC item one
- [ ] AC item two
"""
        criteria = extract_acceptance_criteria(desc)
        assert len(criteria) == 2

    def test_deduplication(self):
        desc = """\
## Acceptance Criteria

- [ ] Same criterion
- [ ] Same criterion
- [ ] Different criterion
"""
        criteria = extract_acceptance_criteria(desc)
        assert len(criteria) == 2

    def test_asterisk_checkboxes(self):
        desc = """\
## Acceptance Criteria

* [ ] Star checkbox one
* [x] Star checkbox two
"""
        criteria = extract_acceptance_criteria(desc)
        assert len(criteria) == 2
        assert "Star checkbox one" in criteria[0]


# ---------------------------------------------------------------------------
# Comment extraction tests
# ---------------------------------------------------------------------------


class TestCommentExtraction:
    def test_standard_comments(self):
        payload = _make_payload(
            comments=[
                {
                    "author": "alice",
                    "comment": "Looks good",
                    "createdAt": "2026-01-01T00:00:00Z",
                },
                {
                    "author": "bob",
                    "comment": "Need changes",
                    "createdAt": "2026-01-02T00:00:00Z",
                },
            ]
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert len(ctx.comments) == 2
        assert ctx.comments[0].author == "alice"
        assert ctx.comments[0].content == "Looks good"
        assert ctx.comments[0].timestamp == "2026-01-01T00:00:00Z"

    def test_body_key_comments(self):
        payload = _make_payload(
            comments=[
                {"user": "carol", "body": "Body text", "timestamp": "2026-02-01"},
            ]
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert len(ctx.comments) == 1
        assert ctx.comments[0].author == "carol"
        assert ctx.comments[0].content == "Body text"

    def test_empty_comments(self):
        payload = _make_payload(comments=[])
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert len(ctx.comments) == 0

    def test_no_comments_key(self):
        payload = _make_payload()
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert len(ctx.comments) == 0

    def test_empty_comment_text_skipped(self):
        payload = _make_payload(
            comments=[
                {"author": "alice", "comment": ""},
                {"author": "bob", "comment": "Real comment"},
            ]
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert len(ctx.comments) == 1
        assert ctx.comments[0].author == "bob"


# ---------------------------------------------------------------------------
# Parent context extraction tests
# ---------------------------------------------------------------------------


class TestParentContextExtraction:
    def test_ancestors_list(self):
        payload = _make_payload(
            parentId="WL-PARENT",
            ancestors=[
                {
                    "id": "WL-PARENT",
                    "title": "Parent Epic",
                    "description": "Parent description text.",
                    "status": "open",
                    "stage": "plan_complete",
                },
            ],
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert ctx.parent_context is not None
        assert ctx.parent_context.id == "WL-PARENT"
        assert ctx.parent_context.title == "Parent Epic"
        assert ctx.parent_context.description == "Parent description text."

    def test_ancestors_long_description_truncated(self):
        long_desc = "x" * 1000
        payload = _make_payload(
            parentId="WL-P",
            ancestors=[
                {"id": "WL-P", "title": "Parent", "description": long_desc},
            ],
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(
            work_item_fetcher=fetcher, max_parent_description=100
        )
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert ctx.parent_context is not None
        assert len(ctx.parent_context.description) <= 104  # 100 + "..."
        assert ctx.parent_context.description.endswith("...")

    def test_parent_id_fallback(self):
        payload = _make_payload(parentId="WL-PARENT")
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert ctx.parent_context is not None
        assert ctx.parent_context.id == "WL-PARENT"
        assert "not available" in ctx.parent_context.description

    def test_no_parent(self):
        payload = _make_payload()
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert ctx.parent_context is None

    def test_multiple_ancestors_uses_last(self):
        payload = _make_payload(
            parentId="WL-CHILD-PARENT",
            ancestors=[
                {"id": "WL-ROOT", "title": "Root", "description": "Root desc"},
                {
                    "id": "WL-CHILD-PARENT",
                    "title": "Direct Parent",
                    "description": "Direct parent desc",
                },
            ],
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert ctx.parent_context is not None
        assert ctx.parent_context.id == "WL-CHILD-PARENT"
        assert ctx.parent_context.title == "Direct Parent"


# ---------------------------------------------------------------------------
# Child item extraction tests
# ---------------------------------------------------------------------------


class TestChildItemExtraction:
    def test_children_present(self):
        payload = _make_payload(
            children=[
                {"id": "WL-C1", "title": "Child 1", "status": "open", "stage": "idea"},
                {
                    "id": "WL-C2",
                    "title": "Child 2",
                    "status": "closed",
                    "stage": "done",
                },
            ],
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert len(ctx.child_items) == 2
        assert ctx.child_items[0].id == "WL-C1"
        assert ctx.child_items[1].stage == "done"

    def test_no_children(self):
        payload = _make_payload(children=[])
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert len(ctx.child_items) == 0

    def test_children_missing_id_skipped(self):
        payload = _make_payload(
            children=[
                {"title": "No ID", "status": "open"},
                {"id": "WL-C1", "title": "Has ID", "status": "open"},
            ],
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert len(ctx.child_items) == 1
        assert ctx.child_items[0].id == "WL-C1"


# ---------------------------------------------------------------------------
# ContextAssembler end-to-end tests
# ---------------------------------------------------------------------------


class TestContextAssembler:
    def test_full_context_assembly(self):
        desc = """\
## Summary

Implement the widget feature.

## Acceptance Criteria

- [ ] Widget renders correctly
- [ ] Widget handles errors gracefully
"""
        payload = _make_payload(
            id="WL-42",
            title="Widget Feature",
            description=desc,
            status="open",
            stage="plan_complete",
            priority="high",
            tags=["feature", "v2"],
            assignee="patch-agent",
            parentId="WL-EPIC",
            comments=[
                {
                    "author": "alice",
                    "comment": "Ready for impl",
                    "createdAt": "2026-01-15",
                },
            ],
            children=[
                {"id": "WL-43", "title": "Sub-task", "status": "open", "stage": "idea"},
            ],
            ancestors=[
                {"id": "WL-EPIC", "title": "Epic", "description": "Epic desc"},
            ],
        )
        fetcher = FakeWorkItemFetcher({"WL-42": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-42")

        assert ctx is not None
        assert ctx.work_item_id == "WL-42"
        assert ctx.title == "Widget Feature"
        assert "widget feature" in ctx.description.lower()
        assert len(ctx.acceptance_criteria) == 2
        assert "Widget renders correctly" in ctx.acceptance_criteria[0]
        assert len(ctx.comments) == 1
        assert ctx.comments[0].author == "alice"
        assert ctx.parent_context is not None
        assert ctx.parent_context.id == "WL-EPIC"
        assert len(ctx.child_items) == 1
        assert ctx.child_items[0].id == "WL-43"
        assert ctx.tags == ("feature", "v2")
        assert ctx.stage == "plan_complete"
        assert ctx.status == "open"
        assert ctx.priority == "high"
        assert ctx.assignee == "patch-agent"

    def test_fetch_failure_returns_none(self):
        fetcher = FakeWorkItemFetcher({})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-MISSING")
        assert ctx is None

    def test_minimal_work_item(self):
        payload = _make_payload(
            id="WL-1",
            title="Minimal",
            description="Short description.",
        )
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert ctx.work_item_id == "WL-1"
        assert ctx.title == "Minimal"
        assert ctx.acceptance_criteria == ()
        assert ctx.comments == ()
        assert ctx.parent_context is None
        assert ctx.child_items == ()

    def test_work_item_without_wrapper(self):
        """Test with a payload that has no workItem wrapper."""
        payload: dict[str, Any] = {
            "id": "WL-1",
            "title": "Direct",
            "description": "No wrapper.",
            "status": "open",
            "stage": "idea",
        }
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert ctx.title == "Direct"

    def test_tags_from_comma_string(self):
        payload = _make_payload()
        payload["workItem"]["tags"] = "feature, urgent, v2"
        fetcher = FakeWorkItemFetcher({"WL-1": payload})
        assembler = ContextAssembler(work_item_fetcher=fetcher)
        ctx = assembler.assemble("WL-1")
        assert ctx is not None
        assert "feature" in ctx.tags
        assert "urgent" in ctx.tags


# ---------------------------------------------------------------------------
# Shell command builder tests
# ---------------------------------------------------------------------------


class TestStageToAction:
    def test_idea(self):
        assert stage_to_action("idea") == "intake"

    def test_intake_complete(self):
        assert stage_to_action("intake_complete") == "plan"

    def test_plan_complete(self):
        assert stage_to_action("plan_complete") == "implement"

    def test_unknown_stage(self):
        assert stage_to_action("in_review") is None

    def test_empty_stage(self):
        assert stage_to_action("") is None


class TestBuildDispatchCommand:
    def test_intake_command(self):
        cmd = build_dispatch_command("WL-42", "intake")
        assert cmd == 'opencode run "/intake WL-42 do not ask questions"'

    def test_plan_command(self):
        cmd = build_dispatch_command("WL-42", "plan")
        assert cmd == 'opencode run "/plan WL-42"'

    def test_implement_command(self):
        cmd = build_dispatch_command("WL-42", "implement")
        assert cmd == 'opencode run "work on WL-42 using the implement skill"'

    def test_unknown_action(self):
        cmd = build_dispatch_command("WL-42", "unknown")
        assert cmd is None

    def test_special_characters_in_id(self):
        cmd = build_dispatch_command("SA-0MLX8FNGJ0IYN1LN", "implement")
        assert "SA-0MLX8FNGJ0IYN1LN" in cmd
        assert (
            cmd
            == 'opencode run "work on SA-0MLX8FNGJ0IYN1LN using the implement skill"'
        )
