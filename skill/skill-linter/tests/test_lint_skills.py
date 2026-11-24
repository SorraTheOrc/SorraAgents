"""Unit tests for the skill-linter deterministic detector core.

Work item: SA-0MWGAYPMX002Z0VC — "Test duplicate & safe-fix detection in
lint_skills.py" (child of SA-0MWFP1E9P0027EOE).

The tests pin the scanner contract that
``skill/skill-linter/scripts/lint_skills.py`` implements, before the sibling
children build the CLI, ``--apply`` machinery and SKILL.md on top of it:

- AC1 exact-duplicate content within one SKILL.md (one finding, every span)
- AC2 identical blocks across two SKILL.md files (paths, spans, text)
- AC3 SKILL.md vs AGENTS.md / AGENTS_GLOBAL.md cross-file duplication
- AC4 non-essential work-item ID / workflow-comment detection, stripping and
  allow-list preservation
- AC5 fix-class classification: safe (auto-appliable) vs recommendation-only

All fixtures are in-memory documents; nothing on disk is touched.
"""

from __future__ import annotations

import json

import lint_skills

#: A three-line block that clears MIN_DUPLICATE_LINES and MIN_DUPLICATE_CHARS.
DUP_BLOCK = (
    "The linter scans SKILL.md files for repeated blocks of prose.",
    "It normalises whitespace before comparing candidate windows.",
    "Every finding reports the file path and the exact line span.",
)

DUP_TEXT = "\n".join(DUP_BLOCK)

WORK_ITEM_ID = "SA-0MWGAYPMX002Z0VC"
SESSION_ID = "01a20d32-86b2-7622-ab9e-1b7288db91fe"
SESSION_PATH = "/home/operator/.pi/agent/sessions/session.jsonl"


def _doc(*lines: str) -> str:
    return "\n".join(lines)


def _path_of(finding: lint_skills.Finding) -> list[str]:
    return [span.path for span in finding.spans]


# ── AC1: duplicate content within one file ────────────────────────────────


class TestDuplicatesWithinOneFile:
    def test_repeated_block_reported_once_with_all_instance_spans(self):
        path = "skill/alpha/SKILL.md"
        text = _doc(
            "# Alpha skill",  # 1
            "",  # 2
            *DUP_BLOCK,  # 3-5
            "",  # 6
            "A separator that differs between the two copies.",  # 7
            "",  # 8
            *DUP_BLOCK,  # 9-11
        )

        findings = lint_skills.find_duplicates({path: text})

        assert len(findings) == 1
        finding = findings[0]
        assert finding.kind == lint_skills.KIND_DUPLICATE
        assert finding.fix_class == lint_skills.FIX_SAFE
        assert [(span.start_line, span.end_line) for span in finding.spans] == [
            (3, 5),
            (9, 11),
        ]
        assert _path_of(finding) == [path, path]
        assert finding.text == DUP_TEXT
        assert [span.text for span in finding.spans] == [DUP_TEXT, DUP_TEXT]
        assert finding.rationale

    def test_indentation_differences_do_not_hide_a_duplicate(self):
        path = "skill/alpha/SKILL.md"
        indented = tuple(f"    {line}" for line in DUP_BLOCK)
        text = _doc("# Alpha skill", "", *DUP_BLOCK, "", *indented)

        findings = lint_skills.find_duplicates({path: text})

        assert len(findings) == 1
        spans = findings[0].spans
        assert [(span.start_line, span.end_line) for span in spans] == [(3, 5), (7, 9)]
        # Reported text is the original text, not the normalised comparison key.
        assert spans[0].text == DUP_TEXT
        assert spans[1].text == "\n".join(indented)

    def test_block_below_minimum_size_is_not_a_duplicate(self):
        text = _doc(
            "first short line",
            "second short line",
            "",
            "first short line",
            "second short line",
        )

        assert lint_skills.find_duplicates({"skill/alpha/SKILL.md": text}) == []


# ── AC2: duplicate content across two files ───────────────────────────────


class TestDuplicatesAcrossFiles:
    def test_identical_block_across_two_skill_files_reports_both_instances(self):
        alpha = "skill/alpha/SKILL.md"
        beta = "skill/beta/SKILL.md"
        documents = {
            alpha: _doc(
                "# Alpha",  # 1
                "",  # 2
                "Alpha-only introduction.",  # 3
                "",  # 4
                *DUP_BLOCK,  # 5-7
                "",  # 8
                "Alpha-only tail.",  # 9
            ),
            beta: _doc(
                "# Beta",  # 1
                "",  # 2
                *DUP_BLOCK,  # 3-5
            ),
        }

        findings = lint_skills.find_duplicates(documents)

        assert len(findings) == 1
        finding = findings[0]
        assert _path_of(finding) == [alpha, beta]
        assert [(span.start_line, span.end_line) for span in finding.spans] == [
            (5, 7),
            (3, 5),
        ]
        assert [span.text for span in finding.spans] == [DUP_TEXT, DUP_TEXT]
        assert finding.text == DUP_TEXT


