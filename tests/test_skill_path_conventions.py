"""Regression test: no legacy `skill/<name>/` path references in SKILL.md docs.

The pi skill convention (WL-0MQOIKGW2005BLZH) requires skill docs to
reference scripts, assets, and sibling docs via relative paths — e.g.
``./scripts/foo.py`` or ``../target/scripts/foo.py`` — never the legacy
``skill/<name>/...`` prefix form.

This regression has recurred multiple times in SorraAgents (commits
``0fa9de7``, ``636dc925``, and ``1dff594c`` via SA-0MSISKM8F004NW1U, which
re-introduced ``skill/audit/scripts/verify_context_reduction.py`` and
``skill/audit/tests/test_verify_context_reduction.py`` into
``skill/audit/SKILL.md`` — SA-0MSOIJSHQ003ANQK). This test scans every
``skill/*/SKILL.md`` and fails on any such reference so the regression
cannot recur undetected.

Detection mirrors the ContextHub suite
(``ContextHub/tests/skill-path-conventions.test.ts``), which validates the
installed ``~/.pi/agent/skills/*/SKILL.md`` copies; this test validates the
repo's source-of-truth docs that get installed by ``scripts/install_pi.sh``.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skill"

# Matches `skill/<name>/` path references, e.g. skill/audit/scripts/foo.py
# or skill/implement/SKILL.md (same pattern as the ContextHub test).
LEGACY_REF_RE = re.compile(r"skill/[a-zA-Z0-9_-]+(/[a-zA-Z0-9_./-]+)?")


def _find_legacy_refs(text: str) -> list[str]:
    """Return every legacy ``skill/<name>/`` path reference in *text*.

    Skips matches where "skill" is the tail of a longer word (e.g.
    "reskill/") — mirrors ContextHub's word-boundary guard.
    """
    refs: list[str] = []
    for match in LEGACY_REF_RE.finditer(text):
        if match.start() > 0 and re.match(r"\w", text[match.start() - 1]):
            continue  # "skill" is part of a larger word, not a path prefix
        refs.append(match.group(0))
    return refs


def _all_skill_files() -> list[Path]:
    """Every SKILL.md under the repo's skill/ directory."""
    return sorted(SKILL_DIR.glob("*/SKILL.md"))


def test_no_legacy_skill_path_references_in_any_skill_doc() -> None:
    """No SKILL.md may reference a path via the legacy `skill/<name>/` form."""
    offenders: list[str] = []
    for skill_file in _all_skill_files():
        refs = _find_legacy_refs(skill_file.read_text(encoding="utf-8"))
        if refs:
            offenders.append(f"{skill_file.relative_to(REPO_ROOT)}: {refs}")
    assert not offenders, (
        "Legacy `skill/<name>/` path references found (use ./ or ../ relative "
        "paths instead):\n" + "\n".join(offenders)
    )


def test_detector_flags_the_known_regression_content() -> None:
    """The detector must catch the exact content regressed in 1dff594c.

    Keeps the guard honest: if the pattern ever stops matching, this test
    fails and signals the detector is broken — not that the docs are clean.
    """
    pre_fix = (
        "Verification script: `skill/audit/scripts/verify_context_reduction.py` "
        "implements the AC2/AC3 checks. Unit tests: "
        "`skill/audit/tests/test_verify_context_reduction.py`."
    )
    refs = _find_legacy_refs(pre_fix)
    assert refs == [
        "skill/audit/scripts/verify_context_reduction.py",
        "skill/audit/tests/test_verify_context_reduction.py",
    ]


def test_detector_ignores_larger_words_and_relative_paths() -> None:
    """Word-boundary guard: 'reskill/x' is not a path ref; ./ and ../ are fine."""
    clean = (
        "Cross-skill refs use `../test/scripts/run_tests.py`; in-skill refs use "
        "`./scripts/audit_runner.py`. reskill/ and skillset/ are ordinary words."
    )
    assert _find_legacy_refs(clean) == []
