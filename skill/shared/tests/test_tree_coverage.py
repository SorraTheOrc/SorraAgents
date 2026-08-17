"""Tests for the shared tree coverage helpers in skill/shared/tree_coverage.py.

Tests cover:
  - Tree fetch with recursive descent
  - Dependency-based ordering (topological sort)
  - Acceptance criteria extraction from markdown descriptions
  - Coverage computation (parent vs child ACs)
  - Gap resolution (auto-close unambiguous gaps)
  - Conflict detection (unresolvable gaps)
  - Full coverage review orchestration

Related work item: SA-0MSLRVQIF0040GAM
"""

import json

from skill.shared.tree_coverage import (
    compute_coverage,
    extract_acceptance_criteria,
    extract_acs_from_item,
    fetch_descendant_tree,
    jaccard_similarity,
    order_by_dependencies,
    resolve_coverage_gaps,
    run_coverage_review,
)

# =========================================================================
# Test fixtures: FakeRunner
# =========================================================================


class _FakeResult:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(
        self, stdout: str = "{}", returncode: int = 0, stderr: str = ""
    ):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _FakeWlRunner:
    """Runner that dispatches wl commands to registered handlers.

    Usage:
        runner = _FakeWlRunner()
        runner.register("show:SA-01", {...})   # wl show SA-01 --json
        runner.register("show:SA-01:children", [...])  # wl show SA-01 --children --json
        runner.register("dep:list:SA-01", [...])  # wl dep list SA-01 --json
    """

    def __init__(self):
        self._handlers: dict[str, dict | list] = {}

    def register(self, key: str, data: dict | list):
        self._handlers[key] = data

    def __call__(self, cmd):
        # Build a lookup key from the command
        parts = [c for c in cmd if c.startswith("SA-")]
        if not parts:
            return _FakeResult("{}", returncode=1)

        target_id = parts[0]

        # Determine command type and build key
        if "--children" in cmd:
            key = f"show:{target_id}:children"
        elif "dep" in cmd and "list" in cmd:
            key = f"dep:list:{target_id}"
        elif "show" in cmd:
            key = f"show:{target_id}"
        else:
            return _FakeResult("{}", returncode=1)

        data = self._handlers.get(key)
        if data is None:
            return _FakeResult("{}", returncode=1)

        # Handle dep list responses differently from show responses
        if key.startswith("dep:list:"):
            payload = json.dumps({"success": True, "dependencies": data})
        elif isinstance(data, list):
            payload = json.dumps({"success": True, "workItem": {"children": data}})
        else:
            payload = json.dumps({"success": True, "workItem": data})
        return _FakeResult(payload)


# =========================================================================
# 1. Tree fetch
# =========================================================================


class TestFetchDescendantTree:
    """Verify recursive tree fetching."""

    def test_empty_children_returns_single_node(self):
        """A leaf work item returns a tree with only itself."""
        runner = _FakeWlRunner()
        runner.register("show:SA-01:children", [])
        tree = fetch_descendant_tree("SA-01", runner=runner)
        assert "SA-01" in tree
        assert tree["SA-01"]["children"] == []

    def test_single_level_tree(self):
        """A parent with one child returns both nodes."""
        runner = _FakeWlRunner()
        runner.register("show:SA-01:children", [
            {"id": "SA-02", "title": "Child 1"},
        ])
        runner.register("show:SA-02:children", [])
        tree = fetch_descendant_tree("SA-01", runner=runner)
        assert "SA-01" in tree
        assert "SA-02" in tree
        assert tree["SA-01"]["children"] == ["SA-02"]
        assert tree["SA-02"]["children"] == []

    def test_multi_level_tree(self):
        """Grandchildren are fetched recursively."""
        runner = _FakeWlRunner()
        runner.register("show:SA-01:children", [
            {"id": "SA-02", "title": "Child 1"},
            {"id": "SA-03", "title": "Child 2"},
        ])
        runner.register("show:SA-02:children", [
            {"id": "SA-04", "title": "Grandchild 1"},
        ])
        runner.register("show:SA-03:children", [])
        runner.register("show:SA-04:children", [])
        tree = fetch_descendant_tree("SA-01", runner=runner)
        assert set(tree.keys()) == {"SA-01", "SA-02", "SA-03", "SA-04"}
        assert tree["SA-01"]["children"] == ["SA-02", "SA-03"]
        assert tree["SA-02"]["children"] == ["SA-04"]

    def test_cycle_detected_returns_pruned_tree(self):
        """When a cycle is detected, the branch is pruned."""
        runner = _FakeWlRunner()
        runner.register("show:SA-01:children", [
            {"id": "SA-02", "title": "Child"},
        ])
        runner.register("show:SA-02:children", [
            {"id": "SA-01", "title": "Back to parent"},
        ])
        tree = fetch_descendant_tree("SA-01", runner=runner)
        assert "SA-01" in tree
        assert "SA-02" in tree


