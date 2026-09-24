"""Regression tests for skill-doc path conventions.

Two banned forms, both break command invocation from the project CWD:

1. Legacy ``skill/<name>/`` path references in SKILL.md docs (e.g.
   ``skill/audit/scripts/foo.py``). The old pi skill convention was relative
   paths — ``./scripts/foo.py`` or ``../target/scripts/foo.py`` — never the
   legacy ``skill/<name>/...`` prefix form.

2. CWD-relative script references (``./scripts/foo.py``,
   ``../<skill>/scripts/foo.py``) as **command invocations**. These resolve
   against the project CWD the agent runs from, not the skill directory, so
   they fail whenever the skill is invoked from anywhere but its own
   directory. The canonical form resolves the skill dir at runtime via the
   ``skill_path`` tool: ``$(skill_path <name>)/scripts/foo.py``.

The docs may still *warn against* relative paths in prose (e.g. "never run
``./scripts/...`` relative to the project repo") — those references are
excluded below; only executable command forms are flagged.

This regression has recurred multiple times in SorraAgents (commits
``0fa9de7``, ``636dc925``, and ``1dff594c`` via SA-0MSISKM8F004NW1U, which
re-introduced ``skill/audit/scripts/verify_context_reduction.py`` and
``skill/audit/tests/test_verify_context_reduction.py`` into
``skill/audit/SKILL.md`` — SA-0MSOIJSHQ003ANQK). This test scans every
``skill/*/SKILL.md`` and fails on any such reference so the regression
cannot recur undetected.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skill"

# Matches `skill/<name>/` path references, e.g. skill/audit/scripts/foo.py
# or skill/implement/SKILL.md (same pattern as the ContextHub test).
LEGACY_REF_RE = re.compile(r"skill/[a-zA-Z0-9_-]+(/[a-zA-Z0-9_./-]+)?")

# Command invocation with an interpreter prefix, e.g.
# `python3 ./scripts/x.py` or `python3 ../test/scripts/run_tests.py`.
INTERPRETER_CMD_RE = re.compile(
    r"(?:^|[^`\w])(?:python3|node|bash|sh) "
    r"(?:\./scripts/|\.\./[\w-]+/scripts/)[A-Za-z0-9_][^\s`]*",
    re.MULTILINE,
)

# Bare relative script reference at a line start (no interpreter), e.g.
# `./scripts/speak.sh "hi"` inside a fenced code block.
BARE_CMD_RE = re.compile(
    r"(?:^|[^`\w])(?:\./scripts/|\.\./[\w-]+/scripts/)[A-Za-z0-9_][^\s`]*",
    re.MULTILINE,
)

# Backtick code span that references a script via a relative path, e.g.
# `` `./scripts/audit_runner.py` ``. Excludes prose ellipses
# (`` `./scripts/...` ``) because a real filename token is required.
SPAN_RE = re.compile(r"`(?:\./scripts/|\.\./[\w-]+/scripts/)[A-Za-z0-9_][^\s`]*")


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


def _relative_script_refs(text: str) -> list[str]:
    """Return every CWD-relative script invocation found in *text*."""
    refs: list[str] = []
    for pattern in (INTERPRETER_CMD_RE, BARE_CMD_RE, SPAN_RE):
        refs.extend(pattern.findall(text))
    return refs


def test_no_legacy_skill_path_references_in_any_skill_doc() -> None:
    """No SKILL.md may reference a path via the legacy `skill/<name>/` form."""
    offenders: list[str] = []
    for skill_file in _all_skill_files():
        refs = _find_legacy_refs(skill_file.read_text(encoding="utf-8"))
        if refs:
            offenders.append(f"{skill_file.relative_to(REPO_ROOT)}: {refs}")
    assert not offenders, (
        "Legacy `skill/<name>/` path references found (use $(skill_path <name>) "
        "instead):\n" + "\n".join(offenders)
    )


def test_no_relative_script_invocations_in_any_skill_doc() -> None:
    """No SKILL.md may invoke scripts via CWD-relative paths.

    Commands (``python3 ./scripts/x.py`` etc.), bare fenced commands
    (``./scripts/x.py``), and backtick code spans (``./scripts/x.py``,
    ``../<skill>/scripts/x.py``) resolve against the project CWD, not the
    skill directory. All invocations must resolve the skill dir at runtime
    via ``skill_path``.
    """
    offenders: list[str] = []
    for skill_file in _all_skill_files():
        refs = _relative_script_refs(skill_file.read_text(encoding="utf-8"))
        if refs:
            offenders.append(f"{skill_file.relative_to(REPO_ROOT)}: {refs}")
    assert not offenders, (
        "CWD-relative script references found (use $(skill_path <name>)/scripts/ "
        "instead):\n" + "\n".join(offenders)
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


def test_detector_flags_relative_script_invocations() -> None:
    """The detector must catch CWD-relative script invocations."""
    bad = (
        "Run `./scripts/audit_runner.py issue SA-123`\n"
        "python3 ../test/scripts/run_tests.py --json\n"
        "./scripts/speak.sh 'hi'"
    )
    refs = _relative_script_refs(bad)
    assert refs, "detector must flag interpreter, bare, and span forms"
    assert any("./scripts/audit_runner.py" in r for r in refs)
    assert any("../test/scripts/run_tests.py" in r for r in refs)


def test_detector_ignores_skill_path_and_prohibition_prose() -> None:
    """skill_path refs and prose that warns against relative paths are fine."""
    clean = (
        "Cross-skill refs use `$(skill_path test)/scripts/run_tests.py`; in-skill "
        "refs use `$(skill_path audit)/scripts/audit_runner.py`. "
        "reskill/ and skillset/ are ordinary words. "
        "Prose may say: never run `./scripts/...` relative to the project repo, "
        "and node $(skill_path ship)/scripts/run-release.js is canonical."
    )
    assert _find_legacy_refs(clean) == []
    assert _relative_script_refs(clean) == []