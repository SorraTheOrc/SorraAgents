"""Tests for budget-baseline derivation (SA-0MU32T6O0001UALR).

Verifies:
- AC1: ``_parse_per_call_timing_lines`` correctly extracts context + elapsed
  from ``Per-call timing:`` debug-log lines.
- AC2: ``_derive_budget_baseline`` computes p50/p95 per phase and applies
  the safety margin correctly.
- AC3: The budget model is documented and the helper is importable.
"""
from __future__ import annotations

from audit.scripts import audit_runner


class TestParsePerCallTimingLines:
    """Tests for _parse_per_call_timing_lines (AC1)."""

    def test_parses_single_line(self):
        """AC1: A single timing line yields one entry for its context."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=12.34",
        ]
        result = audit_runner._parse_per_call_timing_lines(lines)
        assert result == {"phase1_parent": [12.34]}

    def test_parses_multiple_lines_same_context(self):
        """AC1: Multiple lines for the same context accumulate."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=12.34",
            "Per-call timing: issue_id=SA-2 context=phase1_parent elapsed_seconds=15.21",
        ]
        result = audit_runner._parse_per_call_timing_lines(lines)
        assert result["phase1_parent"] == [12.34, 15.21]

    def test_parses_multiple_contexts(self):
        """AC1: Lines for different contexts are separated."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=12.34",
            "Per-call timing: issue_id=SA-2 context=phase2_deep elapsed_seconds=45.67",
        ]
        result = audit_runner._parse_per_call_timing_lines(lines)
        assert result["phase1_parent"] == [12.34]
        assert result["phase2_deep"] == [45.67]

    def test_ignores_non_timing_lines(self):
        """AC1: Non-matching lines are silently ignored."""
        lines = [
            "Some other log line",
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=12.34",
            "Another log line",
        ]
        result = audit_runner._parse_per_call_timing_lines(lines)
        assert result == {"phase1_parent": [12.34]}

    def test_empty_list(self):
        """AC1: An empty list returns an empty dict."""
        result = audit_runner._parse_per_call_timing_lines([])
        assert result == {}

    def test_parses_line_with_extra_fields(self):
        """AC1: Extra fields (input_tokens, ac_count, model) don't break parsing."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase2_deep elapsed_seconds=30.5 input_tokens=5000 ac_count=3 model=gpt-4",
        ]
        result = audit_runner._parse_per_call_timing_lines(lines)
        assert result["phase2_deep"] == [30.5]

    def test_all_standard_contexts(self):
        """AC1: Standard phase contexts are parsed correctly."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=10.0",
            "Per-call timing: issue_id=SA-2 context=phase1_children elapsed_seconds=20.0",
            "Per-call timing: issue_id=SA-3 context=phase2_deep elapsed_seconds=30.0",
        ]
        result = audit_runner._parse_per_call_timing_lines(lines)
        assert "phase1_parent" in result
        assert "phase1_children" in result
        assert "phase2_deep" in result


class TestDeriveBudgetBaseline:
    """Tests for _derive_budget_baseline (AC2)."""

    def test_empty_lines_returns_empty(self):
        """AC2: Empty timing lines produce an empty baseline."""
        result = audit_runner._derive_budget_baseline([])
        assert result == {}

    def test_single_sample_phase(self):
        """AC2: A single sample yields p95 == that value and budget = p95 * margin."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=12.0",
        ]
        result = audit_runner._derive_budget_baseline(lines, safety_margin=1.5)
        assert len(result) == 1
        assert result["phase1_parent"]["p95"] == 12.0
        assert result["phase1_parent"]["budget"] == 18.0  # 12.0 * 1.5
        assert result["phase1_parent"]["p50"] == 12.0

    def test_two_samples_phase(self):
        """AC2: Two samples compute meaningful p50/p95."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=10.0",
            "Per-call timing: issue_id=SA-2 context=phase1_parent elapsed_seconds=20.0",
        ]
        result = audit_runner._derive_budget_baseline(lines, safety_margin=1.5)
        assert result["phase1_parent"]["p50"] == 15.0  # median of [10, 20]
        # p95 index = int(2 * 0.95) - 1 = 0 → sorted_vals[1] = 20.0
        assert result["phase1_parent"]["p95"] == 20.0
        assert result["phase1_parent"]["budget"] == 30.0

    def test_many_samples_phase(self):
        """AC2: Many samples compute p50/p95 correctly."""
        # 20 samples: 1..20
        lines = [
            f"Per-call timing: issue_id=SA-{i} context=phase2_deep elapsed_seconds={float(i)}"
            for i in range(1, 21)
        ]
        result = audit_runner._derive_budget_baseline(lines, safety_margin=1.5)
        assert result["phase2_deep"]["count"] == 20
        assert result["phase2_deep"]["p50"] == 10.5  # median of 1..20
        # p95: sorted_vals[int(20 * 0.95) - 1] = sorted_vals[18] = 19.0
        assert result["phase2_deep"]["p95"] == 19.0
        assert result["phase2_deep"]["budget"] == 28.5  # 19.0 * 1.5
        assert result["phase2_deep"]["mean"] == 10.5
        assert result["phase2_deep"]["min"] == 1.0
        assert result["phase2_deep"]["max"] == 20.0

    def test_custom_safety_margin(self):
        """AC2: Custom safety margin is applied correctly."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=10.0",
        ]
        result = audit_runner._derive_budget_baseline(lines, safety_margin=2.0)
        assert result["phase1_parent"]["budget"] == 20.0  # 10.0 * 2.0

    def test_default_safety_margin(self):
        """AC2: Default safety margin is 1.5."""
        assert audit_runner.BUDGET_SAFE_MARGIN_DEFAULT == 1.5

    def test_multiple_phases(self):
        """AC2: Multiple phases are computed independently."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=10.0",
            "Per-call timing: issue_id=SA-2 context=phase2_deep elapsed_seconds=20.0",
            "Per-call timing: issue_id=SA-3 context=phase1_parent elapsed_seconds=30.0",
        ]
        result = audit_runner._derive_budget_baseline(lines, safety_margin=1.5)
        assert "phase1_parent" in result
        assert "phase2_deep" in result
        assert result["phase1_parent"]["count"] == 2
        assert result["phase2_deep"]["count"] == 1

    def test_rounds_values(self):
        """AC2: Float values are rounded to 2 decimal places."""
        lines = [
            "Per-call timing: issue_id=SA-1 context=phase1_parent elapsed_seconds=10.123456",
            "Per-call timing: issue_id=SA-2 context=phase1_parent elapsed_seconds=20.987654",
        ]
        result = audit_runner._derive_budget_baseline(lines, safety_margin=1.5)
        assert result["phase1_parent"]["p50"] == 15.56  # rounded
        assert result["phase1_parent"]["p95"] == 20.99  # rounded
        assert result["phase1_parent"]["budget"] == 31.48  # 20.99 * 1.5 rounded


class TestBudgetConstants:
    """Tests for budget-related constants (AC3)."""

    def test_safety_margin_constant_exists(self):
        """AC3: BUDGET_SAFE_MARGIN_DEFAULT is defined."""
        assert hasattr(audit_runner, "BUDGET_SAFE_MARGIN_DEFAULT")
        assert audit_runner.BUDGET_SAFE_MARGIN_DEFAULT == 1.5

    def test_timing_regex_constant_exists(self):
        """AC3: _PER_CALL_TIMING_RE is a compiled regex."""
        assert hasattr(audit_runner, "_PER_CALL_TIMING_RE")
        assert hasattr(audit_runner._PER_CALL_TIMING_RE, "search")

    def test_functions_are_importable(self):
        """AC3: New functions are importable from audit_runner."""
        assert callable(audit_runner._parse_per_call_timing_lines)
        assert callable(audit_runner._derive_budget_baseline)