# =========================================================================
# 2. Dependency ordering
# =========================================================================


class TestOrderByDependencies:
    """Verify topological ordering of children by ``wl dep`` edges."""

    def test_no_dependencies_preserves_order(self):
        """Children without dependencies keep their listed order."""
        runner = _FakeWlRunner()
        runner.register(
            "dep:list:SA-01",
            [],
        )
        children = [
            {"id": "SA-03", "title": "Third"},
            {"id": "SA-01", "title": "First"},
            {"id": "SA-02", "title": "Second"},
        ]
        ordered = order_by_dependencies(children, "SA-01", runner=runner)
        assert [c["id"] for c in ordered] == ["SA-03", "SA-01", "SA-02"]

    def test_dependencies_respected(self):
        """Children are ordered after their prerequisites."""
        runner = _FakeWlRunner()
        runner.register(
            "dep:list:SA-01",
            [
                {"targetId": "SA-02", "prerequisiteId": "SA-01"},
            ],
        )
        children = [
            {"id": "SA-02", "title": "Depends on SA-01"},
            {"id": "SA-01", "title": "Prerequisite"},
        ]
        ordered = order_by_dependencies(children, "SA-01", runner=runner)
        # SA-01 should come before SA-02
        assert ordered[0]["id"] == "SA-01"
        assert ordered[1]["id"] == "SA-02"

    def test_multi_level_dependencies(self):
        """Chain: A -> B -> C respects the full order."""
        runner = _FakeWlRunner()
        runner.register(
            "dep:list:SA-01",
            [
                {"targetId": "SA-02", "prerequisiteId": "SA-01"},
                {"targetId": "SA-03", "prerequisiteId": "SA-02"},
            ],
        )
        children = [
            {"id": "SA-03", "title": "Last"},
            {"id": "SA-01", "title": "First"},
            {"id": "SA-02", "title": "Middle"},
        ]
        ordered = order_by_dependencies(children, "SA-01", runner=runner)
        assert [c["id"] for c in ordered] == ["SA-01", "SA-02", "SA-03"]

    def test_empty_children(self):
        """Empty children list returns empty list."""
        runner = _FakeWlRunner()
        ordered = order_by_dependencies([], "SA-01", runner=runner)
        assert ordered == []

    def test_dep_list_failure_returns_original_order(self):
        """A failed ``wl dep list`` returns children in original order."""
        runner = _FakeWlRunner()
        # No dep list handler → returns []
        children = [
            {"id": "SA-02", "title": "Second"},
            {"id": "SA-01", "title": "First"},
        ]
        ordered = order_by_dependencies(children, "SA-01", runner=runner)
        assert [c["id"] for c in ordered] == ["SA-02", "SA-01"]


# =========================================================================
# 3. AC extraction
# =========================================================================


