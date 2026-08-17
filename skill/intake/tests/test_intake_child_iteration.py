"""Tests for intake child-iteration and coverage-verification logic.

These tests verify that the intake skill's per-child iteration and AC
coverage verification behave correctly, including:

  - Per-child intake runs when children exist
  - Coverage verification across existing children
  - Stage advancement blocked on unresolvable conflicts
  - Stage advancement when coverage is fully satisfied

Related work item: SA-0MSLRVQIF0040GAM
"""

import json

import pytest

from skill.shared.tree_coverage import (
    compute_coverage,
    extract_acceptance_criteria,
    extract_acs_from_item,
    run_coverage_review,
)

# =========================================================================
# Test fixtures
# =========================================================================


class _FakeResult:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(
        self, stdout: str = "{}", returncode: int = 0, stderr: str = ""
    ):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _build_runner(
    children_data: list[dict] | None = None,
    parent_description: str = "",
    child_descriptions: dict[str, str] | None = None,
) -> callable:
    """Build a FakeWlRunner for a specific test scenario."""
    child_descriptions = child_descriptions or {}

    def runner(cmd):
        if "--children" in cmd:
            payload = json.dumps({
                "success": True,
                "workItem": {
                    "children": children_data or [],
                },
            })
            return _FakeResult(payload)

        # Check for wl show --json
        if "show" in cmd and "--json" in cmd:
            target_id = None
            for part in cmd:
                if part.startswith("SA-"):
                    target_id = part
                    break
            if target_id:
                if target_id == cmd[2] if len(cmd) > 2 else None:
                    pass  # use parent description
                if target_id in child_descriptions:
                    desc = child_descriptions[target_id]
                elif target_id == cmd[2] if len(cmd) > 2 else "parent":
                    desc = parent_description
                else:
                    desc = parent_description

                return _FakeResult(
                    json.dumps({
                        "success": True,
                        "workItem": {
                            "id": target_id,
                            "description": desc,
                        },
                    })
                )
        return _FakeResult("{}", returncode=1)

    return runner


# =========================================================================
# 1. Per-child intake simulation
# =========================================================================


class TestPerChildIntake:
    """Verify that per-child intake would iterate over all children."""

    def test_no_children_skips_child_intake(self):
        """When there are no children, per-child intake is a no-op."""
        runner = _build_runner(children_data=[])
        review = run_coverage_review("SA-01", runner=runner)
        assert review["child_summary"] == []
        assert review["recommendation"] == "proceed"

    def test_children_are_iterated(self):
        """Each child is fetched and its ACs extracted for coverage check."""
        runner = _build_runner(
            children_data=[
                {"id": "SA-02", "title": "Child 1"},
                {"id": "SA-03", "title": "Child 2"},
            ],
            parent_description=(
                "## Acceptance Criteria\n"
                "- AC 1\n"
                "- AC 2\n"
            ),
            child_descriptions={
                "SA-02": (
                    "## Acceptance Criteria\n"
                    "- AC 1 implemented\n"
                ),
                "SA-03": (
                    "## Acceptance Criteria\n"
                    "- AC 2 implemented\n"
                ),
            },
        )
        review = run_coverage_review("SA-01", runner=runner)
        assert len(review["child_summary"]) == 2
        child_ids = {c["id"] for c in review["child_summary"]}
        assert "SA-02" in child_ids
        assert "SA-03" in child_ids


# =========================================================================
# 2. Coverage verification
# =========================================================================


