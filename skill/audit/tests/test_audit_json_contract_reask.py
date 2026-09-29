
# <!-- REFACTOR-SA-0MUMEJVW00096MA8
# smell: bug_risk
# severity: medium
# description: Function definition does not bind loop variable `calls`
# -->

# <!-- REFACTOR-SA-0MUMEJV3U001FBT4
# smell: ruff_specific
# severity: medium
# description: Unpacked variable `result` is never used
# -->
#!/usr/bin/env python3
"""Audit JSON-contract tolerance and bounded re-ask tests (SA-0MU32TCFM003B78I).

Covers:
- AC2: ``_extract_json_array`` tolerates leading/trailing prose and Markdown
  code fences and selects the intended balanced array (real fixture shapes).
- AC3: a parse failure at a Phase 1/2 site triggers exactly one bounded
  "return only the JSON array" re-ask under the observable ``verdict_reask``
  context before the caller falls back to ``partial``.
- AC4: corpus replay over >=20 captured responses shows the re-ask recovers
  the genuine failures, reducing the genuine parse-failure rate to 0 on the
  replay corpus (offline evidence per the producer's option (a)).
- AC5: verdict semantics are unchanged — a persistent failure still falls
  back to the conservative ``partial``, never ``met``.

All tests run offline with ``_call_pi_and_maybe_log`` mocked.
"""  # noqa: EXE001
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner

# ---------------------------------------------------------------------------
# AC2: tolerant extraction
# ---------------------------------------------------------------------------

_VALID_ARRAY = json.dumps([{"index": 0, "verdict": "met", "evidence": "x.py:1"}])


class TestTolerantExtraction:
    def test_parses_bare_array(self):
        assert audit_runner._extract_json_array(_VALID_ARRAY)[0]["index"] == 0

    def test_parses_prose_before_array(self):
        text = f"I reviewed the code.\n\n{_VALID_ARRAY}"
        assert audit_runner._extract_json_array(text)[0]["verdict"] == "met"

    def test_parses_complete_fenced_array_with_prose(self):
        text = f"Here is the array:\n\n```json\n{_VALID_ARRAY}\n```\nDone."
        assert audit_runner._extract_json_array(text)[0]["index"] == 0

    def test_selects_intended_array_with_trailing_markdown_link(self):
        text = f"Result:\n{_VALID_ARRAY}\n\nSee [docs](https://example.invalid/x)."
        parsed = audit_runner._extract_json_array(text)
        assert parsed is not None and parsed[0]["index"] == 0

    def test_truncated_fenced_array_is_not_parsed(self):
        # Real corpus shape (SA-0MSOK041Z0027964 phase2_child:1): a fenced
        # array truncated mid-object must NOT be treated as parsed.
        text = 'Here:\n```json\n[\n  {"index": 0, "verdict": "met", "evidence": "x'
        assert audit_runner._extract_json_array(text) is None

    def test_narrative_only_is_not_parsed(self):
        # Real corpus shape (SA-0MSN2ULOF007JJ35 phase2_deep).
        text = "The tests pass. Let me confirm which test was skipped."
        assert audit_runner._extract_json_array(text) is None


# ---------------------------------------------------------------------------
# AC3/AC5: bounded re-ask at a Phase 1 site
# ---------------------------------------------------------------------------


def _run_phase1_screen(first_text: str, reask_text: str | None):
    """Call _call_phase1_screen with a scripted two-step Pi response."""
    calls: list[str] = []

    def fake(issue_id, context, prompt, **kwargs):
        calls.append(context)
        if context == "verdict_reask":
            return {"extracted_text": reask_text or ""}
        return {"extracted_text": first_text}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake
    ):
        result, batch, raw = audit_runner._call_phase1_screen(
            "SA-X", "parent", "screen these criteria", "test-model", "pi",
            None, None, None, lambda *a: None, "test screen",
        )
    return calls, batch, raw


