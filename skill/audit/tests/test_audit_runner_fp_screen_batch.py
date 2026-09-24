"""Tests: batched false-positive screen (SA-0MSYPAV1R000SMHK).

Verifies the batched-screen fix for large ruff finding sets that would
previously crash with ``E2BIG`` (Argument list too long).

Coverage per ACs:
  1. No crash on large finding sets (2,000 findings).
  2. Chunked/bounded batching — findings are split across multiple Pi calls.
  3. No untracked findings — every finding is classified in the result.
  4. Configurable batch size via AUDIT_FP_SCREEN_BATCH_SIZE.
  5. Small finding sets still work (single batch, no regression).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(severity: str, code: str = "X", linter: str = "ruff",
             file: str = "src/bad.py", line: int = 1) -> dict:
    """Minimal code-quality finding dict (matches linter_runner schema)."""
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "message": f"{code} message",
        "linter": linter,
        "code": code,
    }


def _ok_result(text: str) -> dict:
    """A healthy _call_pi result carrying *text* as extracted output."""
    return {"extracted_text": text}


def _make_findings(count: int, severity: str = "high",
                   base_code: str = "F") -> list[dict]:
    """Create *count* ruff findings with sequential codes."""
    return [
        _finding(severity, code=f"{base_code}{i:04d}",
                 file=f"src/file_{i}.py", line=i + 1)
        for i in range(count)
    ]


def _make_batch_response(batch_findings: list[dict]) -> dict:
    """Build a model response classifying the findings in *batch_findings*."""
    return _ok_result(json.dumps([
        {
            "index": i,
            "classification": "genuine",
            "justification": f"finding {i}",
        }
        for i in range(len(batch_findings))
    ]))


def _make_mixed_response(batch_findings: list[dict]) -> dict:
    """Build a response with mixed classifications."""
    return _ok_result(json.dumps([
        {
            "index": i,
            "classification": [
                "genuine", "confident-false-positive", "uncertain"
            ][i % 3],
            "justification": f"finding {i}",
        }
        for i in range(len(batch_findings))
    ]))


def _extract_batch_size_from_prompt(prompt: str) -> int:
    """Extract the number of findings from a prompt string."""
    import re
    m = re.search(r'Findings: (\[.*?\])', prompt, re.DOTALL)
    if m:
        batch = json.loads(m.group(1))
        return len(batch)
    return 0


# ===========================================================================
# AC1 — no crash on large finding sets (2,000 findings)
# ===========================================================================

class TestNoCrashLargeSets:
    """AC1: the screen completes (no E2BIG) with 2,000+ findings."""

    def test_two_thousand_findings_no_crash(self):
        """2,000 findings must not crash — all must be classified."""
        findings = _make_findings(2000)
        ac_fallback_used = mock.Mock()
        call_count = [0]
        prompt_sizes = []

        def _fake_pi(*args, **kwargs):
            call_count[0] += 1
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            prompt_sizes.append(len(prompt))
            # Verify the prompt is not absurdly large (should fit in ~50KB per batch)
            assert len(prompt) < 100_000, f"Prompt too large: {len(prompt)} bytes"
            batch_size = _extract_batch_size_from_prompt(prompt)
            return _make_batch_response(list(range(batch_size)))

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi,
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert len(entries) == 2000
        assert call_count[0] > 1  # Must be chunked
        # All prompts should be under 100KB
        for sz in prompt_sizes:
            assert sz < 100_000
        ac_fallback_used.set.assert_not_called()

    def test_eight_hundred_findings_chunks(self):
        """800 findings with batch_size=200 → 4 batches."""
        findings = _make_findings(800)
        ac_fallback_used = mock.Mock()
        call_count = [0]

        def _fake_pi(*args, **kwargs):
            call_count[0] += 1
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            batch_size = _extract_batch_size_from_prompt(prompt)
            return _make_batch_response(list(range(batch_size)))

        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi,
            ),
            mock.patch.dict(os.environ, {"AUDIT_FP_SCREEN_BATCH_SIZE": "200"}),
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert len(entries) == 800
        assert call_count[0] == 4
        assert ac_fallback_used.set.call_count == 0


# ===========================================================================
# AC2 — chunked/bounded batching
# ===========================================================================

class TestBoundedBatching:
    """AC2: findings are sent in bounded batches, not one unbounded call."""

    def test_default_batch_size_is_500(self):
        assert audit_runner.FP_SCREEN_BATCH_SIZE_DEFAULT == 500

    def test_batch_size_resolved_from_env(self):
        with mock.patch.dict(os.environ, {"AUDIT_FP_SCREEN_BATCH_SIZE": "100"}):
            assert audit_runner._resolve_fp_screen_batch_size() == 100

    def test_invalid_env_values_use_default(self):
        for bad_val in ["abc", "0", "-1", ""]:
            with mock.patch.dict(os.environ, {"AUDIT_FP_SCREEN_BATCH_SIZE": bad_val}):
                assert audit_runner._resolve_fp_screen_batch_size() == 500

    def test_batches_are_chunked_correctly(self):
        """1_200 findings → 3 batches (500+500+200) with default batch size."""
        findings = _make_findings(1200)
        ac_fallback_used = mock.Mock()
        batch_sizes = []

        def _capture_batches(*args, **kwargs):
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            batch_size = _extract_batch_size_from_prompt(prompt)
            batch_sizes.append(batch_size)
            return _make_batch_response(list(range(batch_size)))

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture_batches,
        ):
            audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert batch_sizes == [500, 500, 200]

    def test_single_batch_for_small_sets(self):
        """100 findings fit in one batch — only one Pi call."""
        findings = _make_findings(100)
        ac_fallback_used = mock.Mock()
        call_count = [0]

        def _fake_pi(*args, **kwargs):
            call_count[0] += 1
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            batch_size = _extract_batch_size_from_prompt(prompt)
            return _make_batch_response(list(range(batch_size)))

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi,
        ):
            audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert call_count[0] == 1


# ===========================================================================
# AC3 — no untracked findings
# ===========================================================================

class TestNoUntrackedFindings:
    """AC3: a screen failure never leaves findings completely untracked."""

    def test_partial_batch_failure_still_classifies_successful_batches(self):
        """If batch 2 fails, batch 1 and 3 results are preserved."""
        findings = _make_findings(750)
        ac_fallback_used = mock.Mock()
        call_count = [0]

        def _partial_fail(*args, **kwargs):
            call_count[0] += 1
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            batch_size = _extract_batch_size_from_prompt(prompt)
            if call_count[0] == 2:
                # Second batch fails
                raise RuntimeError("simulated batch 2 failure")
            return _make_batch_response(list(range(batch_size)))

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_partial_fail,
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert len(entries) == 750
        # Failed batch findings are uncertain
        # Batches 1 and 3 are genuine, batch 2 is uncertain
        uncertain_count = sum(1 for e in entries if e["classification"] == "uncertain")
        genuine_count = sum(1 for e in entries if e["classification"] == "genuine")
        assert uncertain_count > 0  # At least the failed batch
        assert genuine_count > 0    # At least the successful batches
        ac_fallback_used.set.assert_called()

    def test_all_batches_fail_all_uncertain(self):
        """If ALL batches fail, every finding is uncertain."""
        findings = _make_findings(300)
        ac_fallback_used = mock.Mock()

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=RuntimeError("pi broken"),
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert len(entries) == 300
        assert all(e["classification"] == "uncertain" for e in entries)
        assert all(e["screen_failed"] for e in entries)
        ac_fallback_used.set.assert_called()

    def test_infra_degraded_batch_does_not_leak_cfps(self):
        """A degraded batch does NOT produce confident-false-positive."""
        findings = _make_findings(700)
        ac_fallback_used = mock.Mock()
        call_count = [0]

        def _degraded(*args, **kwargs):
            call_count[0] += 1
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            batch_size = _extract_batch_size_from_prompt(prompt)
            if call_count[0] == 2:
                return {"extracted_text": "", "_timeout": True}
            return _make_batch_response(list(range(batch_size)))

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_degraded,
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert len(entries) == 700
        # The degraded batch (indices 500-699) must be uncertain, not CFP
        assert all(e["classification"] != "confident-false-positive" for e in entries)


# ===========================================================================
# AC4 — regression test for large finding sets
# ===========================================================================

class TestRegressionLargeSets:
    """AC4: explicit regression test for the 2,000-finding path."""

    def test_two_thousand_findings_all_classified(self):
        """Simulated 2,000-finding scan: all findings classified, no crash."""
        findings = _make_findings(2000)
        ac_fallback_used = mock.Mock()
        total_calls = [0]

        def _simulate_large_scan(*args, **kwargs):
            total_calls[0] += 1
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            batch_size = _extract_batch_size_from_prompt(prompt)
            return _make_mixed_response(list(range(batch_size)))

        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                side_effect=_simulate_large_scan,
            ),
            mock.patch.dict(os.environ, {"AUDIT_FP_SCREEN_BATCH_SIZE": "333"}),
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert len(entries) == 2000
        # Check all three classifications are present
        classifications = {e["classification"] for e in entries}
        assert classifications == {"genuine", "confident-false-positive", "uncertain"}
        # Total calls = ceil(2000 / 333) = 7 (6×333=1998 + 2 remaining)
        assert total_calls[0] == 7
        ac_fallback_used.set.assert_not_called()

    def test_large_set_preserves_finding_metadata(self):
        """Each entry carries the correct original finding."""
        findings = _make_findings(1500, severity="critical")
        ac_fallback_used = mock.Mock()

        def _fake_pi(*args, **kwargs):
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            batch_size = _extract_batch_size_from_prompt(prompt)
            return _make_batch_response(list(range(batch_size)))

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi,
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert len(entries) == 1500
        # Every entry's finding must match the original
        for i, entry in enumerate(entries):
            assert entry["index"] == i
            assert entry["finding"]["severity"] == "critical"
            assert entry["finding"]["file"] == f"src/file_{i}.py"
            assert entry["finding"]["linter"] == "ruff"


# ===========================================================================
# Backward compatibility — small sets still work
# ===========================================================================

class TestBackwardCompatibility:
    """Ensure existing small-set behaviour is preserved."""

    def test_empty_findings_zero_calls(self):
        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log") as call:
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", [], pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=mock.Mock(),
            )
        assert entries == []
        call.assert_not_called()

    def test_non_ruff_findings_zero_calls(self):
        findings = [_finding("high", code="no-unused", linter="eslint")]
        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log") as call:
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=mock.Mock(),
            )
        assert entries == []
        call.assert_not_called()

    def test_small_set_single_batch(self):
        """5 ruff findings → single batch, single call."""
        findings = [_finding("high", code=f"F{i:02d}") for i in range(5)]
        ac_fallback_used = mock.Mock()
        call_count = [0]

        def _fake_pi(*args, **kwargs):
            call_count[0] += 1
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            batch_size = _extract_batch_size_from_prompt(prompt)
            return _make_batch_response(list(range(batch_size)))

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi,
        ):
            entries = audit_runner._screen_ruff_findings(
                "TEST-1", findings, pi_bin="pi", resolved_model="m",
                debug_log=None, timeout=None,
                ac_fallback_used=ac_fallback_used,
            )

        assert call_count[0] == 1
        assert len(entries) == 5
        for i, entry in enumerate(entries):
            assert entry["index"] == i
            assert entry["finding"]["code"] == f"F{i:02d}"
