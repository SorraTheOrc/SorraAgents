"""Tests for the UK English requirement in AGENTS_GLOBAL.md.

The global agent guidance (``AGENTS_GLOBAL.md``, installed to
``~/.pi/agent/AGENTS.md``) must instruct every agent to write in UK English,
with a concrete example, in a prominent location.

Related work item: SA-0MTSOBDRS009W85V
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_GLOBAL = _REPO_ROOT / "AGENTS_GLOBAL.md"

_CRITICAL_RULES_HEADER = "## CRITICAL RULES"


def _content() -> str:
    assert _AGENTS_GLOBAL.exists(), f"File not found: {_AGENTS_GLOBAL}"
    return _AGENTS_GLOBAL.read_text(encoding="utf-8")


def _critical_rules_section() -> str:
    """Return the body of the CRITICAL RULES section (exclusive of the heading)."""
    content = _content()
    assert _CRITICAL_RULES_HEADER in content, (
        f"{_AGENTS_GLOBAL.name} does not contain a {_CRITICAL_RULES_HEADER!r} section"
    )
    body = content.split(_CRITICAL_RULES_HEADER, 1)[1]
    next_header = body.find("\n## ")
    return body if next_header == -1 else body[:next_header]


def _uk_english_rule() -> str:
    """Return the CRITICAL RULES bullet that states the UK English requirement."""
    for line in _critical_rules_section().splitlines():
        if "UK English" in line:
            return line
    raise AssertionError(
        f"{_AGENTS_GLOBAL.name} CRITICAL RULES has no UK English requirement"
    )


def test_uk_english_requirement_is_a_critical_rule() -> None:
    """AC-1/AC-3: a UK English instruction is present in CRITICAL RULES."""
    rule = _uk_english_rule()
    assert "UK English" in rule


def test_uk_english_rule_applies_to_written_output() -> None:
    """AC-1: the requirement covers all text output (not just code comments)."""
    rule = _uk_english_rule()
    assert "output" in rule.lower(), (
        f"UK English rule should apply to written output, got: {rule!r}"
    )


def test_uk_english_rule_includes_concrete_example() -> None:
    """AC-2: the rule shows a concrete spelling example ('colour' not 'color')."""
    rule = _uk_english_rule()
    assert "colour" in rule, f"UK English rule should mention 'colour', got: {rule!r}"
    assert "color" in rule, f"UK English rule should contrast 'color', got: {rule!r}"


def test_existing_critical_rules_preserved() -> None:
    """AC-4: adding the rule does not remove pre-existing critical rules."""
    section = _critical_rules_section()
    for existing in (
        "Use wl for ALL task tracking",
        "Never commit without: a work item association",
        "Session logging:",
    ):
        assert existing in section, f"pre-existing critical rule removed: {existing!r}"