# ── AC3: SKILL.md vs AGENTS.md / AGENTS_GLOBAL.md ─────────────────────────


class TestDuplicatesAgainstAgentsDocs:
    def test_block_shared_by_skill_and_agents_docs_reports_every_file(self):
        documents = {
            "AGENTS.md": _doc("# Project AGENTS", "", *DUP_BLOCK),
            "AGENTS_GLOBAL.md": _doc("# Global AGENTS", "", *DUP_BLOCK),
            "skill/alpha/SKILL.md": _doc("# Alpha", "", *DUP_BLOCK),
        }

        findings = lint_skills.find_duplicates(documents)

        assert len(findings) == 1
        finding = findings[0]
        assert _path_of(finding) == [
            "AGENTS.md",
            "AGENTS_GLOBAL.md",
            "skill/alpha/SKILL.md",
        ]
        assert all((span.start_line, span.end_line) == (3, 5) for span in finding.spans)
        assert finding.text == DUP_TEXT


# ── AC4: work-item IDs and workflow-comment noise ─────────────────────────


class TestNonEssentialIds:
    def test_work_item_id_is_reported_as_safe_with_its_line_span(self):
        path = "skill/alpha/SKILL.md"
        line = f"Tracking note for {WORK_ITEM_ID}."
        findings = lint_skills.find_nonessential_ids(
            {path: _doc("# Alpha", "", line, "Keep this.")}
        )

        assert len(findings) == 1
        finding = findings[0]
        assert finding.kind == lint_skills.KIND_NONESSENTIAL_ID
        assert finding.fix_class == lint_skills.FIX_SAFE
        assert finding.text == WORK_ITEM_ID
        assert finding.spans[0].path == path
        assert (finding.spans[0].start_line, finding.spans[0].end_line) == (3, 3)
        assert finding.spans[0].text == line

    def test_allow_listed_ids_are_preserved(self):
        documents = {"skill/alpha/SKILL.md": f"Guarded by {WORK_ITEM_ID}."}

        assert lint_skills.find_nonessential_ids(documents) != []
        assert (
            lint_skills.find_nonessential_ids(documents, allow_ids={WORK_ITEM_ID}) == []
        )

    def test_non_id_tokens_are_not_reported(self):
        documents = {
            "skill/alpha/SKILL.md": _doc(
                "Not an id: SA-SHORT",
                "Not an id either: C0MWGAYPMX002Z0VC",
                "Still not an id: SA-ABC123456789",
                "Project prefixes without digits are not ids: ALPHA-0SHORT",
            )
        }

        assert lint_skills.find_nonessential_ids(documents) == []

    def test_each_id_on_a_line_is_reported(self):
        other = "SA-0MAAAAAAAAAAAAA"
        findings = lint_skills.find_nonessential_ids(
            {"skill/alpha/SKILL.md": f"Pairs {WORK_ITEM_ID} and {other}."}
        )

        assert [finding.text for finding in findings] == [WORK_ITEM_ID, other]


class TestWorkflowComments:
    def test_workflow_noise_lines_are_reported_and_instructions_are_not(self):
        noise = [
            f"implement - Session ID: {SESSION_ID} - {SESSION_PATH}",
            f"- Session ID: {SESSION_ID} - {SESSION_PATH}",
            "Auto-dispatched by the herdr downtime worker at 2026-11-24T10:21:39Z.",
            "Claim released: the session working on this item was killed.",
            "Start phase: 3 orphaned stashes detected.",
            "Work committed to dev",
        ]
        instructions = [
            "Comment the worklog item with its session id.",
            "Format: `- Session ID: <pi_session_id> - <path_to_sessions_log>`",
        ]
        text = _doc("Keep this instruction.", *noise, *instructions)

        findings = lint_skills.find_workflow_comments({"AGENTS.md": text})

        assert [finding.text for finding in findings] == noise
        assert all(
            finding.kind == lint_skills.KIND_WORKFLOW_COMMENT for finding in findings
        )
        assert all(finding.fix_class == lint_skills.FIX_SAFE for finding in findings)
        assert [finding.spans[0].start_line for finding in findings] == [
            2,
            3,
            4,
            5,
            6,
            7,
        ]