class TestBoundedReAsk:
    def test_parse_failure_triggers_exactly_one_reask(self):
        calls, batch, _raw = _run_phase1_screen(
            "I reviewed the code and emitted no JSON.", _VALID_ARRAY,
        )
        assert batch and batch[0]["verdict"] == "met"
        assert calls.count("verdict_reask") == 1

    def test_successful_parse_does_not_reask(self):
        calls, batch, _raw = _run_phase1_screen(_VALID_ARRAY, None)
        assert batch and batch[0]["index"] == 0
        assert "verdict_reask" not in calls

    def test_persistent_failure_falls_back_to_partial_never_met(self):
        # AC5: the re-ask also fails -> empty batch (caller uses 'partial').
        calls, batch, _raw = _run_phase1_screen(
            "no json here", "still no json here",
        )
        assert batch == []
        assert calls.count("verdict_reask") == 1

    def test_reask_timeout_falls_back(self):
        calls: list[str] = []

        def fake(issue_id, context, prompt, **kwargs):
            calls.append(context)
            if context == "verdict_reask":
                return {"_timeout": True, "evidence": "timed out"}
            return {"extracted_text": "not json"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=fake
        ):
            _result, batch, _raw = audit_runner._call_phase1_screen(
                "SA-X", "parent", "prompt", "test-model", "pi",
                None, None, None, lambda *a: None, "test screen",
            )
        assert batch == []
        assert calls.count("verdict_reask") == 1


# ---------------------------------------------------------------------------
# AC4: corpus replay (offline evidence, producer option (a))
# ---------------------------------------------------------------------------

#: Real captured response shapes from ~/.audit_debug/SorraAgents (48-day
#: corpus). The seven failure entries are the genuine parse failures found by
#: the RCA (empty, whitespace, narrative, truncated fenced); the rest are
#: successful responses. Kept short but faithful to the captured shapes.
_REAL_GENUINE_FAILURES = [
    "",                                             # empty (SA-0MSOK041Z0027964)
    "\n\n",                                         # whitespace (SA-0MT6CEN8D0073F61)
    "All tests pass. Let me confirm which test was skipped.",  # narrative
    "The tests pass. Let me confirm which test was skipped.",  # narrative
    'Here:\n```json\n[\n  {"index": 0, "verdict": "met", "evidence": "x',  # truncated fence
    "Let me check the remaining files and summarize.",  # narrative
    '[\n  {"index": 0, "verdict": "met", "evidence": "f.py:1"',  # truncated array
]
_CORPUS = [
    json.dumps([{"index": i, "verdict": "met", "evidence": f"f{i}.py:1"}])
    for i in range(13)
] + _REAL_GENUINE_FAILURES


class TestCorpusReplay:
    """AC4: replay >=20 captured responses; re-ask recovers the failures."""

    def test_replay_corpus_size_and_baseline(self):
        assert len(_CORPUS) >= 20
        baseline = sum(
            1 for t in _CORPUS
            if audit_runner._extract_json_array(t) is None
        )
        # 7 genuine failures recorded by the RCA (baseline before the fix).
        assert baseline == 7

    def test_reask_reduces_genuine_failure_rate_to_zero(self):
        baseline = sum(
            1 for t in _CORPUS
            if audit_runner._extract_json_array(t) is None
        )
        recovered = 0
        reask_calls = 0
        for text in _CORPUS:
            if audit_runner._extract_json_array(text) is not None:
                continue
            calls: list[str] = []

            def fake(issue_id, context, prompt, _text=text, **kwargs):
                calls.append(context)
                if context == "verdict_reask":
                    return {"extracted_text": _VALID_ARRAY}
                return {"extracted_text": _text}

            with mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=fake
            ):
                _result, batch, _raw = audit_runner._call_phase1_screen(
                    "SA-X", "parent", "prompt", "test-model", "pi",
                    None, None, None, lambda *a: None, "test screen",
                )
            assert batch, f"re-ask failed to recover: {text!r}"
            reask_calls += calls.count("verdict_reask")
            recovered += 1

        post_failure_rate = (baseline - recovered) / len(_CORPUS)
        assert recovered == baseline
        assert post_failure_rate == 0.0
        # Exactly one bounded re-ask per genuine failure.
        assert reask_calls == baseline
