"""Docs-vs-source consistency for the pane-driven `drive` transport.

SA-0MVPHZJ22007L8LQ: an operator (or an auditor) must be able to discover,
enable and troubleshoot pane-per-child driving from the skill documentation
alone. That promise only holds while the flag names, environment variables and
report fields the docs print are the ones the shipped `implement.py` accepts
and emits — a renamed flag or a dropped report key would silently leave the
guidance wrong.

The documents are read as text on purpose: the contract under test *is* the
wording an operator sees. The shipped values are read from the source (and the
flag names are exercised through the real argument parser), so this test fails
in either direction:

* a value documented for the operator must exist in the source, and
* the operator-facing facts for this transport must be documented in both
  ``skill/implement/SKILL.md`` and ``docs/dev/implement-skill-reference.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

SKILL_MD = REPO_ROOT / "skill" / "implement" / "SKILL.md"
DRIVE_DOC = REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"

#: Both documents must carry the operator-facing pane-driving guidance
#: (AC1/AC2): the workflow skill and the drive reference.
DOCS = (SKILL_MD, DRIVE_DOC)

#: The flags the docs name, and the parser attribute each one drives.
PANE_FLAGS: tuple[tuple[str, str, object], ...] = (
    ("--pane-sessions", "pane_sessions", True),
    ("--no-pane-sessions", "pane_sessions", False),
    ("--focus", "focus", True),
    ("--no-focus", "focus", False),
)

#: Report keys a `--json` consumer reads for the pane transport (AC4).
#: The reference document owns the full vocabulary; the workflow skill names
#: the ones an agent needs while driving (parity + the blocked stop).
PANE_REPORT_KEYS: tuple[str, ...] = (
    "children_driven",
    "transport",
    "pane_id",
    "pane_closed",
    "pane_cleanup",
    "retained_panes",
    "blocked_child",
    "blocked_pane",
)

#: The subset both documents must name.
PANE_REPORT_KEYS_BOTH: tuple[str, ...] = (
    "children_driven",
    "transport",
    "pane_id",
    "retained_panes",
    "blocked_child",
    "blocked_pane",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("flag,attribute,expected", PANE_FLAGS)
def test_documented_flag_is_accepted_by_the_parser(
    flag: str, attribute: str, expected: object
) -> None:
    """Every documented flag exists and sets the value the docs claim."""
    args = implement.parse_args(["drive", "SA-0TEST", flag])

    assert getattr(args, attribute) is expected, (
        f"{flag} does not set {attribute}={expected!r}: the docs would send an "
        "operator to a flag the shipped CLI does not honour"
    )


def test_pane_sessions_and_focus_defaults_match_the_docs() -> None:
    """The docs promise opt-in + `--no-focus`; the parser must agree."""
    args = implement.parse_args(["drive", "SA-0TEST"])

    assert args.pane_sessions is None, (
        "pane driving must stay opt-in (no flag and no env var means the "
        "headless path automated callers rely on)"
    )
    assert args.focus is implement.DEFAULT_PANE_FOCUS
    assert implement.DEFAULT_PANE_FOCUS is False


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_documented_flag_appears_in_both_documents(doc: Path) -> None:
    """A reader of either document can find the flag to type."""
    text = _read(doc)

    for flag, _attribute, _expected in PANE_FLAGS:
        assert flag in text, f"{flag} is not documented in {doc.name}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_opt_in_env_vars_are_documented(doc: Path) -> None:
    """The env-var opt-in and the child markers are documented by name."""
    text = _read(doc)

    for name in (
        implement.PANE_SESSIONS_ENV,
        implement.DRIVE_RECURSION_ENV,
        implement.WORKTREE_ENV,
        "HERDR_ENV",
    ):
        assert name in text, f"{name} is not documented in {doc.name}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_headless_fallback_is_documented_for_automated_callers(doc: Path) -> None:
    """The docs keep the headless default explicit for non-Herdr callers."""
    text = _read(doc)

    assert "headless" in text, f"{doc.name} never mentions the headless path"
    lowered = text.lower()
    for caller in ("downtime worker", "audit dispatch", "ci"):
        assert caller in lowered, (
            f"{doc.name} does not name {caller!r} as a caller that keeps the "
            "headless path — an automated caller must not be surprised by a "
            "pane transport"
        )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_blocked_pane_guidance_names_the_inspection_remedy(doc: Path) -> None:
    """A blocked child's pane is left open and the docs say how to read it."""
    text = _read(doc)

    assert "left open" in text, (
        f"{doc.name} does not state that a blocked child's pane is left open"
    )
    assert "🚫" in text, f"{doc.name} does not name the blocked pane's 🚫 title"
    assert "herdr pane read" in text, (
        f"{doc.name} does not print the shipped inspection remedy "
        "(`herdr pane read <pane_id>`) for a retained pane"
    )
    assert implement._pane_inspect_remedy("w1:p1") == "herdr pane read w1:p1"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_core_report_keys_are_documented(doc: Path) -> None:
    """The report vocabulary an agent needs mid-drive is documented."""
    text = _read(doc)

    for key in PANE_REPORT_KEYS_BOTH:
        assert key in text, f"report key {key!r} is not documented in {doc.name}"


def test_full_report_vocabulary_is_documented_in_the_reference() -> None:
    """The reference document describes every pane-lifecycle report field."""
    text = _read(DRIVE_DOC)

    for key in PANE_REPORT_KEYS:
        assert key in text, f"report key {key!r} is not documented in {DRIVE_DOC.name}"


@pytest.mark.parametrize("key", PANE_REPORT_KEYS)
def test_documented_report_keys_are_emitted_by_the_source(key: str) -> None:
    """Every documented report key is really written by `implement.py`."""
    source = Path(implement.__file__).read_text(encoding="utf-8")

    assert f'"{key}"' in source, (
        f"report key {key!r} is documented for the operator but never written "
        "by implement.py — the docs describe a report that does not exist"
    )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_worked_example_is_present(doc: Path) -> None:
    """The worked example (AC3) covers driving, inspection and resuming."""
    text = _read(doc)

    assert "drive " in text
    assert "--pane-sessions" in text
    assert "herdr pane read" in text
    lowered = text.lower()
    assert "re-run" in lowered, (
        f"{doc.name} does not tell the operator how to resume after "
        "intervening in a blocked child's pane"
    )
