"""Docs-vs-source consistency for the no-op finish contract.

SA-0MW74IL6B001AMQN (parent SA-0MVYYL7X9008MGA1): an agent that meets a
clean, commit-less worktree learns the contract from
``skill/implement/SKILL.md`` *before* it hits the refusal. The docs must state
the default refusal, the ``--verification-only`` opt-in (including
``start --verification-only`` recording the signal in the worktree state) and
how the drive/parent classification treats a work-less child.

The documents are read as text on purpose: the contract under test *is* the
wording an agent sees. Source constants that the wording must describe are
imported so a renamed verdict fails here instead of silently leaving the
guidance stale.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

SKILL_MD = REPO_ROOT / "skill" / "implement" / "SKILL.md"
REFERENCE = REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"

#: The reason recorded by ``finish`` when it refuses a no-commit completion.
NO_COMMIT_REASON = "no_commit_no_signal"
#: The explicit opt-in that keeps the verification-only path finalisable.
VERIFICATION_ONLY_FLAG = "--verification-only"
#: The drive/parent verdict for a child with no attributable work.
NOT_FINALISABLE_ACTION = "not-finalisable"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_skill_md_documents_the_default_no_commit_refusal() -> None:
    """An agent must recognise the refusal and know why it happened."""
    skill = _read(SKILL_MD)

    assert NO_COMMIT_REASON in skill, (
        "SKILL.md must name the default no-commit refusal reason "
        f"({NO_COMMIT_REASON!r}) so an agent recognises the refusal and its "
        "reported steps.commit.reason"
    )
    assert "refus" in skill.lower(), (
        "SKILL.md must state that a clean, commit-less worktree is refused by "
        "default rather than silently finalised"
    )


def test_skill_md_documents_the_verification_only_flag_and_state_signal() -> None:
    """The opt-in is documented for both invocation styles that carry it."""
    skill = _read(SKILL_MD)

    assert VERIFICATION_ONLY_FLAG in skill, (
        f"SKILL.md must document the {VERIFICATION_ONLY_FLAG!r} opt-in"
    )
    assert f"start <id> {VERIFICATION_ONLY_FLAG}" in skill, (
        "SKILL.md must document that `start <id> --verification-only` records "
        "the signal in the worktree state"
    )
    assert f"finish <id> {VERIFICATION_ONLY_FLAG}" in skill, (
        "SKILL.md must document the `finish <id> --verification-only` form"
    )
    assert implement.STATE_FILE_NAME in skill, (
        "SKILL.md must name the state file that records the verification-only "
        f"signal ({implement.STATE_FILE_NAME})"
    )


def test_skill_md_documents_work_less_worktree_guidance() -> None:
    """A clean, commit-less worktree is unfinished work, not a completion."""
    skill = _read(SKILL_MD)

    assert "re-enter" in skill.lower() or "produce the work" in skill.lower(), (
        "SKILL.md must tell the agent to re-enter the worktree and produce the "
        "work the acceptance criteria require rather than treating the clean "
        "tree as finished"
    )
    assert "unfinished" in skill.lower(), (
        "SKILL.md must describe a clean, commit-less worktree as unfinished "
        "work"
    )


def test_skill_md_documents_the_drive_classification_verdict() -> None:
    """drive/phase_parent must not present a work-less child as finishable."""
    skill = _read(SKILL_MD)

    assert NOT_FINALISABLE_ACTION in skill, (
        "SKILL.md must document the drive/parent "
        f"{NOT_FINALISABLE_ACTION!r} verdict for a work-less child"
    )
    assert implement.CHILD_VERIFICATION_ONLY in skill, (
        "SKILL.md must document the verification-only worktree classification"
    )
    assert implement.CHILD_NO_WORK in skill, (
        "SKILL.md must document the no-work worktree classification"
    )


def test_reference_documents_the_no_commit_refusal_and_classification() -> None:
    """The deep reference must keep the refusal contract and its verdict."""
    reference = _read(REFERENCE)

    assert "### No-commit finalisation: refusal by default" in reference, (
        "the implement-skill reference lost its no-commit finalisation section"
    )
    assert NO_COMMIT_REASON in reference, (
        f"the reference must name {NO_COMMIT_REASON!r} as the refusal reason"
    )
    assert NOT_FINALISABLE_ACTION in reference, (
        "the reference must document the drive/parent "
        f"{NOT_FINALISABLE_ACTION!r} verdict for a work-less child"
    )


def test_reference_names_the_verification_only_state_signal() -> None:
    """The reference must keep the state-file signal that also opts in."""
    reference = _read(REFERENCE)

    assert "verification_only" in reference, (
        "the reference must document the `verification_only` state-file key "
        "that `start --verification-only` records"
    )
    assert implement.STATE_FILE_NAME in reference, (
        "the reference must name the state file that carries the signal "
        f"({implement.STATE_FILE_NAME})"
    )
