"""Doc-contract tests: the driver owns the per-child lock during a drive.

Verifies SA-0MW7OHTH4007ZJP2 (child of SA-0MVXWMVTH007MNJR): the implement
skill and its reference doc must describe the driven-session lock holder
accurately — *who* holds the lock (the long-lived ``phase_drive`` driver, not
the short-lived ``start`` subprocess inside ``parent``), *for how long* (from
immediately before the session spawn until the spawner returns), *how* the
ownership token reaches the spawned session, the release paths (a ``finally``
around the spawn, so success, non-zero exit and timeout all release), and the
accepted residual window when the driver dies while a spawned session lives.

The deliverable is documentation, so these tests assert the documented
contract (the guidance itself), in the established style of
``tests/test_implement_skill_session_per_child_doc.py``.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_DOC = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_REFERENCE_DOC = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"

_SKILL_STEP_HEADING = "6.1. Parent recursion (epic/parent items only)"
_REFERENCE_SECTION_HEADING = "## Per-child implementation lock"
_REFERENCE_NEXT_HEADING = "\n## Herdr session boundary helper"


def _normalise(text: str) -> str:
    """Collapse whitespace so prose reflow never breaks an assertion."""
    return " ".join(text.split())


def _read(path: Path) -> str:
    assert path.exists(), f"File not found: {path}"
    return path.read_text(encoding="utf-8")


def _skill_step() -> str:
    content = _read(_SKILL_DOC)
    start = content.find(_SKILL_STEP_HEADING)
    assert start != -1, f"{_SKILL_DOC.name} must contain {_SKILL_STEP_HEADING!r}"
    end = content.find("\n7. Automated self-review", start)
    assert end != -1 and end > start, "Step 6.1 must precede step 7"
    return _normalise(content[start:end])


def _reference_section() -> str:
    content = _read(_REFERENCE_DOC)
    start = content.find(_REFERENCE_SECTION_HEADING)
    assert start != -1, (
        f"{_REFERENCE_DOC.name} must contain {_REFERENCE_SECTION_HEADING!r}"
    )
    end = content.find(_REFERENCE_NEXT_HEADING, start)
    assert end != -1 and end > start, (
        "the per-child lock section must precede the herdr helper section"
    )
    return _normalise(content[start:end])


class TestDrivenLockHolderDocumented:
    """AC1–AC2: the driver is named as the holder for the session's lifetime."""

    def test_skill_step_names_the_driver_as_holder(self) -> None:
        step = _skill_step()
        assert "driver" in step.lower()
        assert "per-child lock" in step.lower()

    def test_skill_step_states_the_session_lifetime_hold(self) -> None:
        step = _skill_step().lower()
        assert "whole lifetime" in step or "whole life" in step

    def test_reference_names_the_driver_process_as_holder(self) -> None:
        section = _reference_section()
        assert "`phase_drive`" in section
        assert "driver" in section.lower()

    def test_reference_states_the_session_lifetime_hold(self) -> None:
        section = _reference_section().lower()
        assert "for the session's whole lifetime" in section

    def test_reference_contrasts_the_short_lived_start_subprocess(self) -> None:
        section = _reference_section()
        assert "`start`" in section
        assert "short-lived" in section

    def test_reference_states_when_the_hold_begins_and_ends(self) -> None:
        section = _reference_section()
        assert "before the session is spawned" in section
        assert "spawner returns" in section


class TestDrivenLockTokenPropagation:
    """AC3: the token reaches the session; ownership stays token-based."""

    def test_reference_names_the_acquire_and_register_calls(self) -> None:
        section = _reference_section()
        assert "`_new_child_lock(child_id)`" in section
        assert "`_set_child_lock`" in section

    def test_reference_names_the_propagation_into_the_session_env(self) -> None:
        section = _reference_section()
        assert "`_child_session_env`" in section
        assert "`IMPLEMENT_CHILD_LOCK_TOKEN`" in section

    def test_reference_keeps_ownership_token_based(self) -> None:
        section = _reference_section()
        assert "never inferred from pid equality" in section


class TestDrivenLockReleasePaths:
    """AC4: every spawner outcome releases; a live foreign owner is refused."""

    def test_reference_documents_the_finally_release(self) -> None:
        section = _reference_section()
        assert "`finally`" in section

    def test_reference_names_every_spawner_outcome(self) -> None:
        section = _reference_section()
        assert "non-zero exit" in section
        assert "timeout" in section

    def test_reference_documents_the_skipped_held_refusal(self) -> None:
        section = _reference_section()
        assert "skipped-held" in section
        assert "held_by" in section
        assert "held_children" in section

    def test_reference_documents_the_residual_driver_death_window(self) -> None:
        section = _reference_section().lower()
        assert "residual window" in section
        assert "killed" in section
        assert "option b" in section
