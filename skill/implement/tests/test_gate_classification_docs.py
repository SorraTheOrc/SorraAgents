"""Docs-vs-source consistency for the gate classification vocabulary.

SA-0MVTET8KQ0031R6Z (child SA-0MVU8XT1M002MSJX): an operator reads the gate's
`reason` and its fact fields in `skill/implement/SKILL.md` and in the two
reference documents — never in the source. A renamed reason, a dropped fact
field or an undocumented new notice code would silently leave that guidance
wrong, so this test fails in either direction:

* a value documented for the operator must exist in the source constants, and
* every source value must be documented.

The documents are read as text on purpose: the contract under test *is* the
wording an operator sees.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement
from skill.test.scripts import run_tests

SKILL_MD = REPO_ROOT / "skill" / "implement" / "SKILL.md"
GATE_DOC = REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"
TEST_DOC = REPO_ROOT / "docs" / "dev" / "test-skill-reference.md"

#: The reasons the finish gate can report — read from the source, so adding a
#: reason without documenting it fails here.
GATE_REASONS: tuple[str, ...] = (
    implement.GATE_FAILURE_TESTS,
    implement.GATE_FAILURE_TIMEOUT,
    implement.GATE_FAILURE_KILLED,
    implement.GATE_FAILURE_UNPARSED,
    implement.GATE_FAILURE_SLOT_CONTENTION,
    implement.GATE_FAILURE_INFRASTRUCTURE,
    implement.GATE_FAILURE_UNKNOWN,
)

#: Runner notice codes the gate classifies on (produced by the test skill,
#: consumed by the gate) — documented in the test skill's reference.
RUNNER_NOTICE_CODES: tuple[str, ...] = (
    run_tests.NOTICE_SLOT_BUSY,
    run_tests.NOTICE_SUITE_TIMEOUT,
    run_tests.NOTICE_KILLED,
    run_tests.NOTICE_UNPARSED_FAILURE,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _reason_table_tokens(markdown: str) -> list[str]:
    """The first-column values of the gate-reason table in *markdown*."""
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if "| `reason` |" not in line:
            continue
        tokens: list[str] = []
        for row in lines[index + 1 :]:
            if re.match(r"^\|\s*-{3,}", row):
                continue  # the markdown header separator
            match = re.match(r"^\|\s*`([a-z_]+)`\s*\|", row)
            if match is None:
                break
            tokens.append(match.group(1))
        return tokens
    return []


@pytest.mark.parametrize("reason", GATE_REASONS)
def test_every_gate_reason_is_documented_for_the_operator(reason: str) -> None:
    """A reason an operator can receive must be explained, with its remedy."""
    for doc in (SKILL_MD, GATE_DOC):
        assert f"`{reason}`" in _read(doc), (
            f"gate reason {reason!r} is not documented in {doc.name} — an "
            "operator woken by a red gate cannot act on an unexplained reason"
        )


@pytest.mark.parametrize("field", implement.GATE_FACT_FIELDS)
def test_every_fact_field_is_documented(field: str) -> None:
    """Every fact the gate reports must be documented with its meaning."""
    for doc in (SKILL_MD, GATE_DOC):
        assert f"`{field}`" in _read(doc), (
            f"gate fact field {field!r} is not documented in {doc.name}"
        )


@pytest.mark.parametrize("code", RUNNER_NOTICE_CODES)
def test_every_runner_notice_code_is_documented(code: str) -> None:
    """The codes the gate classifies on are documented in the test skill doc."""
    assert f"`{code}`" in _read(TEST_DOC), (
        f"runner notice code {code!r} is not documented in {TEST_DOC.name}"
    )


def test_documented_reason_table_matches_the_source_vocabulary() -> None:
    """The reason table lists exactly the reasons the source can produce."""
    documented = _reason_table_tokens(_read(SKILL_MD))

    assert documented, (
        f"no gate-reason table found in {SKILL_MD.name}: the operator guidance "
        "must list the reasons with the action for each"
    )
    assert set(documented) == set(GATE_REASONS), (
        "the documented gate reasons and the source constants have drifted: "
        f"documented={sorted(set(documented))}, "
        f"source={sorted(set(GATE_REASONS))}"
    )


def test_test_reference_points_at_the_gate_reasons() -> None:
    """The two reference documents cross-reference each other."""
    test_doc = _read(TEST_DOC)

    assert "implement-skill-reference.md#gate-failure-diagnostics" in test_doc, (
        "the test skill reference must cross-reference the finish gate's "
        "reason mapping so a reader of the notice codes finds the remedy"
    )
