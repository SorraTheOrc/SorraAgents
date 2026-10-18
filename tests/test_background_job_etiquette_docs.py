"""Tests for the background-job etiquette docs rollout (SA-0MVDP5JTK009HWOT).

Asserts the observable deliverables of the rollout: the canonical shared doc
exists, documents the pattern and the deprecation of fixed ``sleep N``
polling, and every skill/location that launches or waits on background jobs
references it. The legacy bare-``sleep N`` polling recommendation must not
remain in any skill doc (AC3).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SHARED_DOC = REPO_ROOT / "skill" / "shared" / "background-job-etiquette.md"
HELPER = REPO_ROOT / "skill" / "shared" / "wait_for_job.py"

#: Locations that launch or wait on background jobs and must reference the
#: shared etiquette section (child AC2).
REQUIRED_REFS = [
    REPO_ROOT / "skill" / "implement" / "SKILL.md",
    REPO_ROOT / "skill" / "audit" / "SKILL.md",
    REPO_ROOT / "docs" / "dev" / "audit-skill-reference.md",
    REPO_ROOT / "skill" / "ship" / "SKILL.md",
    REPO_ROOT / "skill" / "plan" / "SKILL.md",
    REPO_ROOT / "skill" / "test" / "SKILL.md",
    REPO_ROOT / "AGENTS_GLOBAL.md",
]

#: Skills/docs that historically recommended fixed-length polling loops and
#: must not still recommend a bare ``sleep N`` background poll (AC3).
LEGACY_POLL_DOCS = [
    REPO_ROOT / "docs" / "dev" / "audit-skill-reference.md",
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestSharedEtiquetteDoc:
    """AC1 — the canonical shared doc exists and defines the pattern."""

    def test_shared_doc_exists(self):
        assert SHARED_DOC.exists(), "shared background-job-etiquette.md must exist"

    def test_documents_launch_prepare_wait_collect_pattern(self):
        text = _text(SHARED_DOC)
        assert "launch → prepare other work → one completion-driven wait" in text
        assert "Collect" in text or "collect" in text

    def test_references_wait_for_job_helper(self):
        text = _text(SHARED_DOC)
        assert "wait_for_job.py" in text
        # The referenced helper must actually exist.
        assert HELPER.exists()

    def test_deprecates_fixed_sleep_polling(self):
        text = _text(SHARED_DOC)
        assert "Deprecated" in text or "deprecated" in text
        assert "sleep" in text.lower()
        assert "model round-trip" in text or "round-trip" in text

    def test_work_item_traceability(self):
        # The doc names the delivering work items (parent + this rollout).
        text = _text(SHARED_DOC)
        assert "SA-0MVCX570P003VLBK" in text
        assert "SA-0MVDP5JTK009HWOT" in text


class TestRolloutReferences:
    """AC2 — every listed skill/location references the shared section."""

    def test_each_required_file_references_shared_doc(self):
        missing = [
            str(p.relative_to(REPO_ROOT))
            for p in REQUIRED_REFS
            if not p.exists() or "background-job-etiquette" not in _text(p)
        ]
        assert not missing, (
            "files must reference skill/shared/background-job-etiquette.md: "
            + ", ".join(missing)
        )

    def test_each_required_file_references_the_helper(self):
        missing = [
            str(p.relative_to(REPO_ROOT))
            for p in REQUIRED_REFS
            if "wait_for_job" not in _text(p)
        ]
        assert not missing, (
            "files must reference the wait_for_job.py helper: "
            + ", ".join(missing)
        )


class TestNoLegacySleepPolling:
    """AC3 — no skill doc still recommends a bare fixed-length sleep poll."""

    def test_legacy_docs_do_not_recommend_fixed_poll_loop(self):
        offenders = []
        for path in LEGACY_POLL_DOCS:
            text = _text(path)
            # A bare recommendation of fixed-interval polling (e.g. "report
            # progress every 3 minutes" / "sleep 900; grep") must be gone.
            if "every 3 minutes" in text or "sleep 900; grep" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert not offenders, (
            "legacy fixed-length polling recommendations remain: "
            + ", ".join(offenders)
        )

    def test_no_skill_doc_recommends_bare_sleep_n_poll(self):
        offenders = []
        for md in (REPO_ROOT / "skill").glob("*/SKILL.md"):
            text = md.read_text(encoding="utf-8", errors="replace")
            # Only flag recommended patterns (a code block / sentence that
            # *endorses* sleeping), not the deprecation warnings themselves.
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith((">", "```", "#")):
                    if "sleep 900" in stripped or "sleep 30" in stripped:
                        offenders.append(f"{md.relative_to(REPO_ROOT)}: {stripped}")
        assert not offenders, (
            "skill docs recommend bare fixed-length sleep polling:\n"
            + "\n".join(offenders)
        )