class TestStripping:
    def test_strip_nonessential_ids_removes_ids_and_their_alone_lines(self):
        text = _doc(
            "Keep this instruction.",
            f"({WORK_ITEM_ID})",
            f"The gate ({WORK_ITEM_ID}) protects the tree.",
            "End.",
            "",
        )

        cleaned, removed = lint_skills.strip_nonessential_ids(text)

        assert cleaned == _doc(
            "Keep this instruction.",
            "The gate protects the tree.",
            "End.",
            "",
        )
        assert removed == [WORK_ITEM_ID, WORK_ITEM_ID]

    def test_strip_nonessential_ids_preserves_allow_listed_ids(self):
        text = _doc(f"Guarded by {WORK_ITEM_ID}.", "")

        cleaned, removed = lint_skills.strip_nonessential_ids(
            text, allow_ids={WORK_ITEM_ID}
        )

        assert cleaned == text
        assert removed == []

    def test_strip_workflow_comments_removes_noise_and_keeps_instructions(self):
        text = _doc(
            "Keep this instruction.",
            f"implement - Session ID: {SESSION_ID} - {SESSION_PATH}",
            "Auto-dispatched by the herdr downtime worker at 2026-11-24T10:21:39Z.",
            "Claim released: the session working on this item was killed.",
            "Work committed to dev",
            "Another instruction.",
            "",
        )

        cleaned, removed = lint_skills.strip_workflow_comments(text)

        assert cleaned == _doc("Keep this instruction.", "Another instruction.", "")
        assert len(removed) == 4


# ── AC5: safe vs recommendation-only classification ───────────────────────


class TestFixClassification:
    def test_exact_duplicates_and_traceability_noise_are_safe(self):
        documents = {
            "skill/alpha/SKILL.md": _doc(
                "# Alpha",  # 1
                "",  # 2
                *DUP_BLOCK,  # 3-5
                "",  # 6
                f"Tracking note for {WORK_ITEM_ID}.",  # 7
                f"implement - Session ID: {SESSION_ID} - {SESSION_PATH}",  # 8
                "",  # 9
                *DUP_BLOCK,  # 10-12
            )
        }

        findings = lint_skills.scan_documents(documents)

        assert {finding.kind for finding in findings} == {
            lint_skills.KIND_DUPLICATE,
            lint_skills.KIND_NONESSENTIAL_ID,
            lint_skills.KIND_WORKFLOW_COMMENT,
        }
        assert {finding.fix_class for finding in findings} == {lint_skills.FIX_SAFE}

    def test_near_duplicate_block_is_recommendation_only(self):
        variant = (
            DUP_BLOCK[0],
            "It normalises indentation before comparing every candidate window.",
            DUP_BLOCK[2],
        )
        text = _doc("# Alpha", "", *DUP_BLOCK, "", "---", "", *variant)

        findings = lint_skills.find_duplicates(
            {"skill/alpha/SKILL.md": text}, include_near=True
        )

        assert len(findings) == 1
        finding = findings[0]
        assert finding.kind == lint_skills.KIND_NEAR_DUPLICATE
        assert finding.fix_class == lint_skills.FIX_RECOMMENDATION
        assert [(span.start_line, span.end_line) for span in finding.spans] == [
            (3, 5),
            (9, 11),
        ]
        assert finding.spans[0].text == DUP_TEXT
        assert finding.spans[1].text == "\n".join(variant)
        assert "operator judgement" in finding.rationale

    def test_unrelated_blocks_are_not_near_duplicates(self):
        text = _doc(
            "Alpha instructions explain how the scanner walks the skill tree.",
            "Alpha instructions insist on deterministic ordering of findings.",
            "Alpha instructions describe the fix classes in plain language.",
            "",
            "Beta guidance covers unrelated cron installation and scheduling.",
            "Beta guidance warns that the schedule is only printed, not applied.",
            "Beta guidance explains the report's context fields in detail.",
        )

        assert (
            lint_skills.find_duplicates(
                {"skill/alpha/SKILL.md": text}, include_near=True
            )
            == []
        )


# ── Report-ready output ───────────────────────────────────────────────────


class TestFindingSerialisation:
    def test_finding_and_span_to_dict_are_json_serialisable(self):
        findings = lint_skills.find_duplicates(
            {"skill/alpha/SKILL.md": _doc("# Alpha", "", *DUP_BLOCK, "", *DUP_BLOCK)}
        )

        payload = json.loads(json.dumps([finding.to_dict() for finding in findings]))

        assert payload[0]["kind"] == lint_skills.KIND_DUPLICATE
        assert payload[0]["fix_class"] == lint_skills.FIX_SAFE
        assert payload[0]["text"] == DUP_TEXT
        assert set(payload[0]["spans"][0]) == {
            "path",
            "start_line",
            "end_line",
            "text",
        }

    def test_scan_documents_is_deterministic(self):
        documents = {
            "skill/alpha/SKILL.md": _doc(
                "# Alpha",
                "",
                *DUP_BLOCK,
                "",
                f"Tracking note for {WORK_ITEM_ID}.",
                f"implement - Session ID: {SESSION_ID} - {SESSION_PATH}",
                "",
                *DUP_BLOCK,
            )
        }

        first = [finding.to_dict() for finding in lint_skills.scan_documents(documents)]
        second = [
            finding.to_dict() for finding in lint_skills.scan_documents(documents)
        ]

        assert first == second
        assert first == sorted(
            first,
            key=lambda item: (
                item["spans"][0]["path"],
                item["spans"][0]["start_line"],
                item["kind"],
            ),
        )
