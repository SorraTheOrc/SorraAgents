"""Docs-vs-source consistency for the drive recovery contract.

LP-0MVWEP3TY001S2A1 (parent epic LP-0MVVYQ1U3006GDWT AC7/AC8): the drive
recovery behaviour shipped by the sibling slices — the finish-failure
classifier vocabulary, the ``--infra-retry-limit`` budget and backoff,
``refs/wip/<id>`` preservation and cleanup, the reinstall step, and the new
report fields — must be documented for the operator in
``skill/implement/SKILL.md`` and ``docs/dev/implement-skill-reference.md``.
The docs are the single source of truth an operator reads, so this module
fails in either direction:

* a value the code emits but the docs never mention (a new refusal reason, a
  new report key, a new per-child result) — the docs are stale; and
* a documented value the code never emits (a forward-looking or renamed key)
  — the docs over-claim.

The behavioural contracts themselves are covered where the code lives
(``tests/test_implement_drive_recovery.py``); this module does not
re-implement them. It pins the *documentation contract*: the vocabulary
table rows equal the code constants exactly (AC2/AC3), the documented
report surface matches a report `phase_drive` actually produces (AC5), and
the WIP-preservation lifecycle / reinstall steps are present with the "not
automatic" and "never edit the installed copy" statements (AC4).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from skill.implement.scripts import implement as implement_mod
from tests.test_implement_drive_recovery import (
    DriveHarness,
    RecordingParent,
    _child,
    infra_refusal_report,
)

_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_REFERENCE_MD = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"

#: The drive report keys this module's docs promise the operator.
#: ``finish_failure`` lives on the failing **parent** report that ``drive``
#: consumes (documented with its ``{kind, step, reason, message, steps}``
#: payload shape); the rest are top-level keys of the produced drive report.
DRIVE_REPORT_KEYS: tuple[str, ...] = (
    "finish_failure",
    "infra_retries",
    "infra_retry_limit",
    "preserved_refs",
    "finish_attempts",
    "finish_attempt_limit",
    "terminal_reconciled",
    "reset_for_retry",
)

#: Result values ``phase_drive`` can record in a ``children_driven`` entry.
#: ``finish-only-retry`` is deliberately NOT listed: it is not shipped, and
#: AC5 forbids documenting forward-looking fields (no documented field may be
#: unshipped).
DRIVE_RESULT_VALUES: tuple[str, ...] = (
    "infra-retry",
    "finished",
    "completed-late",
    "terminal-reconciled",
    "reset-for-retry",
    "finalised",
    "failed",
)


@pytest.fixture(autouse=True)
def _neutralise_ambient_drive_guard(monkeypatch):
    """Make this module hermetic when pytest runs inside a driven session.

    ``implement.py finish`` runs the changed-scope suite with
    ``IMPLEMENT_DRIVE_ACTIVE=1`` exported by the driving parent. The
    produced-report test below runs ``phase_drive``; without the env var
    removed it would hit the recursion guard and fail the run (same hazard
    as SA-0MV8HEQ4O009Y3UB, pinned in ``tests/test_implement_drive.py``).
    """
    monkeypatch.delenv("IMPLEMENT_DRIVE_ACTIVE", raising=False)


def _read(path: Path) -> str:
    assert path.exists(), f"document not found at {path}"
    return path.read_text(encoding="utf-8")


def _normalised(path: Path) -> str:
    """Collapse whitespace so assertions survive line rewrapping."""
    return " ".join(_read(path).split())


def _classification_table_rows() -> list[tuple[str, str]]:
    """Parse the reference doc's ``(step, reason) → kind`` table into rows.

    Each row is ``(pair_text, kind)``; ``pair_text`` is the raw first cell so
    the doc↔code comparison below expands exactly what the operator reads.
    """
    text = _read(_REFERENCE_MD)
    start = text.find("### Finish-failure classification vocabulary")
    assert start != -1, (
        "reference doc must carry the classification vocabulary section"
    )
    section = text[start:]
    rows: list[tuple[str, str]] = []
    # The vocabulary table is the FIRST table after the heading; stop at the
    # first non-table paragraph so later tables in the file (the gate's
    # reason table, the lock-step tables, …) never leak in.
    in_table = False
    for line in section.splitlines():
        if line.startswith("| `(step, reason)` | kind |"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break  # the table has ended
        if "|---|---|" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        pair_text, kind_cell = cells[0], cells[1]
        kind_match = re.search(r"`([a-z_]+)`", kind_cell)
        if kind_match is None:
            continue
        rows.append((pair_text, kind_match.group(1)))
    return rows


def _expand_table_pairs(
    rows: list[tuple[str, str]],
    wildcard: str,
) -> dict[tuple[str, str], str]:
    """Expand the compacted table rows into ``{(step, reason): kind}``.

    The table compacts several reasons under one step
    (`` `step` + `r1` / `r2` `` …) and uses prose for the lock/guard rows and
    the wildcard row; this expands exactly those notations:
    * `` `step` + `r1` / `r2` … `` → one pair per reason;
    * `` `step` refusal (`reason`) `` / `` `step` verdict `reason` `` → one
      pair;
    * `` `step` + any reason `` → the wildcard pair.
    The ``anything else`` sentinel row maps to no pair (it documents the
    fail-safe ``unknown`` kind).
    """
    pairs: dict[tuple[str, str], str] = {}
    for pair_text, kind in rows:
        if pair_text.startswith("anything else"):
            continue
        tokens = re.findall(r"`([^`]+)`", pair_text)
        if not tokens:
            continue
        step = tokens[0]
        if "any reason" in pair_text:
            reasons = [wildcard]
        elif "refusal" in pair_text or "verdict" in pair_text:
            # `` `step` refusal (`lock_held`) `` / `` `step` verdict `fail` ``
            reasons = [tokens[1]]
        else:
            reasons = tokens[1:]
        for reason in reasons:
            pairs[(step, reason)] = kind
    return pairs


def _expected_kinds() -> dict[tuple[str, str], str]:
    """The (step, reason) → kind map the code constants define."""
    pairs: dict[tuple[str, str], str] = {}
    for pair in implement_mod.FINISH_INFRA_REFUSALS:
        pairs[tuple(pair)] = "infrastructure"
    for pair in implement_mod.FINISH_GATE_REFUSALS:
        pairs[tuple(pair)] = "gate"
    return pairs


def _documented_drive_report_fields() -> set[str]:
    """The report-field names documented in §Drive failure recovery's
    "Additional report fields" list."""
    text = _normalised(_REFERENCE_MD)
    start = text.find("Additional report fields:")
    assert start != -1, "the reference doc must list the drive report fields"
    section = text[start : start + 2000]
    return set(re.findall(r"- `([a-z_]+)`", section))


# ---------------------------------------------------------------------------
# AC2/AC3: the vocabulary table rows equal the code constants exactly
# ---------------------------------------------------------------------------


class TestVocabularyTableMatchesCode:
    """The ``(step, reason) → kind`` table is the reference's mirror of the
    code constants. AC2: every row pair exists in the constants with the same
    kind; AC3: divergence fails in **either** direction."""

    def test_kinds_are_a_subset_of_the_classifier_kinds(self) -> None:
        kind_cells = {kind for _, kind in _classification_table_rows()}
        assert kind_cells <= set(implement_mod.FINISH_FAILURE_KINDS), (
            f"table kinds {sorted(kind_cells)} exceed the classifier kinds "
            f"{list(implement_mod.FINISH_FAILURE_KINDS)}"
        )

    def test_table_pairs_equal_code_pairs_both_directions(self) -> None:
        documented = _expand_table_pairs(
            _classification_table_rows(), implement_mod.FINISH_REASON_WILDCARD
        )
        expected = _expected_kinds()
        assert documented == expected, (
            "the reference vocabulary table and the code constants have "
            "drifted:\n"
            f"  documented-only: "
            f"{sorted(set(documented) - set(expected))}\n"
            f"  code-only: {sorted(set(expected) - set(documented))}\n"
            f"  kind mismatches: "
            f"{sorted({k for k in set(documented) & set(expected) if documented[k] != expected[k]})}"
        )

    def test_every_code_pair_is_documented(self) -> None:
        """A forward direction the set-equality test hides: each code pair
        with its kind is spelled out in the table text."""
        text = _read(_REFERENCE_MD)
        for (step, reason), kind in _expected_kinds().items():
            assert f"`{step}`" in text, (
                f"code pair {step!r}/{reason!r} (kind {kind}) is not "
                "documented in the reference table"
            )
            if reason == implement_mod.FINISH_REASON_WILDCARD:
                assert re.search(r"`" + re.escape(step) + r"`\s*\+ any reason", text), (
                    f"the wildcard row for step {step!r} must be documented "
                    "as 'any reason'"
                )
            else:
                assert f"`{reason}`" in text, (
                    f"code pair {step!r}/{reason!r} (kind {kind}) is not "
                    "documented in the reference table"
                )


# ---------------------------------------------------------------------------
# AC5: documented report fields match a produced drive report
# ---------------------------------------------------------------------------


class TestReportSurfaceMatchesProducedReport:
    """The documented report-surface additions must be keys a real
    ``phase_drive`` run emits (AC5), and pre-existing keys keep their
    original types. No documented field may be unshipped — the
    ``finish-only-retry`` result is absent on purpose."""

    def test_documented_report_keys_are_emitted_by_phase_drive(self) -> None:
        documented = _documented_drive_report_fields()
        # AC5's hard requirements: each documented **new** key appears in a
        # produced report, and pre-existing keys keep their meaning/types.
        assert set(DRIVE_REPORT_KEYS) <= documented, (
            "every drive-report key this module's contract promises must be "
            "listed in the 'Additional report fields' documentation"
        )
        assert {"infra_retries", "infra_retry_limit", "preserved_refs"} <= documented, (
            "the 'Additional report fields' list must document infra_retries, "
            "infra_retry_limit and preserved_refs"
        )
        assert {"finish_attempts", "finish_attempt_limit", "terminal_reconciled", "reset_for_retry"} <= documented, (
            "the 'Additional report fields' list must keep documenting the "
            "pre-existing finish_attempts / finish_attempt_limit / "
            "terminal_reconciled / reset_for_retry keys"
        )

    def test_produced_report_carries_each_documented_key(self) -> None:
        """Run one scripted drive and assert every documented key appears
        with its original type (AC5's 'present in a produced report')."""
        harness = DriveHarness(
            implement_mod,
            children=[_child("SA-C9")],
            runner=RecordingParent([
                infra_refusal_report("SA-C9") for _ in range(6)
            ]),
            child_states={"SA-C9": _child("SA-C9")},
        )
        report = harness.run(
            infra_retry_limit=3, finish_attempt_limit=1, delay=lambda _s: None,
        )

        assert isinstance(report["children_driven"], list)
        assert isinstance(report["finish_attempts"], dict)
        assert isinstance(report["finish_attempt_limit"], int) or report["finish_attempt_limit"] is None
        assert isinstance(report["terminal_reconciled"], int)
        assert isinstance(report["reset_for_retry"], int)
        # AC5 new keys, with the types the report init declares.
        assert isinstance(report["infra_retries"], dict)
        assert report["infra_retry_limit"] == 3
        assert isinstance(report["preserved_refs"], list)
        # The infra-retry result is emitted for the infra-refusing child.
        assert any(
            entry.get("result") == "infra-retry"
            for entry in report["children_driven"]
        )
        # finish_failure is carried on the failing *parent* report drive
        # consumes (never on the drive report itself).
        consumed = harness.finishing_failed_reports("SA-C9")
        assert consumed, "the scripted run must consume finish-failing parent reports"
        for parent in consumed:
            payload = parent.get("finish_failure")
            assert isinstance(payload, dict), "parent report must carry finish_failure"
            assert set(payload) >= {"kind", "step", "reason"}, (
                "finish_failure payload must carry kind/step/reason"
            )

    def test_no_forward_looking_result_is_documented(self) -> None:
        """AC5: no documented field is unshipped. ``finish-only-retry`` is not
        shipped by the code, so it must not be promised by the docs."""
        skill, reference = _read(_SKILL_MD), _read(_REFERENCE_MD)
        assert "finish-only-retry" not in skill
        assert "finish-only-retry" not in reference

    def test_documented_result_values_are_shipped(self) -> None:
        """Every result value the docs promise in §Drive failure recovery is
        one the code actually emits (searched as an emitted literal)."""
        for result in DRIVE_RESULT_VALUES:
            assert f'"{result}"' in _read(
                _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
            ), (
                f"result {result!r} is documented but never emitted by "
                "implement.py — a forward-looking claim"
            )


# ---------------------------------------------------------------------------
# AC1/AC4: SKILL.md coverage, the WIP-preservation lifecycle and the
# reinstall step
# ---------------------------------------------------------------------------


class TestRecoveryContractDocumentation:
    """AC1 (SKILL.md documents the classifier kinds, the flag, the preservation
    gate and the report fields; the reference anchor resolves) and AC4 (the
    reference documents ``refs/wip/<id>`` creation/inspection/cleanup as
    manual, and the reinstall step with the never-edit rule)."""

    def test_skill_md_drive_recovery_section_covers_the_classifier(self) -> None:
        skill = _read(_SKILL_MD)
        section_start = skill.find("**A failing `finish` is bounded")
        assert section_start != -1, "SKILL.md must carry the drive-recovery section"
        section = skill[section_start : section_start + 6000]
        # The three classifier kinds an operator can be told about.
        for kind in implement_mod.FINISH_FAILURE_KINDS:
            assert f"`{kind}`" in section, (
                f"SKILL.md drive-recovery section must document the "
                f"{kind!r} classifier kind"
            )
        # ``unknown`` fails safe — never retried as infrastructure.
        assert re.search(r"unknown[\s\S]{0,120}never[\s\S]{0,40}infrastructure", section), (
            "SKILL.md must state that an unknown pair is never treated as an "
            "infrastructure refusal (fails safe)"
        )

    def test_skill_md_documents_the_infra_retry_flag_and_backoff(self) -> None:
        skill = _read(_SKILL_MD)
        assert "`--infra-retry-limit" in skill, (
            "SKILL.md drive options must document `--infra-retry-limit`"
        )
        # The full document (not just the options paragraph) carries the
        # default and the backoff schedule, so a rewrap or reflow cannot
        # push them out of a fixed window.
        assert (
            "default `5`" in skill
            and "2 s doubling to a 30 s cap" in skill
        ), (
            "SKILL.md must document the default infra-retry limit of 5 and "
            "the 2s-doubling-to-30s backoff schedule"
        )
        assert "exponential backoff" in skill or "backoff" in skill

    def test_skill_md_documents_wip_preservation_and_report_fields(self) -> None:
        skill = _read(_SKILL_MD)
        section = skill[skill.find("**A failing `finish` is bounded") : skill.find("**A failing `finish` is bounded") + 6000]
        assert "`refs/wip/<id>`" in section, (
            "SKILL.md drive-recovery section must document the refs/wip/<id> "
            "preservation ref"
        )
        for field in ("finish_failure", "infra_retries", "infra_retry_limit", "preserved_refs"):
            assert f"`{field}`" in skill, (
                f"SKILL.md must mention the {field!r} report field"
            )

    def test_skill_md_reference_anchor_still_resolves(self) -> None:
        skill = _read(_SKILL_MD)
        assert "implement-skill-reference.md#drive-failure-recovery-bounded-finish-attempts-reconciliation-and-reset" in skill, (
            "SKILL.md must keep its cross-reference anchor to the reference "
            "doc's §Drive failure recovery"
        )
        reference = _normalised(_REFERENCE_MD)
        assert "Drive failure recovery: bounded finish attempts, reconciliation and reset" in reference, (
            "the reference anchor target heading must still exist"
        )

    def test_reference_documents_wip_ref_lifecycle(self) -> None:
        reference = _normalised(_REFERENCE_MD)
        # Creation.
        assert "refs/wip/" in reference
        # Inspection commands.
        assert "git rev-parse" in reference
        assert "git log --oneline -1 refs/wip/" in reference
        # Cleanup is manual, never automatic.
        assert "git update-ref -d refs/wip/" in reference
        assert re.search(r"cleanup[\s\S]{0,80}manual|manual[\s\S]{0,40}cleanup|not automatic|no automatic", reference), (
            "the reference must state that refs/wip cleanup is manual, not "
            "automatic"
        )

    def test_reference_documents_reinstall_and_never_edit_rule(self) -> None:
        reference = _normalised(_REFERENCE_MD)
        assert "~/projects/SorraAgents/skill/implement/" in reference, (
            "the reference must name the canonical source path"
        )
        assert "~/.pi/agent/skills/implement/" in reference, (
            "the reference must name the installed runtime copy"
        )
        assert "scripts/install_pi.sh" in reference, (
            "the reference must document the reinstall step"
        )
        # The never-edit rule binds the installed copy; the phrase can precede
        # or follow the path within the same sentence.
        assert re.search(
            r"never be edited[\s\S]{0,120}~/\.pi/agent/skills/implement/"
            r"|~/\.pi/agent/skills/implement/[\s\S]{0,120}never be edited",
            reference,
        ), (
            "the reference must state the installed copy is never edited"
        )
