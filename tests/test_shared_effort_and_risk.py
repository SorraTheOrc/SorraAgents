#!/usr/bin/env python3
"""
Tests for the shared effort-and-risk module (_shared.py).

Validates that compute_omp, pick_tshirt, TSHIRT_MAP, and DEFAULT_THRESHOLDS
are correctly defined and behave identically to the original duplicated copies.
"""

import sys
import os
import unittest

# Add the shared module directory to path
_SCRIPT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "skill", "effort-and-risk", "scripts"
)
sys.path.insert(0, _SCRIPT_DIR)

from _shared import compute_omp, level_from_score, pick_tshirt, TSHIRT_MAP, DEFAULT_THRESHOLDS


class TestComputeOmp(unittest.TestCase):
    """Tests for compute_omp(data: dict) -> tuple[float, float, float]."""

    def test_individual_omp(self):
        """Returns individual o, m, p when no items list is provided."""
        data = {"o": 3.0, "m": 5.0, "p": 10.0}
        self.assertEqual(compute_omp(data), (3.0, 5.0, 10.0))

    def test_individual_omp_missing_fields_default_to_zero(self):
        """Defaults o, m, p to 0 when keys are missing."""
        data = {}
        self.assertEqual(compute_omp(data), (0.0, 0.0, 0.0))

    def test_individual_omp_partial_fields(self):
        """Uses provided fields and defaults missing to 0."""
        data = {"o": 2.0, "p": 8.0}
        self.assertEqual(compute_omp(data), (2.0, 0.0, 8.0))

    def test_items_aggregation(self):
        """Aggregates o, m, p across a list of items."""
        data = {
            "items": [
                {"id": "1", "title": "Task A", "o": 2, "m": 4, "p": 6},
                {"id": "2", "title": "Task B", "o": 3, "m": 5, "p": 8},
            ]
        }
        self.assertEqual(compute_omp(data), (5.0, 9.0, 14.0))

    def test_items_empty_list_falls_back_to_individual(self):
        """Empty items list falls back to individual o, m, p."""
        data = {"items": [], "o": 1.0, "m": 2.0, "p": 3.0}
        self.assertEqual(compute_omp(data), (1.0, 2.0, 3.0))

    def test_items_single_item(self):
        """Single item in items list returns its o, m, p."""
        data = {
            "items": [
                {"id": "1", "o": 5, "m": 7, "p": 9},
            ]
        }
        self.assertEqual(compute_omp(data), (5.0, 7.0, 9.0))

    def test_items_float_string_values(self):
        """Handles string float values in items."""
        data = {
            "items": [
                {"o": "1.5", "m": "2.5", "p": "3.5"},
            ]
        }
        self.assertEqual(compute_omp(data), (1.5, 2.5, 3.5))


class TestPickTshirt(unittest.TestCase):
    """Tests for pick_tshirt(hours: float, thresholds: dict | None = None) -> str."""

    def test_default_thresholds_xs(self):
        """Hours below 4 return 'XS' with default thresholds."""
        result = pick_tshirt(3.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "XS")

    def test_default_thresholds_s(self):
        """Hours between 4 and 24 return 'S'."""
        result = pick_tshirt(10.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "S")

    def test_default_thresholds_m(self):
        """Hours between 24 and 80 return 'M'."""
        result = pick_tshirt(50.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "M")

    def test_default_thresholds_l(self):
        """Hours between 80 and 240 return 'L'."""
        result = pick_tshirt(120.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "L")

    def test_default_thresholds_xl(self):
        """Hours >= 240 return 'XL'."""
        result = pick_tshirt(500.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "XL")

    def test_boundary_min_exact(self):
        """Exact min value (e.g., 4) returns the size for that bucket ('S')."""
        result = pick_tshirt(4.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "S")

    def test_boundary_max_exclusive(self):
        """Exact max value (e.g., 24) falls to the next bucket ('M')."""
        result = pick_tshirt(24.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "M")

    def test_zero_hours(self):
        """Zero hours returns 'XS'."""
        result = pick_tshirt(0.0, DEFAULT_THRESHOLDS)
        self.assertEqual(result, "XS")

    def test_custom_thresholds(self):
        """Uses custom thresholds when provided."""
        custom = {
            "Small": {"min": 0, "max": 10},
            "Large": {"min": 10, "max": None},
        }
        self.assertEqual(pick_tshirt(5.0, custom), "Small")
        self.assertEqual(pick_tshirt(10.0, custom), "Large")
        self.assertEqual(pick_tshirt(100.0, custom), "Large")

    def test_no_thresholds_falls_back_to_default(self):
        """When thresholds is None, uses DEFAULT_THRESHOLDS."""
        result = pick_tshirt(3.0, None)
        self.assertEqual(result, "XS")
        result = pick_tshirt(50.0, None)
        self.assertEqual(result, "M")


