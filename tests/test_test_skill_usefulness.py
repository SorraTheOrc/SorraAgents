"""Unit tests for skill/test/scripts/evaluate_usefulness.py.

Covers code-path anti-pattern detection (source-grep, placeholder,
self-referential simulation, duplicate coverage) per
skill/shared/test-writing-guidelines.md, with conservative verdicts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skill.test.scripts.evaluate_usefulness import (
    VERDICT_KEEP,
    VERDICT_REMOVE,
    VERDICT_REPORT,
    VETO,
    evaluate_file,
)

# ---------------------------------------------------------------------------
# Fixtures — small test files exercising each anti-pattern
# ---------------------------------------------------------------------------

SOURCE_GREP_TEST = """\
import { readFileSync } from 'fs';
import { join } from 'path';

test('source contains the handler', () => {
  const source = readFileSync(join(__dirname, '../src/engine.ts'), 'utf8');
  expect(source).toContain('handlePile');
});
"""

PLACEHOLDER_TEST = """\
// TODO: implement real coverage once feature ships
test('placeholder', () => {
  expect(true).toBe(true);
});
"""

ZERO_ASSERTION_TEST = """\
test('boots the scene', async () => {
  await mount(<Game />);
  // never asserts anything about the result
});
"""

SELF_REFERENTIAL_TEST = """\
// No production imports — re-implements the logic it claims to test.
function computeTotal(items) {
  return items.reduce((sum, item) => sum + item.price, 0);
}

test('computeTotal sums prices', () => {
  const items = [{ price: 1 }, { price: 2 }];
  expect(computeTotal(items)).toBe(3);
});
"""

GOOD_TEST = """\
import { computeTotal } from '../src/engine';

test('computeTotal sums prices', () => {
  const items = [{ price: 1 }, { price: 2 }];
  expect(computeTotal(items)).toBe(3);
});
"""

DUPLICATE_TEST = """\
import { computeTotal } from '../src/engine';

// Re-tests the same behaviour already covered by engine.test.ts
test('computeTotal sums prices again', () => {
  const items = [{ price: 1 }, { price: 2 }];
  expect(computeTotal(items)).toBe(3);
});
"""


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    return tmp_path


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Verdict constants
# ---------------------------------------------------------------------------


def test_verdict_constants_are_distinct() -> None:
    assert {VERDICT_KEEP, VERDICT_REMOVE, VERDICT_REPORT} == {"keep", "remove", "report-to-user"}


# ---------------------------------------------------------------------------
# Source-grep detection
# ---------------------------------------------------------------------------


def test_detects_source_code_grep_test(fixture_dir: Path) -> None:
    path = _write(fixture_dir, "grep.test.ts", SOURCE_GREP_TEST)
    verdict = evaluate_file(path)
    assert verdict.verdict == VERDICT_REMOVE
    assert VETO["source_grep"] in verdict.reasons


# ---------------------------------------------------------------------------
# Placeholder detection
# ---------------------------------------------------------------------------


def test_detects_placeholder_expect_true(fixture_dir: Path) -> None:
    path = _write(fixture_dir, "placeholder.test.ts", PLACEHOLDER_TEST)
    verdict = evaluate_file(path)
    assert verdict.verdict == VERDICT_REMOVE
    assert any("placeholder" in reason.lower() for reason in verdict.reasons)


def test_detects_zero_assertion_test(fixture_dir: Path) -> None:
    path = _write(fixture_dir, "zero.test.ts", ZERO_ASSERTION_TEST)
    verdict = evaluate_file(path)
    assert verdict.verdict == VERDICT_REMOVE
    assert any("assertion" in reason.lower() for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# Self-referential simulation detection
# ---------------------------------------------------------------------------


def test_detects_self_referential_simulation(fixture_dir: Path) -> None:
    path = _write(fixture_dir, "selfref.test.ts", SELF_REFERENTIAL_TEST)
    verdict = evaluate_file(path)
    assert verdict.verdict == VERDICT_REMOVE
    assert any("self-referential" in reason.lower() for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# Conservative verdicts
# ---------------------------------------------------------------------------


def test_good_test_is_kept(fixture_dir: Path) -> None:
    path = _write(fixture_dir, "good.test.ts", GOOD_TEST)
    verdict = evaluate_file(path)
    assert verdict.verdict == VERDICT_KEEP


def test_uncertain_test_reports_to_user(fixture_dir: Path) -> None:
    """A test we cannot confidently classify must report, never auto-remove."""
    content = """\
import { something } from '../src/mystery';

test('mystery behaviour', () => {
  expect(something()).toBeDefined();
});
"""
    path = _write(fixture_dir, "mystery.test.ts", content)
    verdict = evaluate_file(path)
    assert verdict.verdict == VERDICT_REPORT
    assert verdict.reasons


def test_duplicate_coverage_reports_to_user(fixture_dir: Path) -> None:
    """Duplicate detection is heuristic — report to user rather than auto-remove."""
    path = _write(fixture_dir, "duplicate.test.ts", DUPLICATE_TEST)
    verdict = evaluate_file(path)
    assert verdict.verdict == VERDICT_REPORT
    assert any("duplicate" in reason.lower() for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def test_verdict_reasoning_surfaces_in_structured_output(fixture_dir: Path) -> None:
    path = _write(fixture_dir, "grep.test.ts", SOURCE_GREP_TEST)
    verdict = evaluate_file(path)
    payload = verdict.to_dict()
    assert payload["file"] == str(path)
    assert payload["verdict"] == VERDICT_REMOVE
    assert payload["reasons"]
    assert payload["summary"]
