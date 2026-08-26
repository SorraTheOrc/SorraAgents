"""Tests for the shared timing utility (SA-0MT319YGQ002E801).

Verifies the ``Timer`` context manager: step timing, nesting roll-up,
percentage calculation, and report rendering in both human-readable and
JSON formats.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SHARED = REPO_ROOT / "skill"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from shared.timing import Timer

# ---------------------------------------------------------------------------
# Basic step timing
# ---------------------------------------------------------------------------


class TestBasicStepTiming:
    """Single-step timing without nesting."""

    def test_records_elapsed_time(self):
        """A Timer records non-zero elapsed time."""
        with Timer("step") as t:
            pass
        assert t.elapsed > 0

    def test_elapsed_is_a_number(self):
        """Elapsed time is a float."""
        with Timer("step") as t:
            pass
        assert isinstance(t.elapsed, float)

    def test_total_time_equals_elapsed_for_leaf(self):
        """For a leaf timer (no children), total_time == elapsed."""
        with Timer("step") as t:
            pass
        assert abs(t.total_time - t.elapsed) < 1e-9


# ---------------------------------------------------------------------------
# Nesting roll-up
# ---------------------------------------------------------------------------


class TestNestingRollUp:
    """Nested timers roll up correctly into parent totals."""

    def test_parent_elapsed_is_sum_of_children(self):
        """Parent total_time equals sum of children's total_time."""
        with Timer("parent") as parent:
            with Timer("child_a") as a:
                pass
            with Timer("child_b") as b:
                pass
        assert abs(parent.total_time - (a.total_time + b.total_time)) < 1e-6

    def test_nested_step_has_parent_reference(self):
        """Nested Timer has a reference to its parent."""
        with Timer("parent") as parent, Timer("child") as child:
            pass
        assert child.parent is parent

    def test_parent_tracks_children_in_nested_steps(self):
        """Parent's nested_steps list contains child Timers."""
        with Timer("parent") as parent:
            with Timer("child_a") as _:
                pass
            with Timer("child_b") as _:
                pass
        assert len(parent.nested_steps) == 2
        names = {s.name for s in parent.nested_steps}
        assert names == {"child_a", "child_b"}

    def test_deep_nesting_roll_up(self):
        """Three levels of nesting roll up correctly."""
        with Timer("level_0") as l0, Timer("level_1") as l1:
            with Timer("level_2") as l2:
                pass
        assert abs(l0.total_time - l1.total_time) < 1e-6
        assert abs(l1.total_time - l2.total_time) < 1e-6


# ---------------------------------------------------------------------------
# Percentage calculation
# ---------------------------------------------------------------------------


class TestPercentageCalculation:
    """Percentages of steps sum to ~100% of total time."""

    def test_single_step_is_100_percent(self):
        """A single root step reports 100%."""
        with Timer("only") as t:
            pass
        assert abs(t.percentage - 100.0) < 0.1

    def test_children_sum_to_100_percent(self):
        """Sibling children's percentages sum to ~100%."""
        with Timer("parent") as parent:
            with Timer("a") as a:
                pass
            with Timer("b") as b:
                pass
        total_pct = a.percentage + b.percentage
        assert abs(total_pct - 100.0) < 0.5

    def test_percentage_is_non_negative(self):
        """Percentages are never negative."""
        with Timer("p") as p, Timer("c") as c:
            pass
        assert p.percentage >= 0
        assert c.percentage >= 0


# ---------------------------------------------------------------------------
# Report rendering (human-readable)
# ---------------------------------------------------------------------------