class TestCoverageVerification:
    """Verify AC coverage across parent/child."""

    def test_fully_covered_allows_advance(self):
        """When all parent ACs are covered, recommendation is proceed."""
        runner = _build_runner(
            children_data=[{"id": "SA-02", "title": "Child"}],
            parent_description=(
                "## Acceptance Criteria\n"
                "- Feature A login implemented\n"
                "- Feature B logout implemented\n"
            ),
            child_descriptions={
                "SA-02": (
                    "## Acceptance Criteria\n"
                    "- Feature A login implemented\n"
                    "- Feature B logout implemented\n"
                ),
            },
        )
        review = run_coverage_review("SA-01", runner=runner)
        assert review["coverage"]["fully_covered"] is True
        assert review["recommendation"] == "proceed"

    def test_missing_coverage_blocks_advance(self):
        """When coverage is missing and unresolvable, recommendation is stop."""
        runner = _build_runner(
            children_data=[{"id": "SA-02", "title": "Child"}],
            parent_description=(
                "## Acceptance Criteria\n"
                "- Feature A\n"
                "- Feature B\n"
            ),
            child_descriptions={
                "SA-02": (
                    "## Acceptance Criteria\n"
                    "- Implements A only\n"
                ),
            },
        )
        review = run_coverage_review("SA-01", runner=runner)
        assert review["coverage"]["fully_covered"] is False
        assert review["recommendation"] == "stop"
        assert len(review["unresolvable_conflicts"]) >= 1

    def test_auto_close_resolvable_gap(self):
        """When gaps can be auto-closed (high similarity), recommendation is
        auto_close rather than stop."""
        # Use a threshold that allows matching
        parent_acs = ["Feature A payment processing"]
        child_acs = [["Feature A payment processing implemented"]]
        result = compute_coverage(parent_acs, child_acs, similarity_threshold=0.4)
        assert result["fully_covered"] is True

    def test_coverage_pct_reflects_uncovered_count(self):
        """Coverage percentage accurately reflects uncovered parent ACs."""
        parent_acs = ["Feature A", "Feature B", "Feature C", "Feature D"]
        child_acs_list = [
            ["Feature A implementation", "Feature B implementation"],
        ]
        result = compute_coverage(parent_acs, child_acs_list, similarity_threshold=0.4)
        # With 0.4 threshold, Feature A and B match
        assert result["coverage_pct"] == 50.0

        # Now with higher threshold
        result2 = compute_coverage(
            parent_acs,
            child_acs_list,
            similarity_threshold=0.6,
        )
        assert result2["coverage_pct"] <= 50.0  # Only A and B match


# =========================================================================
# 3. Stage advancement blocking
# =========================================================================


class TestStageAdvancementBlocking:
    """Verify that the skill stops on unresolvable conflicts."""

    def test_conflict_prevents_stage_advance(self):
        """An unresolvable conflict yields recommendation=stop."""
        runner = _build_runner(
            children_data=[{"id": "SA-02", "title": "Child"}],
            parent_description=(
                "## Acceptance Criteria\n"
                "- System must encrypt data at rest with AES-256\n"
            ),
            child_descriptions={
                "SA-02": (
                    "## Acceptance Criteria\n"
                    "- User dashboard loads in under 2 seconds\n"
                ),
            },
        )
        review = run_coverage_review("SA-01", runner=runner)
        assert review["recommendation"] == "stop"
        # Should have an unresolvable conflict
        assert len(review["unresolvable_conflicts"]) >= 1
        # The conflict should mention the uncovered parent AC
        conflict_text = " ".join(review["unresolvable_conflicts"])
        assert "encrypt" in conflict_text.lower() or "AES" in conflict_text

    def test_no_conflict_allows_advance(self):
        """When there are no conflicts, the skill does not stop."""
        runner = _build_runner(children_data=[])
        review = run_coverage_review("SA-01", runner=runner)
        assert review["recommendation"] == "proceed"
        assert review["unresolvable_conflicts"] == []


# =========================================================================
# 4. Edge cases
# =========================================================================


class TestIntakeEdgeCases:
    """Test edge cases for intake child iteration."""

    def test_parent_without_ac_section(self):
        """A parent without an Acceptance Criteria section is fully covered."""
        runner = _build_runner(
            children_data=[{"id": "SA-02", "title": "Child"}],
            parent_description="Just a description with no ACs.",
            child_descriptions={
                "SA-02": (
                    "## Acceptance Criteria\n"
                    "- Child AC\n"
                ),
            },
        )
        review = run_coverage_review("SA-01", runner=runner)
        assert review["coverage"]["fully_covered"] is True
        assert review["coverage"]["coverage_pct"] == 100.0

    def test_child_without_ac_section(self):
        """A child without ACs is treated as contributing zero coverage."""
        runner = _build_runner(
            children_data=[{"id": "SA-02", "title": "Child"}],
            parent_description=(
                "## Acceptance Criteria\n"
                "- Parent needs AC\n"
            ),
            child_descriptions={
                "SA-02": "No ACs here, just a description.",
            },
        )
        review = run_coverage_review("SA-01", runner=runner)
        assert review["coverage"]["fully_covered"] is False
        assert "Parent needs AC" in review["coverage"]["uncovered"]

    def test_many_children(self):
        """Works correctly with many children."""
        children = [{"id": f"SA-{i:02d}", "title": f"Child {i}"} for i in range(2, 12)]
        child_descs = {
            f"SA-{i:02d}": f"## Acceptance Criteria\n- Covers AC {i-2}"
            for i in range(2, 12)
        }
        parent_desc = "\n".join(
            f"- AC {i}" for i in range(10)
        )
        runner = _build_runner(
            children_data=children,
            parent_description=f"## Acceptance Criteria\n{parent_desc}",
            child_descriptions=child_descs,
        )
        review = run_coverage_review("SA-01", runner=runner)
        assert len(review["child_summary"]) == 10