class TestExtractAcceptanceCriteria:
    """Verify acceptance criteria extraction from markdown descriptions."""

    def test_simple_acs(self):
        """Basic AC list is extracted correctly."""
        desc = """# My feature

## Acceptance Criteria
- AC 1: The system shall do X
- AC 2: The system shall do Y
"""
        acs = extract_acceptance_criteria(desc)
        assert acs == ["AC 1: The system shall do X", "AC 2: The system shall do Y"]

    def test_case_insensitive_header(self):
        """The section header is matched case-insensitively."""
        desc = """## acceptance criteria
- AC one
- AC two
"""
        acs = extract_acceptance_criteria(desc)
        assert len(acs) == 2
        assert "AC one" in acs

    def test_stops_at_next_heading(self):
        """Extraction stops at the next ``##`` heading."""
        desc = """## Acceptance Criteria
- AC 1
- AC 2

## Constraints
- Constraint 1
"""
        acs = extract_acceptance_criteria(desc)
        assert acs == ["AC 1", "AC 2"]

    def test_no_ac_section_returns_empty(self):
        """A description without an AC section returns an empty list."""
        desc = "# No acceptance criteria here"
        acs = extract_acceptance_criteria(desc)
        assert acs == []

    def test_empty_section_returns_empty(self):
        """An AC section with no bullets returns an empty list."""
        desc = """## Acceptance Criteria
Some text but no bullets.
"""
        acs = extract_acceptance_criteria(desc)
        assert acs == []

    def test_plus_bullets_included(self):
        """Both ``-`` and ``+`` bullets are collected."""
        desc = """## Acceptance Criteria
- Bullet one
+ Bullet two
- Bullet three
"""
        acs = extract_acceptance_criteria(desc)
        assert acs == ["Bullet one", "Bullet two", "Bullet three"]


# =========================================================================
# 4. Coverage computation
# =========================================================================


class TestComputeCoverage:
    """Verify collective coverage computation."""

    def test_fully_covered(self):
        """All parent ACs covered by child ACs."""
        parent_acs = ["Feature A login", "Feature B dashboard"]
        child_acs_list = [
            ["Feature A login implemented"],
            ["Feature B dashboard implemented"],
        ]
        result = compute_coverage(parent_acs, child_acs_list, similarity_threshold=0.4)
        assert result["fully_covered"] is True
        assert result["uncovered"] == []
        assert result["coverage_pct"] == 100.0

    def test_uncovered_parent_ac(self):
        """Uncovered parent ACs are reported."""
        parent_acs = ["Feature A", "Feature B", "Feature C"]
        child_acs_list = [["Feature A implementation"], ["Feature B implementation"]]
        result = compute_coverage(parent_acs, child_acs_list, similarity_threshold=0.4)
        assert result["fully_covered"] is False
        assert "Feature C" in result["uncovered"]
        # 2 out of 3 covered
        assert abs(result["coverage_pct"] - 66.66666666666666) < 0.01

    def test_multiple_children_cover_one_parent(self):
        """Multiple children can each cover a portion of one parent AC."""
        parent_acs = ["Feature handles both X and Y"]
        child_acs_list = [
            ["Handles X part"],
            ["Handles Y part"],
        ]
        result = compute_coverage(parent_acs, child_acs_list, similarity_threshold=0.4)
        assert result["fully_covered"] is False
        assert "Feature handles both X and Y" in result["uncovered"]

    def test_empty_parent_acs_returns_full_coverage(self):
        """A parent with no ACs is considered fully covered."""
        result = compute_coverage([], [["Child AC"]])
        assert result["fully_covered"] is True
        assert result["coverage_pct"] == 100.0

    def test_coverage_map_includes_child_indices(self):
        """The coverage map maps parent AC index to covering child AC indices."""
        parent_acs = ["Parent AC test coverage"]
        child_acs_list = [
            ["Parent AC test match child 1"],
            ["Parent AC test match child 2"],
        ]
        result = compute_coverage(parent_acs, child_acs_list, similarity_threshold=0.4)
        # With matching tokens, children cover the parent
        covering = result["coverage_map"].get(0, [])
        assert 0 in covering or 1 in covering


