"""Docs-vs-source consistency for the finish main-checkout guard.

SA-0MVXVC127009T0NP: an agent that hits the finish main-checkout guard reads
`docs/dev/implement-skill-reference.md` § "Finish main-checkout guard" to
predict what the guard will do — which stages it verifies, what the report
entry it is reading carries, which comment it is about to receive and why a
path is or is not allow-listed. A stage added to the code, a report field
renamed, or a test reference that no longer resolves would silently leave that
guidance wrong, so this test fails in either direction:

* every stage the code can record is documented, and the documented stage
  table lists exactly those stages, and
* every report field the document promises exists under the names it shows,
  and every test reference it makes resolves to a real test file (and test
  function where named).

The documents are read as text on purpose: the contract under test *is* the
wording an operator sees. No source grep: the stage vocabulary and field
values come from the production module, the references are resolved on disk.
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

GUARD_DOC = REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"
WORKTREE_DOC = REPO_ROOT / "docs" / "dev" / "worktree-isolation.md"

#: The top-level keys of ``report["steps"]["main_checkout_guard"]`` the
#: document promises, and the keys of one ``guard["verifies"]`` entry.
GUARD_REPORT_FIELDS: tuple[str, ...] = (
    "repo_root",
    "snapshot_at",
    "baseline_paths",
    "own_churn",
    "verifies",
    "verdict",
)
VERIFY_ENTRY_FIELDS: tuple[str, ...] = (
    "stage",
    "at",
    "verdict",
    "offending",
    "actor_evidence",
)

#: Test files that the guard section references without a directory (they live
#: beside this test). A bare name is resolved against the candidate dirs below.
TEST_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "skill" / "implement" / "tests",
    REPO_ROOT / "tests",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _guard_section() -> str:
    """The markdown of the guard section, from its heading to the next `## `."""
    lines = _read(GUARD_DOC).splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("## Finish main-checkout guard")
    )
    end = next(
        (i for i, line in enumerate(lines[start + 1 :], start=start + 1)
         if line.startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _documented_stage_table() -> list[str]:
    """The first-column stage labels of the verify-stage table, in order."""
    lines = _guard_section().splitlines()
    header = next(
        (i for i, line in enumerate(lines) if line.startswith("| Stage |")),
        None,
    )
    assert header is not None, (
        "the guard section lost its verify-stage table: an agent cannot "
        "predict which stages can appear in guard['verifies']"
    )
    stages: list[str] = []
    for row in lines[header + 1 :]:
        if re.match(r"^\|\s*-{3,}", row):
            continue  # the markdown header separator
        match = re.match(r"^\|\s*`([a-z_]+)`\s*\|", row)
        if match is None:
            break
        stages.append(match.group(1))
    return stages


def _stage_row(stage: str) -> str:
    """The markdown table row documenting *stage*."""
    for line in _guard_section().splitlines():
        if re.match(rf"^\|\s*`{re.escape(stage)}`\s*\|", line):
            return line
    raise AssertionError(f"no table row documents verify stage {stage!r}")


def _resolve_test_file(name: str) -> Path | None:
    """Resolve a test-file reference from the guard section to a real file."""
    if name.startswith(("skill/", "tests/")):
        candidate = REPO_ROOT / name
        return candidate if candidate.is_file() else None
    for directory in TEST_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    matches = sorted(REPO_ROOT.glob(f"skill/*/tests/{name}"))
    return matches[0] if matches else None


def _test_references() -> list[tuple[str, str | None]]:
    """``(file, function or None)`` for every test reference in the section."""
    pattern = re.compile(r"([\w/\.-]+\.py)(?:::(\w+))?")
    references: list[tuple[str, str | None]] = []
    for match in pattern.finditer(_guard_section()):
        name = match.group(1)
        if "/" not in name and not name.startswith("test_"):
            continue  # not a test-file reference (e.g. `implement.py`)
        references.append((name, match.group(2)))
    return references


@pytest.mark.parametrize("stage", implement.VERIFY_STAGES)
def test_every_verify_stage_is_documented(stage: str) -> None:
    """A stage the code can record in ``verifies`` must be explained."""
    section = _guard_section()
    assert f"`{stage}`" in section, (
        f"verify stage {stage!r} can appear in guard['verifies'] but is not "
        "documented in the guard section"
    )
    # And it must be in the stage table, not merely mentioned in prose.
    assert stage in _documented_stage_table(), (
        f"verify stage {stage!r} is not listed in the verify-stage table"
    )


def test_documented_stage_table_matches_the_source_vocabulary() -> None:
    """The table lists exactly the stages the code can produce, in order."""
    documented = _documented_stage_table()

    assert documented == list(implement.VERIFY_STAGES), (
        "the documented verify stages and VERIFY_STAGES have drifted: "
        f"documented={documented}, source={list(implement.VERIFY_STAGES)}"
    )


def test_refactor_stage_is_documented_as_advisory() -> None:
    """The non-gating refactor verify must be described as advisory."""
    row = _stage_row("refactor").lower()

    assert "advisory" in row, (
        "the refactor verify row must state that it is advisory — it is "
        "report/comment only and never aborts the run"
    )
    assert "no" in row, (
        "the refactor verify row must state that it does not abort the run"
    )


@pytest.mark.parametrize("field", GUARD_REPORT_FIELDS)
def test_every_guard_report_field_is_documented(field: str) -> None:
    """Each top-level guard report field is documented under its real name."""
    assert f'guard["{field}"]' in _guard_section(), (
        f"guard report field {field!r} is not documented in the guard section"
    )


@pytest.mark.parametrize("field", VERIFY_ENTRY_FIELDS)
def test_every_verify_entry_field_is_documented(field: str) -> None:
    """Each field of a ``verifies`` entry is documented under its real name."""
    assert f"verifies[].{field}" in _guard_section(), (
        f"verify entry field {field!r} is not documented in the guard section"
    )


def test_guard_section_test_references_exist() -> None:
    """Every test file/test function the section names must exist."""
    references = _test_references()
    assert references, (
        "the guard section references no test at all: it must name the "
        "regression pins for the behaviour it documents"
    )
    for name, function in references:
        resolved = _resolve_test_file(name)
        assert resolved is not None, (
            f"the guard section references {name!r}, which does not exist"
        )
        if function is not None:
            assert re.search(
                rf"^def {re.escape(function)}\b", _read(resolved), re.MULTILINE
            ), (
                f"the guard section references {name}::{function}, but "
                f"{resolved.relative_to(REPO_ROOT)} has no such test"
            )


def test_attribution_contract_is_documented() -> None:
    """The attribution contract is where a reader of the guard looks."""
    section = _guard_section()

    assert "own_churn" in section
    assert "observed dirty" in section, (
        "the section must state that own_churn is confined to churn observed "
        "in the finished checkout"
    )
    assert "never allow-listed" in section, (
        "the section must state that deletions are never allow-listed"
    )
    assert "_repository_root_mismatch" in section, (
        "the section must name the finished-repo confinement check"
    )


def test_fail_closed_repo_root_rule_is_documented() -> None:
    """The fail-closed ``repo_root`` resolution rule is documented."""
    section = _guard_section()

    assert "### Fail-closed `repo_root` resolution" in section, (
        "the guard section must have a dedicated fail-closed repo_root rule"
    )
    assert "_resolve_main_repo_root" in section
    assert "derived" in section, (
        "the section must state that the main root is derived, never trusted "
        "from the state file alone"
    )


def test_worktree_isolation_reference_lists_the_failure_path_stages() -> None:
    """The normative main-checkout table names the failure-path stages too."""
    doc = _read(WORKTREE_DOC)

    for stage in implement.VERIFY_STAGES:
        if stage == "refactor":
            continue  # advisory, called out separately in the sentence
        assert f"`{stage}`" in doc, (
            f"worktree-isolation.md does not name the {stage!r} verify stage "
            "in the finish guard row, so its enforcement table is stale"
        )