class TestTshirtMap(unittest.TestCase):
    """Tests for TSHIRT_MAP constant."""

    def test_contains_all_sizes(self):
        """TSHIRT_MAP contains all five standard sizes."""
        expected = {"XS", "S", "M", "L", "XL"}
        self.assertEqual(set(TSHIRT_MAP.keys()), expected)

    def test_expected_mappings(self):
        """TSHIRT_MAP maps codes to expected full-text labels."""
        self.assertEqual(TSHIRT_MAP["XS"], "Extra Small")
        self.assertEqual(TSHIRT_MAP["S"], "Small")
        self.assertEqual(TSHIRT_MAP["M"], "Medium")
        self.assertEqual(TSHIRT_MAP["L"], "Large")
        self.assertEqual(TSHIRT_MAP["XL"], "Extra Large")


class TestDefaultThresholds(unittest.TestCase):
    """Tests for DEFAULT_THRESHOLDS constant."""

    def test_contains_all_sizes(self):
        """DEFAULT_THRESHOLDS contains all five standard sizes."""
        expected = {"XS", "S", "M", "L", "XL"}
        self.assertEqual(set(DEFAULT_THRESHOLDS.keys()), expected)

    def test_boundaries_are_ordered(self):
        """Each size boundary is consistent with the next."""
        sizes = ["XS", "S", "M", "L", "XL"]
        for i in range(len(sizes) - 1):
            curr = DEFAULT_THRESHOLDS[sizes[i]]
            nxt = DEFAULT_THRESHOLDS[sizes[i + 1]]
            self.assertEqual(
                curr["max"],
                nxt["min"],
                f"{sizes[i]}.max ({curr['max']}) should equal {sizes[i+1]}.min ({nxt['min']})",
            )

    def test_xl_has_no_upper_bound(self):
        """XL threshold has max set to None."""
        self.assertIsNone(DEFAULT_THRESHOLDS["XL"]["max"])

    def test_xs_starts_at_zero(self):
        """XS threshold min is 0."""
        self.assertEqual(DEFAULT_THRESHOLDS["XS"]["min"], 0)


class TestLevelFromScore(unittest.TestCase):
    """Tests for level_from_score(score: int | float) -> str."""

    def test_low_boundary_0(self):
        """Score of 0 returns 'Low'."""
        self.assertEqual(level_from_score(0), "Low")

    def test_low_boundary_1(self):
        """Score of 1 returns 'Low'."""
        self.assertEqual(level_from_score(1), "Low")

    def test_low_boundary_5(self):
        """Score of 5 returns 'Low' (upper bound of Low)."""
        self.assertEqual(level_from_score(5), "Low")

    def test_medium_boundary_6(self):
        """Score of 6 returns 'Medium' (lower bound of Medium)."""
        self.assertEqual(level_from_score(6), "Medium")

    def test_medium_boundary_12(self):
        """Score of 12 returns 'Medium' (upper bound of Medium)."""
        self.assertEqual(level_from_score(12), "Medium")

    def test_high_boundary_13(self):
        """Score of 13 returns 'High' (lower bound of High)."""
        self.assertEqual(level_from_score(13), "High")

    def test_high_boundary_19(self):
        """Score of 19 returns 'High' (upper bound of High)."""
        self.assertEqual(level_from_score(19), "High")

    def test_critical_boundary_20(self):
        """Score of 20 returns 'Critical' (lower bound of Critical)."""
        self.assertEqual(level_from_score(20), "Critical")

    def test_critical_high_score(self):
        """Score of 25 returns 'Critical'."""
        self.assertEqual(level_from_score(25), "Critical")

    def test_float_low(self):
        """Float score 5.5 returns 'Medium'."""
        self.assertEqual(level_from_score(5.5), "Medium")

    def test_float_medium_upper(self):
        """Float score 12.0 returns 'Medium'."""
        self.assertEqual(level_from_score(12.0), "Medium")

    def test_float_high_upper(self):
        """Float score 19.9 returns 'Critical' because 19.9 > 19 (same as original behavior)."""
        self.assertEqual(level_from_score(19.9), "Critical")

    def test_negative_score(self):
        """Negative score (e.g., -1) returns 'Low' (same behavior as score <= 5)."""
        self.assertEqual(level_from_score(-1), "Low")


if __name__ == "__main__":

    unittest.main()