class TestJaccardSimilarity:
    """Verify word-level Jaccard similarity."""

    def test_identical_strings(self):
        assert jaccard_similarity("hello world", "hello world") == 1.0

    def test_completely_different(self):
        assert jaccard_similarity("hello world", "foo bar") == 0.0

    def test_partial_overlap(self):
        """Shared words increase similarity."""
        score = jaccard_similarity("the quick brown fox", "the quick red fox")
        assert 0.0 < score < 1.0

    def test_empty_strings(self):
        assert jaccard_similarity("", "") == 1.0
        assert jaccard_similarity("hello", "") == 0.0


# =========================================================================
# 5. Gap resolution
# =========================================================================


class TestResolveCoverageGaps:
    """Verify auto-close and conflict detection for coverage gaps."""

    def test_unambiguous_gap_auto_closes(self):
        """A near-identical child AC auto-closes the gap."""
        result = resolve_coverage_gaps(
            "System handles OAuth authentication",
            ["System handles OAuth authentication flow"],
            similarity_threshold=0.75,
        )
        assert result["resolved"] is True
        assert result["conflict"] is False

    def test_no_match_is_conflict(self):
        """A completely unrelated child AC cannot auto-close."""
        result = resolve_coverage_gaps(
            "System shall encrypt data at rest",
            ["User interface shows dashboard"],
            similarity_threshold=0.85,
        )
        assert result["resolved"] is False
        assert result["match_score"] == 0.0
        assert result["conflict"] is False  # No partial match at all

    def test_partial_match_is_conflict(self):
        """A partial match (some words) is a conflict, not a resolve."""
        result = resolve_coverage_gaps(
            "System handles OAuth authentication",
            ["System handles data encryption"],
            similarity_threshold=0.85,
        )
        assert result["resolved"] is False
        # "System" and "handles" are shared → some overlap
        assert result["match_score"] > 0.0
        assert result["conflict"] is True

    def test_partial_word_match_is_conflict(self):
        """A partial word match is a conflict, not a resolve."""
        result = resolve_coverage_gaps(
            "System shall authenticate users via OAuth",
            ["System handles authentication"],
            similarity_threshold=0.85,
        )
        assert result["resolved"] is False
        assert result["conflict"] is True  # 'system' and 'authenticat' overlap


# =========================================================================
# 6. Full coverage review
# =========================================================================


