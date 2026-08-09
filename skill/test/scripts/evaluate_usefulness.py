#!/usr/bin/env python3
"""Evaluate whether a failing test is genuinely useful via code-path analysis.

Performs static code-path analysis (NOT comment-based) to detect the test
anti-patterns documented in skill/shared/test-writing-guidelines.md:

  1. source-code-grep tests     — readFileSync/read_text + toContain on source
  2. placeholder tests          — expect(true).toBe(true), zero assertions,
                                  TODO-only bodies
  3. self-referential simulations — test re-implements the logic it claims to
                                  test and imports no production code
  4. duplicate coverage         — same behaviour already covered via public API
                                  (heuristic — always report to user)

Verdicts are conservative: uncertainty returns ``report-to-user``, never an
automatic ``remove``.

Usage:
  evaluate_usefulness.py <test-file-path> [--json]
  evaluate_usefulness.py --file <test-file-path>

Exit codes:
  0 - evaluation completed
  2 - usage / IO error
"""  # noqa: EXE001

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERDICT_KEEP = "keep"
VERDICT_REMOVE = "remove"
VERDICT_REPORT = "report-to-user"

# Anti-pattern vetoes and their human-readable reasons.
VETO: dict[str, str] = {
    "source_grep": "reads source file text and asserts on it (source-code-grep anti-pattern)",
    "placeholder": "placeholder test (expect(true).toBe(true) or zero assertions)",
    "zero_assertion": "test body contains no assertions",
    "self_referential": "test re-implements logic with no production imports (self-referential simulation)",
    "duplicate": "same behaviour appears already covered via a public API (duplicate coverage heuristic)",
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+[^'\"]*?\s+from\s+|from\s+|import\s*)['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
_REQUIRE_RE = re.compile(r"^\s*(?:const|let|var)\s+.+=\s*require\s*\(['\"]([^'\"]+)['\"]", re.MULTILINE)
_SOURCE_READ_RE = re.compile(
    r"readFileSync|readFile|open\(|read_text|Path\.read|fs\.promises", re.MULTILINE
)
_STRING_ASSERT_RE = re.compile(
    r"toContain|toMatch|includes\(|indexOf\(|match\(/|\.match\(", re.MULTILINE
)
_EXPECT_TRUE_RE = re.compile(r"expect\s*\(\s*true\s*\)\s*\.\s*toBe\s*\(\s*true\s*\)")
_WEAK_ASSERT_RE = re.compile(
    r"expect\s*\(.+?\)\s*\.\s*(?:not\s*\.)?(?:toBeDefined|toBeTruthy|toBeUndefined|toBeNull)\s*\(",
    re.MULTILINE,
)
_ASSERT_RE = re.compile(
    r"expect\s*\(|assert\s*\(|assert\.|assertEqual|assertTrue|assertEquals|assert\.",
    re.MULTILINE,
)
_TODO_RE = re.compile(r"TODO|FIXME|placeholder|not implemented", re.IGNORECASE)
_FUNCTION_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+|^\s*(?:export\s+)?const\s+\w+\s*=\s*(?:async\s*)?\(",
    re.MULTILINE,
)
_RETURN_RE = re.compile(r"^\s*return\s+", re.MULTILINE)
_TEST_BODY_RE = re.compile(
    r"(?:test|it)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*(?:async\s*)?(?:\([^)]*\)\s*=>|function\s*\([^)]*\)\s*\{)",
    re.MULTILINE,
)


@dataclass
class Verdict:
    """Structured evaluation result for a single test file."""

    file: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "summary": self.summary,
        }


def _imports_production_code(content: str) -> bool:
    """True if the test imports anything outside itself (any import counts
    as exercising real code paths — conservative)."""
    for match in _IMPORT_RE.finditer(content):
        module = match.group(1)
        if not module.startswith(".") and not module.startswith("/"):
            # Bare specifier (framework) — ignore, but relative imports matter.
            continue
        if module in (".", "..", "../..", "../../.."):
            continue
        return True
    for match in _REQUIRE_RE.finditer(content):
        module = match.group(1)
        if module.startswith((".", "/")):
            return True
    return False


def _has_assertions(content: str) -> bool:
    return bool(_ASSERT_RE.search(content))


def _count_test_cases(content: str) -> int:
    return len(_TEST_BODY_RE.findall(content))


def _detect_source_grep(content: str) -> bool:
    """Anti-pattern 1: reads source text and asserts a string on it."""
    has_read = bool(_SOURCE_READ_RE.search(content))
    has_string_assert = bool(_STRING_ASSERT_RE.search(content))
    return has_read and has_string_assert


def _detect_placeholder(content: str) -> tuple[bool, str]:
    """Anti-pattern 2: placeholder / zero-assertion tests."""
    if _EXPECT_TRUE_RE.search(content):
        return True, VETO["placeholder"]
    if _TEST_BODY_RE.search(content) and not _has_assertions(content):
        return True, VETO["zero_assertion"]
    return False, ""


def _detect_self_referential(content: str) -> tuple[bool, str]:
    """Anti-pattern 3: re-implements logic and imports no production code."""
    if _imports_production_code(content):
        return False, ""
    # Multiple function definitions with returns inside a test file and no
    # production import strongly suggests the test simulates the logic itself.
    func_defs = len(_FUNCTION_DEF_RE.findall(content))
    has_returns = bool(_RETURN_RE.search(content))
    if func_defs >= 1 and has_returns and not _imports_production_code(content):
        return True, VETO["self_referential"]
    return False, ""


def _detect_weak_assertion(content: str) -> tuple[bool, str]:
    """Weak smoke assertions (toBeDefined etc.) are uncertain coverage.

    A test that only asserts something is defined provides almost no
    behavioural guarantee; whether it is genuinely useful cannot be decided
    by code-path analysis alone — report to the user.
    """
    assertions = list(_ASSERT_RE.finditer(content))
    if not assertions:
        return False, ""
    weak = list(_WEAK_ASSERT_RE.finditer(content))
    if weak and len(weak) >= len(assertions):
        return True, "weak assertion (toBeDefined/toBeTruthy only) provides uncertain coverage"
    return False, ""


def _detect_duplicate(content: str) -> tuple[bool, str]:
    """Anti-pattern 4: duplicate coverage heuristic.

    Detects test names that look like re-tests of the same behaviour (e.g.
    "... again", "also", "duplicate") while importing the same production
    module. Always reports to user — never auto-removes.
    """
    duplicate_markers = re.compile(r"again|also covers|duplicate|already covered", re.IGNORECASE)
    test_names = _TEST_BODY_RE.findall(content)
    if any(duplicate_markers.search(name) for name in test_names) and _imports_production_code(content):
        return True, VETO["duplicate"]
    return False, ""


def evaluate_file(path: Path | str) -> Verdict:
    """Evaluate a single test file and return a conservative verdict.

    Verdict order (first matching anti-pattern wins; all reasons are
    collected). Uncertainty always reports to the user.
    """
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Verdict(
            file=str(path),
            verdict=VERDICT_REPORT,
            reasons=[f"cannot read test file: {exc}"],
            summary="Could not read the test file; report to user.",
        )

    reasons: list[str] = []

    if _detect_source_grep(content):
        reasons.append(VETO["source_grep"])

    placeholder, placeholder_reason = _detect_placeholder(content)
    if placeholder:
        reasons.append(placeholder_reason)

    selfref, selfref_reason = _detect_self_referential(content)
    if selfref:
        reasons.append(selfref_reason)

    duplicate, duplicate_reason = _detect_duplicate(content)
    if duplicate:
        reasons.append(duplicate_reason)

    weak, weak_reason = _detect_weak_assertion(content)
    if weak:
        reasons.append(weak_reason)

    if reasons:
        auto_remove = (
            VETO["source_grep"],
            VETO["placeholder"],
            VETO["zero_assertion"],
            VETO["self_referential"],
        )
        verdict = VERDICT_REMOVE if any(r in auto_remove for r in reasons) else VERDICT_REPORT
        # Duplicate-only findings are always report (heuristic).
        if duplicate and all(r == VETO["duplicate"] for r in reasons):
            verdict = VERDICT_REPORT
        summary = _summarize(verdict, reasons)
        return Verdict(file=str(path), verdict=verdict, reasons=reasons, summary=summary)

    return Verdict(
        file=str(path),
        verdict=VERDICT_KEEP,
        reasons=[],
        summary="No anti-pattern detected via code-path analysis; test appears useful.",
    )


def _summarize(verdict: str, reasons: list[str]) -> str:
    if verdict == VERDICT_REMOVE:
        return f"Anti-patterns detected — candidate for removal: {'; '.join(reasons)}."
    if verdict == VERDICT_REPORT:
        return f"Uncertain or heuristic findings — report to user: {'; '.join(reasons) if reasons else 'could not classify'}."
    return "Test appears useful."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate whether a test file is genuinely useful (code-path analysis)."
    )
    parser.add_argument("test_file", nargs="?", help="Path to the test file to evaluate.")
    parser.add_argument("--file", dest="file_opt", help="Alternative way to pass the test file path.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    test_file = args.file_opt or args.test_file
    if not test_file:
        parser.error("a test file path is required")

    verdict = evaluate_file(test_file)
    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))
    else:
        print(f"file: {verdict.file}")
        print(f"verdict: {verdict.verdict}")
        for reason in verdict.reasons:
            print(f"  - {reason}")
        print(f"summary: {verdict.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
