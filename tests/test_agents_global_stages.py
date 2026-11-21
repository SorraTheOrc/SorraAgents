"""Docs assertion: AGENTS_GLOBAL.md stage list matches the canonical wl stage set.

Regression test for SA-0MWK3M7DQ009PV42 / SA-0MUYHGUKY007SEW4.  The canonical
`wl` stage set is ``idea``, ``intake_complete``, ``plan_complete``,
``in_review``, ``done`` — ``in_progress`` is a **status**, not a stage.

If this test fails, someone has re-introduced a retired stage name into the
agent guidance.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_GLOBAL = _REPO_ROOT / "AGENTS_GLOBAL.md"

#: The canonical stage set (source of truth — matches `wl` schema).
_CANONICAL_STAGES = {"idea", "intake_complete", "plan_complete", "in_review", "done"}

#: Regex that matches an inline parenthesised list of stage names, e.g.
#: ``(idea, intake_complete, plan_complete, in_review, done)``.
_STAGE_LIST_RE = re.compile(
    r"\(\s*([a-z_]+(?:,\s*[a-z_]+)*)\s*\)"
)


def _extract_stages_from_stage_line() -> list[str]:
    """Return the stage names from the *stage* axis bullet in AGENTS_GLOBAL.md."""
    text = _AGENTS_GLOBAL.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- **`stage`**"):
            for match in _STAGE_LIST_RE.finditer(line):
                return [s.strip() for s in match.group(1).split(",")]
    return []  # pragma: no cover — always found in a well-formed file


def _extract_in_progress_mentions_outside_fences() -> list[tuple[int, str]]:
    """Return (line_no, line) for every *in_progress* mention outside code
    blocks — to check that it is only mentioned as a *status*."""
    text = _AGENTS_GLOBAL.read_text(encoding="utf-8")
    in_fence = False
    result: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and "in_progress" in line:
            result.append((line_no, line))
    return result


class TestAgentsGlobalStageList:
    """Validate that AGENTS_GLOBAL.md's stage list matches the wl schema."""

    def test_stage_list_contains_canonical_stages(self) -> None:
        """AC1: the stage bullet lists exactly the canonical stages."""
        found = _extract_stages_from_stage_line()
        found_set = set(found)
        assert found_set == _CANONICAL_STAGES, (
            f"Stage list mismatch — expected {_CANONICAL_STAGES}, "
            f"found {found_set} (line: {found!r})"
        )

    def test_stage_list_does_not_include_retired_in_progress(self) -> None:
        """AC1: the retired in_progress stage must not appear in the stage list."""
        found = _extract_stages_from_stage_line()
        assert "in_progress" not in found, (
            "in_progress is a *status*, never a *stage* — it must not appear "
            "in the stage list"
        )

    def test_in_progress_mentioned_only_as_status(self) -> None:
        """AC2: outside code blocks, every mention of in_progress must make it
        clear it is a *status*, never a *stage*."""
        mentions = _extract_in_progress_mentions_outside_fences()
        for line_no, line in mentions:
            stripped = line.strip().lower()
            # --status in_progress is a status usage — allowed.
            if "--status" in stripped or "--status in_progress" in stripped:
                continue
            # Explicit clarification — allowed.
            if "is a status" in stripped or "is a *status*" in stripped:
                continue
            # --status in-progress (hyphen) — allowed (same status).
            if "--status" in stripped:
                continue
            pytest.fail(
                f"Line {line_no} mentions 'in_progress' but does not clearly "
                f"mark it as a status (not a stage): {line.strip()!r}"
            )