class TestRunCoverageReview:
    """Verify the full coverage review orchestrator."""

    def test_no_children_returns_proceed(self):
        """A parent with no children is considered fully covered."""
        runner = _FakeWlRunner()
        runner.register("show:SA-01:children", [])
        result = run_coverage_review("SA-01", runner=runner)
        assert result["recommendation"] == "proceed"
        assert result["coverage"]["fully_covered"] is True
        assert result["child_summary"] == []

    def test_child_with_matching_acs_proceeds(self):
        """When child ACs match parent ACs, recommendation is proceed."""
        runner = _FakeWlRunner()
        runner.register("show:SA-01:children", [
            {"id": "SA-02", "title": "Child"},
        ])
        runner.register(
            "show:SA-01",
            {
                "id": "SA-01",
                "title": "Parent",
                "description": (
                    "## Acceptance Criteria\n"
                    "- Feature A login implemented\n"
                    "- Feature B logout implemented\n"
                ),
            },
        )
        runner.register(
            "show:SA-02",
            {
                "id": "SA-02",
                "title": "Child",
                "description": (
                    "## Acceptance Criteria\n"
                    "- Feature A login implemented\n"
                    "- Feature B logout implemented\n"
                ),
            },
        )
        runner.register("dep:list:SA-01", [])

        result = run_coverage_review("SA-01", runner=runner)
        assert result["work_item_id"] == "SA-01"
        assert len(result["child_summary"]) == 1
        assert result["child_summary"][0]["id"] == "SA-02"

    def test_missing_coverage_returns_stop(self):
        """When parent ACs are not covered and cannot be auto-closed,
        recommendation is stop."""
        def _custom_runner(cmd):
            if "--children" in cmd:
                return _FakeResult(
                    json.dumps({
                        "success": True,
                        "workItem": {"children": [
                            {"id": "SA-02", "title": "Child"},
                        ]},
                    })
                )
            if "SA-01" in cmd:
                return _FakeResult(
                    json.dumps({
                        "success": True,
                        "workItem": {
                            "description": (
                                "## Acceptance Criteria\n"
                                "- System must encrypt all data\n"
                            ),
                        },
                    })
                )
            if "SA-02" in cmd:
                return _FakeResult(
                    json.dumps({
                        "success": True,
                        "workItem": {
                            "description": (
                                "## Acceptance Criteria\n"
                                "- User can view profile\n"
                            ),
                        },
                    })
                )
            return _FakeResult("{}")

        result = run_coverage_review("SA-01", runner=_custom_runner)
        assert result["recommendation"] == "stop"
        assert len(result["unresolvable_conflicts"]) >= 1

    def test_child_summary_includes_ac_count(self):
        """Child summary lists each child's AC count."""
        def _custom_runner(cmd):
            if "--children" in cmd:
                return _FakeResult(
                    json.dumps({
                        "success": True,
                        "workItem": {"children": [
                            {"id": "SA-02", "title": "Child 1"},
                            {"id": "SA-03", "title": "Child 2"},
                        ]},
                    })
                )
            if "SA-01" in cmd:
                return _FakeResult(
                    json.dumps({
                        "success": True,
                        "workItem": {
                            "description": "No ACs here.",
                        },
                    })
                )
            if "SA-02" in cmd:
                return _FakeResult(
                    json.dumps({
                        "success": True,
                        "workItem": {
                            "description": (
                                "## Acceptance Criteria\n"
                                "- AC one\n"
                                "- AC two\n"
                            ),
                        },
                    })
                )
            if "SA-03" in cmd:
                return _FakeResult(
                    json.dumps({
                        "success": True,
                        "workItem": {
                            "description": (
                                "## Acceptance Criteria\n"
                                "- AC three\n"
                            ),
                        },
                    })
                )
            return _FakeResult("{}")

        result = run_coverage_review("SA-01", runner=_custom_runner)
        child_ids = {c["id"] for c in result["child_summary"]}
        assert "SA-02" in child_ids
        assert "SA-03" in child_ids
        for child in result["child_summary"]:
            if child["id"] == "SA-02":
                assert child["ac_count"] == 2
            elif child["id"] == "SA-03":
                assert child["ac_count"] == 1


class TestExtractAcsFromItem:
    """Verify AC extraction via ``wl show`` subprocess."""

    def test_success_returns_acs(self):
        """A successful fetch extracts ACs from the description."""
        runner = _FakeWlRunner()
        runner.register(
            "show:SA-01",
            {
                "id": "SA-01",
                "title": "Test",
                "description": (
                    "## Acceptance Criteria\n"
                    "- First AC\n"
                    "- Second AC\n"
                ),
            },
        )
        acs = extract_acs_from_item("SA-01", runner=runner)
        assert "First AC" in acs
        assert "Second AC" in acs

    def test_failed_fetch_returns_empty(self):
        """A failed fetch returns an empty list."""
        acs = extract_acs_from_item("SA-01", runner=_FakeWlRunner())
        assert acs == []


# =========================================================================
# 7. Integration: coverage_map structure
# =========================================================================


class TestCoverageMapStructure:
    """Verify the internal structure of coverage_map."""

    def test_coverage_map_keys_are_parent_indices(self):
        """Coverage map keys are integer indices of parent ACs."""
        parent_acs = ["A", "B", "C"]
        child_acs_list = [
            ["A match"],
            ["B match", "C match"],
        ]
        result = compute_coverage(parent_acs, child_acs_list, similarity_threshold=0.0)
        assert set(result["coverage_map"].keys()) == {0, 1, 2}

    def test_coverage_map_values_are_list_of_child_indices(self):
        """Coverage map values are lists of child indices that cover the parent."""
        parent_acs = ["Parent"]
        child_acs_list = [
            ["No match"],
            ["Close match"],
        ]
        result = compute_coverage(parent_acs, child_acs_list, similarity_threshold=0.0)
        for child_idx in result["coverage_map"].get(0, []):
            assert isinstance(child_idx, int)
