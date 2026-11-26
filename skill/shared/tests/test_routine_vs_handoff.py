"""Routine questions vs genuine producer handoff (SA-0MWUNYMLS003B52S).

The routine-vs-handoff rule is **agent judgement expressed in skill prose**, so
there is no runtime branch to unit-test. This file therefore combines the two
tests the work item specifies:

1. **Documentation contract** — the three consuming skill docs must carry the
   distinction. This is a deliberate, justified exception to the
   "no source-text-grep tests" anti-pattern in
   ``skill/shared/test-writing-guidelines.md``: the rule *is* the prose, and
   without a guard a later edit can silently restore the old
   flag-everything behaviour. The assertions are structural (the §4 decision
   matrix must keep a routine row that continues and a genuine row that hands
   off) rather than a pinned copy of the wording.
2. **Behavioural worklog test** — against a real ephemeral ``wl`` store, prove
   the flag semantics the prose prescribes: recording a routine question and
   its resolved assumption in the Appendix leaves ``needsProducerReview``
   false, while a genuine handoff (``wl reviewed <id> true``) sets it.

Rationale for the rule lives in ``docs/dev/routine-vs-producer-handoff.md``.
Related work items: SA-0MWUNYMLS003B52S (this change), WL-0MWTVPL1A005KB4D
(coordination), WL-0MWTAD1KM0081UUD (parent investigation, scope item 4c).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = _REPO_ROOT / "skill"

_INTERVIEW = _SKILLS_ROOT / "interview" / "SKILL.md"
_PLAN = _SKILLS_ROOT / "plan" / "SKILL.md"
_INTAKE = _SKILLS_ROOT / "intake" / "SKILL.md"
_RATIONALE_DOC = _REPO_ROOT / "docs" / "dev" / "routine-vs-producer-handoff.md"

_HANDOFF_MARKER = "wl reviewed"
_ROUTINE_WORD = "routine"


# ---------------------------------------------------------------------------
# Markdown helpers — section extraction and the §4 decision matrix
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.is_file(), f"expected skill doc at {path}"
    return path.read_text(encoding="utf-8")


def _section(text: str, heading_pattern: str) -> str:
    """Return the body of the first heading matching ``heading_pattern``.

    The body runs until the next heading at the same or a shallower level, so a
    subsection inside the match is returned with it.
    """
    match = re.search(heading_pattern, text, flags=re.MULTILINE | re.IGNORECASE)
    assert match, f"heading matching {heading_pattern!r} not found"
    level = len(match.group(1))
    body_start = match.end()
    next_heading = re.compile(
        rf"^(#{{1,{level}}})\s", flags=re.MULTILINE
    ).search(text, body_start)
    body_end = next_heading.start() if next_heading else len(text)
    return text[body_start:body_end]


def _table_rows(section: str) -> list[list[str]]:
    """Return the data rows (pipe-separated cells) of the first table."""
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        # Skip the header separator (--- style) and the header itself is kept.
        if all(set(c) <= set("-: ") and c for c in cells):
            continue
        rows.append(cells)
    return rows


def _row_for(rows: list[list[str]], keyword: str) -> list[str]:
    matches = [r for r in rows if r and keyword.lower() in r[0].lower()]
    assert len(matches) == 1, (
        f"expected exactly one decision-matrix row starting with {keyword!r}; "
        f"got {matches!r}"
    )
    return matches[0]


def _paragraph(text: str, marker: str) -> str:
    """Return the first blank-line-separated block containing ``marker``."""
    blocks = re.split(r"\n\s*\n", text)
    matches = [b for b in blocks if marker in b]
    assert matches, f"no paragraph containing {marker!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. Documentation contract
# ---------------------------------------------------------------------------


class TestInterviewContractDocumentsDistinction:
    """``skill/interview/SKILL.md`` §4 defines routine vs genuine handoff."""

    @pytest.fixture(scope="class")
    def handoff(self) -> str:
        return _section(_read(_INTERVIEW), r"^(#{2,4})\s*.*Producer-review handoff")

    def test_names_both_categories(self, handoff):
        lowered = handoff.lower()
        assert _ROUTINE_WORD in lowered, (
            "§4 must name the 'routine question' category so agents know when "
            "NOT to set the hold"
        )
        assert "genuine" in lowered, (
            "§4 must name the 'genuine producer handoff' category"
        )

    def test_routine_path_records_appendix_and_continues(self, handoff):
        routine_row = _row_for(_table_rows(handoff), _ROUTINE_WORD)
        action = " ".join(routine_row[1:]).lower()
        assert "appendix" in action, (
            "the routine row must say the assumption is recorded in the Appendix"
        )
        assert "continue" in action, (
            "the routine row must say the agent continues rather than stopping"
        )
        assert _HANDOFF_MARKER not in action, (
            "the routine row must not invoke the producer-review flag"
        )

    def test_genuine_path_uses_the_review_flag_and_stops(self, handoff):
        genuine_row = _row_for(_table_rows(handoff), "genuine")
        action = " ".join(genuine_row[1:]).lower()
        assert _HANDOFF_MARKER in action, (
            "the genuine-handoff row must call `wl reviewed <id> true`"
        )
        assert "stop" in action, (
            "the genuine-handoff row must say the agent stops"
        )

    def test_links_the_rationale_page(self, handoff):
        assert "routine-vs-producer-handoff.md" in handoff, (
            "§4 must cross-link docs/dev/routine-vs-producer-handoff.md so the "
            "rationale is discoverable"
        )


class TestConsumingSkillsReferenceTheDistinction:
    """`plan` and `intake` defer to the shared contract, not their own rule."""

    def test_plan_producer_review_section_is_routine_aware(self):
        plan = _read(_PLAN)
        producer_review = _paragraph(plan, "**Producer review:**")
        lowered = producer_review.lower()
        assert _ROUTINE_WORD in lowered, (
            "plan's Producer review guidance must mention routine questions"
        )
        assert "interview/SKILL.md" in producer_review, (
            "plan's Producer review guidance must reference the shared contract"
        )

    def test_plan_pausing_bullet_stays_handoff_only(self):
        plan = _read(_PLAN)
        bullet = [
            line
            for line in plan.splitlines()
            if "Pausing for producer input" in line
        ]
        assert len(bullet) == 1, "expected exactly one 'Pausing for producer input' bullet"
        lowered = bullet[0].lower()
        assert "handoff-only" in lowered, "the bullet must stay handoff-only"
        assert _ROUTINE_WORD in lowered, (
            "the bullet must exclude routine questions from the handoff"
        )

    def test_intake_interview_section_is_routine_aware(self):
        intake = _read(_INTAKE)
        interview = _section(intake, r"^(#{2,4})\s*.*\bInterview\b")
        lowered = interview.lower()
        assert _ROUTINE_WORD in lowered, (
            "intake's Interview section must mention routine questions"
        )
        assert "interview/SKILL.md" in interview, (
            "intake's Interview section must reference the shared contract"
        )


class TestRationalePageExists:
    """AC4 — the rationale and worked example live in docs/dev."""

    def test_rationale_records_evidence_and_both_paths(self):
        doc = _read(_RATIONALE_DOC)
        lowered = doc.lower()
        assert "16" in doc and "18" in doc, (
            "the rationale must cite the 16-of-18 question-loop provenance figure"
        )
        assert "question-loop" in lowered or "question loop" in lowered
        assert "appendix" in lowered, "worked example (a) must show the Appendix record"
        assert _HANDOFF_MARKER in lowered, "worked example (b) must show the handoff"
        assert "no dispatcher" in lowered or "no gate change" in lowered, (
            "the rationale must state that no dispatcher or gate change is made"
        )


# ---------------------------------------------------------------------------
# 2. Behavioural worklog test — the flag semantics the prose prescribes
# ---------------------------------------------------------------------------


def _wl(store: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["wl", "--worklog-dir", str(store), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _wl_json(store: Path, *args: str) -> dict:
    proc = _wl(store, *args)
    assert proc.returncode == 0, (
        f"wl {' '.join(args)} failed: {proc.stdout} {proc.stderr}"
    )
    return json.loads(proc.stdout)


def _wl_ok(store: Path, *args: str) -> None:
    """Run a ``wl`` command that prints human text even with ``--json``."""
    proc = _wl(store, *args)
    assert proc.returncode == 0, (
        f"wl {' '.join(args)} failed: {proc.stdout} {proc.stderr}"
    )


def _create_item(store: Path, title: str) -> str:
    data = _wl_json(
        store,
        "create",
        "--title",
        title,
        "--description",
        "## Acceptance Criteria\n- Placeholder AC\n",
        "--issue-type",
        "task",
        "--priority",
        "medium",
        "--json",
    )
    return data["workItem"]["id"]


def _flag(store: Path, item_id: str) -> bool:
    work_item = _wl_json(store, "show", item_id, "--json")["workItem"]
    return bool(work_item.get("needsProducerReview"))


@pytest.fixture
def wl_store(tmp_path) -> Path:
    """A temporary, initialized worklog store (no git, no auto-sync)."""
    store = tmp_path / "store" / ".worklog"
    store.parent.mkdir(parents=True)
    proc = subprocess.run(
        [
            "wl", "init",
            "--worklog-dir", str(store),
            "--project-name", "ROUTINE-HANDOFF",
            "--prefix", "RH",
            "--auto-export", "no",
            "--auto-sync", "no",
            "--json",
        ],
        input="\n",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"wl init failed: {proc.stdout} {proc.stderr}"
    return store


class TestRoutineQuestionDoesNotSetTheHold:
    def test_appendix_record_only_leaves_flag_false(self, wl_store):
        item = _create_item(wl_store, "Routine question is self-resolved")
        assert _flag(wl_store, item) is False, "a fresh item must not be flagged"

        # Routine path per the interview contract §4: research/assume, record in
        # the Appendix, continue. No `wl reviewed` call is made.
        appendix = (
            "- Q: How should the prose rule be tested? — Answer (agent, research): "
            "a doc-contract test plus a behavioural flag test. Source: repo search."
        )
        _wl_json(
            wl_store,
            "comment",
            "add",
            item,
            "--comment",
            f"## Appendix — clarifying questions & answers\n\n{appendix}\n",
            "--author",
            "interview",
            "--json",
        )

        assert _flag(wl_store, item) is False, (
            "recording a routine question and its resolved assumption in the "
            "Appendix must NOT set needsProducerReview"
        )


class TestGenuineHandoffSetsTheHold:
    def test_wl_reviewed_true_sets_flag(self, wl_store):
        item = _create_item(wl_store, "Genuine producer decision required")
        assert _flag(wl_store, item) is False

        _wl_ok(wl_store, "reviewed", item, "true")

        assert _flag(wl_store, item) is True, (
            "a genuine producer handoff (`wl reviewed <id> true`) must set "
            "needsProducerReview"
        )

    def test_flag_can_be_cleared_once_the_producer_answers(self, wl_store):
        item = _create_item(wl_store, "Producer answered the handoff")
        _wl_ok(wl_store, "reviewed", item, "true")
        assert _flag(wl_store, item) is True

        _wl_ok(wl_store, "reviewed", item, "false")
        assert _flag(wl_store, item) is False, (
            "the hold is released once the producer's answer is recorded"
        )