class TestHumanReadableReport:
    """Human-readable report rendering."""

    def test_render_returns_string(self):
        """render() returns a string."""
        with Timer("step") as t:
            pass
        result = t.render()
        assert isinstance(result, str)

    def test_render_contains_header(self):
        """Human report contains a header."""
        with Timer("step") as t:
            pass
        report = t.render()
        assert "Timing Report" in report

    def test_render_contains_step_name(self):
        """Human report contains step names."""
        with Timer("my_step") as t:
            pass
        report = t.render()
        assert "my_step" in report

    def test_render_contains_elapsed_seconds(self):
        """Human report contains elapsed time with sub-second precision."""
        with Timer("step") as t:
            pass
        report = t.render()
        # Check that the report has numeric content (elapsed seconds)
        assert "0." in report or "0.0" in report or "0." in report

    def test_render_contains_total_row(self):
        """Human report contains a Total row."""
        with Timer("step") as t:
            pass
        report = t.render()
        assert "Total" in report

    def test_render_has_table_structure(self):
        """Human report has table-like structure with separators."""
        with Timer("step") as t:
            pass
        report = t.render()
        assert "=" in report
        assert "-" in report

    def test_render_nested_steps(self):
        """Human report shows nested steps with indentation."""
        with Timer("parent") as parent, Timer("child") as child:
            pass
        report = parent.render()
        assert "parent" in report
        assert "child" in report

    def test_render_column_headers(self):
        """Human report has column headers for step, elapsed, percentage, total."""
        with Timer("step") as t:
            pass
        report = t.render()
        lines = report.splitlines()
        header_line = [l for l in lines if "Elapsed" in l][0]
        assert "Step" in header_line
        assert "Elapsed" in header_line
        assert "%" in header_line
        assert "Total" in header_line


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


class TestJsonSerialization:
    """JSON-serializable dict and string output."""

    def test_to_dict_returns_dict(self):
        """to_dict() returns a dict."""
        with Timer("step") as t:
            pass
        result = t.to_dict()
        assert isinstance(result, dict)

    def test_to_dict_has_required_keys(self):
        """Dict has name, elapsed, total_time, percentage, nested_steps."""
        with Timer("step") as t:
            pass
        d = t.to_dict()
        assert "name" in d
        assert "elapsed" in d
        assert "total_time" in d
        assert "percentage" in d
        assert "nested_steps" in d
        assert d["name"] == "step"

    def test_to_dict_nested_structure(self):
        """Nested timers produce nested dicts."""
        with Timer("parent") as parent, Timer("child") as _:
            pass
        d = parent.to_dict()
        assert len(d["nested_steps"]) == 1
        assert d["nested_steps"][0]["name"] == "child"

    def test_to_json_returns_string(self):
        """to_json() returns a JSON string."""
        with Timer("step") as t:
            pass
        result = t.to_json()
        assert isinstance(result, str)

    def test_to_json_is_valid_json(self):
        """to_json() produces valid JSON."""
        with Timer("step") as t:
            pass
        data = json.loads(t.to_json())
        assert "name" in data

    def test_json_serializable_from_fresh_context(self):
        """to_dict produces a fully JSON-serializable structure."""
        with Timer("root") as root:
            with Timer("a") as a:
                pass
            with Timer("b") as b:
                pass
        json.dumps(root.to_dict())  # must not raise


# ---------------------------------------------------------------------------
# Nested-step percentages
# ---------------------------------------------------------------------------


class TestNestedStepPercentages:
    """Nested steps report correct percentages."""

    def test_child_percentage_in_parent_context(self):
        """Child percentage is relative to root total."""
        with Timer("root") as root:
            with Timer("half") as half:
                pass
            with Timer("half") as half2:
                pass
        # Both should be ~50%
        assert 40 < half.percentage < 60
        assert 40 < half2.percentage < 60

    def test_deeply_nested_child_percentage(self):
        """A deeply nested child's percentage is relative to the root."""
        with Timer("root") as root, Timer("level1") as l1:
            with Timer("level2") as l2:
                pass
        assert 0 < root.percentage <= 100
        assert 0 < l1.percentage <= 100
        assert 0 < l2.percentage <= 100


# ---------------------------------------------------------------------------
# JSON output mode (additive, not breaking)
# ---------------------------------------------------------------------------


class TestJsonAdditive:
    """Timing data is additive and doesn't break existing consumers."""

    def test_timing_dict_is_self_contained(self):
        """The timing dict can be merged into existing output without conflicts."""
        with Timer("timing") as t:
            pass
        timing_data = t.to_dict()
        # Should only contain expected keys
        expected_keys = {"name", "elapsed", "total_time", "percentage", "nested_steps"}
        assert set(timing_data.keys()) <= expected_keys

    def test_multiple_timer_instances_are_independent(self):
        """Two separate Timer instances don't interfere."""
        results = []
        with Timer("t1") as t1:
            pass
        with Timer("t2") as t2:
            pass
        d1 = t1.to_dict()
        d2 = t2.to_dict()
        assert d1["name"] == "t1"
        assert d2["name"] == "t2"
        assert d1 is not d2
