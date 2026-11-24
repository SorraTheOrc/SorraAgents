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
import shutil
import subprocess
import sys
from pathlib import Path

import lint_skills

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTEXT_AUDIT = REPO_ROOT / "skill" / "context-audit" / "scripts" / "measure_context.py"

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

    def test_traceability_only_lines_are_dropped_with_their_markdown_marker(self):
        """A bullet/quote/table marker must not survive as a stray artefact."""
        text = _doc(
            "Keep this instruction.",
            f"- {WORK_ITEM_ID}",
            f"1. {WORK_ITEM_ID}",
            f"> {WORK_ITEM_ID}",
            f"| {WORK_ITEM_ID} |",
            f"- [ ] ({WORK_ITEM_ID})",
            "End.",
            "",
        )

        cleaned, removed = lint_skills.strip_nonessential_ids(text)

        assert cleaned == _doc("Keep this instruction.", "End.", "")
        assert removed == [WORK_ITEM_ID] * 5

    def test_inline_removal_leaves_readable_prose(self):
        text = _doc(
            f"Tracked in ({WORK_ITEM_ID}).",
            f"Tracked in {WORK_ITEM_ID}.",
            f"{WORK_ITEM_ID} explains the gate.",
            f"The gate ({WORK_ITEM_ID}) protects the tree.",
            f"- see {WORK_ITEM_ID} for detail",
            "",
        )

        cleaned, _ = lint_skills.strip_nonessential_ids(text)

        assert cleaned == _doc(
            "Tracked in.",
            "Tracked in.",
            "explains the gate.",
            "The gate protects the tree.",
            "- see for detail",
            "",
        )

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


# ══════════════════════════════════════════════════════════════════════════
# CLI, report formatting, --dry-run / --apply and --install-cron
#
# Work item: SA-0MWGAZ286009G2JR — "Test report format, CLI, dry-run, cron"
# (child of SA-0MWFP1E9P0027EOE). The tests pin the CLI contract that the
# sibling child SA-0MWGAZA390062S6B implements on top of the detector core:
#
# - AC1 text and JSON reports carry file, section/line span, current text,
#   proposed text, fix class and rationale; JSON is parseable and stable
# - AC2 --dry-run (the default) changes nothing and prints what would change
# - AC3 --apply applies only safe-fix classes; exit reflects what remains
# - AC4 recommendation-class findings are reported with full context and are
#   never applied automatically (explicit operator approval required)
# - AC5 --install-cron weekly prints a valid weekly crontab line for a
#   dry-run report and does NOT claim the schedule is active
# - AC6 exit codes: 0 clean, 1 findings present, 2 usage/scan error
# ══════════════════════════════════════════════════════════════════════════

CLI_DOC = _doc(
    "# Alpha skill",  # 1
    "",  # 2
    "## Usage",  # 3
    "",  # 4
    *DUP_BLOCK,  # 5-7
    "",  # 8
    f"Tracking note for {WORK_ITEM_ID}.",  # 9
    f"implement - Session ID: {SESSION_ID} - {SESSION_PATH}",  # 10
    "",  # 11
    *DUP_BLOCK,  # 12-14
)


def _make_repo(tmp_path, documents: dict[str, str]):
    """Materialise *documents* under *tmp_path* and return the repo root."""
    from pathlib import Path

    root = Path(tmp_path)
    for relative, text in documents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _run_cli(capsys, *argv: str):
    """Invoke ``lint_skills.main`` in-process and capture its streams."""
    code = lint_skills.main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _read(root, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def _copy_repo_documents(destination: Path) -> Path:
    """Copy this repo's scannable documents into *destination*.

    The skill-linter's default scope (``skill/*/SKILL.md`` plus the root
    ``AGENTS.md`` / ``AGENTS_GLOBAL.md``) is mirrored so the context-audit
    measurement can run against a disposable tree.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("AGENTS.md", "AGENTS_GLOBAL.md"):
        source = REPO_ROOT / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    for source in sorted(REPO_ROOT.glob("skill/*/SKILL.md")):
        target = destination / "skill" / source.parent.name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination


def _measure_context(root: Path) -> dict:
    """Run the context-audit measurement for *root* and return its components."""
    result = subprocess.run(
        [sys.executable, str(CONTEXT_AUDIT), "--repo-root", str(root), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["components"]


# ── AC1: text and JSON report formatting ──────────────────────────────────


class TestTextReport:
    def test_text_report_shows_file_span_section_text_and_rationale(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {"skill/alpha/SKILL.md": CLI_DOC})

        code, out, _ = _run_cli(capsys, "--root", root)

        assert code == lint_skills.EXIT_FINDINGS
        # File and the duplicate's line span, with its nearest section heading.
        assert "skill/alpha/SKILL.md:5-7" in out
        assert "## Usage" in out
        # Current text and rationale are rendered for each finding.
        assert DUP_BLOCK[0] in out
        assert "rationale:" in out
        # Fix class is visible in the text report.
        assert "safe" in out
        # The duplicate action explains exactly what would change.
        assert "would" in out.lower()

    def test_text_report_lists_every_finding(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {"skill/alpha/SKILL.md": CLI_DOC})

        _, out, _ = _run_cli(capsys, "--root", root)

        assert WORK_ITEM_ID in out
        assert SESSION_ID in out
        assert out.count("rationale:") == 3


class TestJsonReport:
    def test_json_report_is_parseable_with_a_stable_schema(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {"skill/alpha/SKILL.md": CLI_DOC})

        code, out, _ = _run_cli(capsys, "--root", root, "--json")

        assert code == lint_skills.EXIT_FINDINGS
        payload = json.loads(out)
        assert payload["schema_version"] == lint_skills.REPORT_SCHEMA_VERSION
        assert payload["dry_run"] is True
        assert payload["root"] == str(root)
        assert set(payload["summary"]) == {
            "total",
            "safe",
            "recommendation",
            "applied",
            "remaining",
        }
        assert payload["summary"]["total"] == len(payload["findings"]) == 3
        assert payload["summary"]["safe"] == 3
        assert payload["summary"]["recommendation"] == 0
        assert payload["summary"]["applied"] == 0

        entry = payload["findings"][0]
        assert set(entry) == {
            "kind",
            "fix_class",
            "path",
            "start_line",
            "end_line",
            "section",
            "text",
            "proposed_text",
            "rationale",
            "requires_approval",
            "action",
            "spans",
        }
        assert entry["path"] == "skill/alpha/SKILL.md"
        assert (entry["start_line"], entry["end_line"]) == (5, 7)
        assert entry["section"] == "## Usage"
        assert entry["text"] == DUP_TEXT
        assert entry["proposed_text"] == DUP_TEXT
        assert entry["requires_approval"] is False
        assert set(entry["spans"][0]) == {
            "path",
            "start_line",
            "end_line",
            "text",
        }

    def test_json_report_is_deterministic_between_runs(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {"skill/alpha/SKILL.md": CLI_DOC})

        _, first, _ = _run_cli(capsys, "--root", root, "--json")
        _, second, _ = _run_cli(capsys, "--root", root, "--json")

        assert first == second


# ── AC2: --dry-run is the default and changes nothing ─────────────────────


class TestDryRun:
    def test_default_run_modifies_no_files(self, tmp_path, capsys):
        path = "skill/alpha/SKILL.md"
        root = _make_repo(tmp_path, {path: CLI_DOC})
        before = _read(root, path)

        code, out, _ = _run_cli(capsys, "--root", root)

        assert code == lint_skills.EXIT_FINDINGS
        assert _read(root, path) == before
        assert "dry-run" in out.lower()
        assert "no files modified" in out.lower()

    def test_explicit_dry_run_overrides_apply(self, tmp_path, capsys):
        path = "skill/alpha/SKILL.md"
        root = _make_repo(tmp_path, {path: CLI_DOC})
        before = _read(root, path)

        code, _, _ = _run_cli(capsys, "--root", root, "--apply", "--dry-run")

        assert code == lint_skills.EXIT_FINDINGS
        assert _read(root, path) == before

    def test_clean_tree_exits_zero(self, tmp_path, capsys):
        root = _make_repo(
            tmp_path,
            {"skill/alpha/SKILL.md": _doc("# Alpha", "", "A single unique instruction.")},
        )

        code, out, _ = _run_cli(capsys, "--root", root)

        assert code == lint_skills.EXIT_CLEAN
        assert "no findings" in out.lower()


# ── AC3: --apply only applies safe-fix classes ────────────────────────────


class TestApply:
    def test_apply_removes_duplicates_and_strips_traceability_noise(self, tmp_path, capsys):
        path = "skill/alpha/SKILL.md"
        root = _make_repo(tmp_path, {path: CLI_DOC})

        code, out, _ = _run_cli(capsys, "--root", root, "--apply")

        assert code == lint_skills.EXIT_CLEAN
        cleaned = _read(root, path)
        assert cleaned.count(DUP_BLOCK[0]) == 1
        assert WORK_ITEM_ID not in cleaned
        assert SESSION_ID not in cleaned
        assert "## Usage" in cleaned
        assert "applied" in out.lower()

    def test_apply_leaves_recommendation_findings_untouched(self, tmp_path, capsys):
        path = "skill/alpha/SKILL.md"
        variant = (
            DUP_BLOCK[0],
            "It normalises indentation before comparing every candidate window.",
            DUP_BLOCK[2],
        )
        text = _doc("# Alpha", "", *DUP_BLOCK, "", "---", "", *variant)
        root = _make_repo(tmp_path, {path: text})

        code, out, _ = _run_cli(capsys, "--root", root, "--apply")

        assert code == lint_skills.EXIT_FINDINGS
        assert _read(root, path) == text
        assert "recommendation" in out.lower()

    def test_apply_exit_code_reflects_remaining_findings(self, tmp_path, capsys):
        root = _make_repo(
            tmp_path,
            {
                "skill/alpha/SKILL.md": _doc(
                    "# Alpha", "", f"Tracking note for {WORK_ITEM_ID}."
                )
            },
        )

        code, _, _ = _run_cli(capsys, "--root", root, "--apply")

        assert code == lint_skills.EXIT_CLEAN
        assert WORK_ITEM_ID not in _read(root, "skill/alpha/SKILL.md")


# ── AC1: --target replaces the default scan scope ─────────────────────────


class TestTargetFlag:
    def test_target_file_is_scanned_instead_of_the_default_scope(
        self, tmp_path, capsys
    ):
        root = _make_repo(
            tmp_path,
            {
                "skill/alpha/SKILL.md": _doc("# Alpha", "", "A unique instruction."),
                "docs/notes.md": _doc("# Notes", "", f"Tracking {WORK_ITEM_ID}."),
            },
        )

        default_code, default_out, _ = _run_cli(capsys, "--root", root)
        assert default_code == lint_skills.EXIT_CLEAN
        assert "docs/notes.md" not in default_out

        code, out, _ = _run_cli(
            capsys, "--root", root, "--target", "docs/notes.md", "--json"
        )

        assert code == lint_skills.EXIT_FINDINGS
        payload = json.loads(out)
        assert {entry["path"] for entry in payload["findings"]} == {"docs/notes.md"}

    def test_repeated_targets_are_scanned_together(self, tmp_path, capsys):
        root = _make_repo(
            tmp_path,
            {
                "skill/alpha/SKILL.md": _doc(
                    "# Alpha", "", f"Tracking {WORK_ITEM_ID}."
                ),
                "skill/beta/SKILL.md": _doc(
                    "# Beta", "", f"Tracking {WORK_ITEM_ID}."
                ),
                "skill/gamma/SKILL.md": _doc(
                    "# Gamma", "", f"Tracking {WORK_ITEM_ID}."
                ),
            },
        )

        _, out, _ = _run_cli(
            capsys,
            "--root",
            root,
            "--json",
            "--target",
            "skill/alpha/SKILL.md",
            "--target",
            "skill/beta/SKILL.md",
        )

        payload = json.loads(out)
        assert {entry["path"] for entry in payload["findings"]} == {
            "skill/alpha/SKILL.md",
            "skill/beta/SKILL.md",
        }


# ── AC4: recommendation-class findings require operator approval ──────────


class TestRecommendationGating:
    def test_recommendation_is_reported_with_context_and_needs_approval(self, tmp_path, capsys):
        variant = (
            DUP_BLOCK[0],
            "It normalises indentation before comparing every candidate window.",
            DUP_BLOCK[2],
        )
        text = _doc("# Alpha", "", *DUP_BLOCK, "", "---", "", *variant)
        root = _make_repo(tmp_path, {"skill/alpha/SKILL.md": text})

        code, out, _ = _run_cli(capsys, "--root", root, "--json")

        assert code == lint_skills.EXIT_FINDINGS
        payload = json.loads(out)
        recommendations = [
            entry
            for entry in payload["findings"]
            if entry["fix_class"] == lint_skills.FIX_RECOMMENDATION
        ]
        assert recommendations
        entry = recommendations[0]
        assert entry["requires_approval"] is True
        assert entry["path"] == "skill/alpha/SKILL.md"
        assert entry["start_line"] == 3
        assert entry["end_line"] == 5
        assert entry["section"] == "# Alpha"
        assert DUP_BLOCK[0] in entry["text"]
        assert entry["rationale"]
        assert "approval" in entry["action"].lower()

    def test_recommendation_class_never_changes_files_on_apply(self, tmp_path, capsys):
        variant = (
            DUP_BLOCK[0],
            "It normalises indentation before comparing every candidate window.",
            DUP_BLOCK[2],
        )
        text = _doc("# Alpha", "", *DUP_BLOCK, "", "---", "", *variant)
        root = _make_repo(tmp_path, {"skill/alpha/SKILL.md": text})

        _, out, _ = _run_cli(capsys, "--root", root, "--apply")

        assert _read(root, "skill/alpha/SKILL.md") == text
        assert "operator approval" in out.lower()


# ── AC5: --install-cron weekly ────────────────────────────────────────────


class TestInstallCron:
    def _cron_line(self, out: str) -> str:
        for line in out.splitlines():
            if "lint_skills.py" in line:
                return line
        raise AssertionError(f"no crontab line in output:\n{out}")

    def test_install_cron_prints_a_valid_weekly_line(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {})

        code, out, _ = _run_cli(capsys, "--root", root, "--install-cron", "weekly")

        assert code == lint_skills.EXIT_CRON_OK
        line = self._cron_line(out)
        assert line.split()[:5] == ["0", "3", "*", "*", "1"]
        assert "--dry-run" in line
        assert "--json" in line
        assert str(root) in line

    def test_install_cron_defaults_to_weekly(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {})

        _, out, _ = _run_cli(capsys, "--root", root, "--install-cron")

        assert self._cron_line(out).split()[:5] == ["0", "3", "*", "*", "1"]

    def test_install_cron_does_not_claim_the_schedule_is_active(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {})

        _, out, _ = _run_cli(capsys, "--root", root, "--install-cron")

        lowered = out.lower()
        assert "not installed" in lowered
        assert "crontab -e" in lowered
        assert "is installed" not in lowered
        assert "has been installed" not in lowered

    def test_install_cron_rejects_an_unknown_schedule(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {})

        code, _, err = _run_cli(capsys, "--root", root, "--install-cron", "daily")

        assert code == lint_skills.EXIT_ERROR
        assert "weekly" in err.lower()


# ── AC6: exit codes ───────────────────────────────────────────────────────


class TestExitCodes:
    def test_exit_codes_are_distinct(self):
        assert lint_skills.EXIT_CLEAN == 0
        assert lint_skills.EXIT_FINDINGS == 1
        assert lint_skills.EXIT_ERROR == 2
        assert len(
            {
                lint_skills.EXIT_CLEAN,
                lint_skills.EXIT_FINDINGS,
                lint_skills.EXIT_ERROR,
            }
        ) == 3

    def test_missing_target_is_a_usage_error(self, tmp_path, capsys):
        root = _make_repo(tmp_path, {})

        code, _, err = _run_cli(capsys, "--root", root, "--target", "missing.md")

        assert code == lint_skills.EXIT_ERROR
        assert "does not exist" in err


# ── Section resolution used by the report ─────────────────────────────────


class TestSectionResolution:
    def test_nearest_preceding_heading_is_reported(self):
        text = _doc(
            "# Top",  # 1
            "",  # 2
            "Intro.",  # 3
            "",  # 4
            "## Usage",  # 5
            "",  # 6
            "Body.",  # 7
            "",  # 8
            "### Details",  # 9
            "",  # 10
            "Deep body.",  # 11
        )

        assert lint_skills.section_for_line(text, 3) == "# Top"
        assert lint_skills.section_for_line(text, 7) == "## Usage"
        assert lint_skills.section_for_line(text, 11) == "### Details"

    def test_documents_without_headings_report_the_document_top(self):
        assert (
            lint_skills.section_for_line("plain body\nsecond line\n", 2)
            == "(document top)"
        )


# ── AC5: --apply leaves the measured context surface non-regressing ───────


class TestApplyKeepsContextSurfaceNonRegressing:
    def test_apply_over_repo_documents_never_grows_a_measured_component(
        self, tmp_path, capsys
    ):
        """Applying safe fixes may only shrink the context-audit surface.

        The committed context-budget gate (``tests/test_context_budget_gate.py``)
        enforces an upper bound on the startup surface, so the linter's safe
        fixes must never push any measured component above its pre-apply value.
        """
        assert CONTEXT_AUDIT.is_file(), f"missing context-audit tool: {CONTEXT_AUDIT}"
        root = _copy_repo_documents(tmp_path / "repo")
        before = _measure_context(root)

        code, out, _ = _run_cli(capsys, "--root", root, "--apply", "--json")

        assert code in (lint_skills.EXIT_CLEAN, lint_skills.EXIT_FINDINGS)
        applied = json.loads(out)["summary"]["applied"]
        after = _measure_context(root)
        for component, measurement in before.items():
            assert after[component]["chars"] <= measurement["chars"], (
                f"applying safe fixes grew the {component!r} context component: "
                f"{measurement['chars']} -> {after[component]['chars']}"
            )
        if applied:
            assert after["total"]["chars"] < before["total"]["chars"]


# ══════════════════════════════════════════════════════════════════════════
# End-to-end integration and SKILL.md documentation contract
#
# Work item: SA-0MWGAZTX1008LZI4 — "Write skill-linter SKILL.md and
# integration test" (child of SA-0MWFP1E9P0027EOE).
#
# - AC1/AC2 the SKILL.md documents invocation, scope, permission flow and the
#   weekly run, and links the reference instead of restating its rule tables
# - AC3 one fixture tree exercises the whole lifecycle through the real CLI:
#   scan → text/JSON report → dry-run immutability → --apply safe fixes only
#   → --install-cron line
# - AC4 the SKILL.md passes the linter's own checks (self-consistency)
# ══════════════════════════════════════════════════════════════════════════

SKILL_MD = REPO_ROOT / "skill" / "skill-linter" / "SKILL.md"
REFERENCE_DOC = REPO_ROOT / "docs" / "dev" / "skill-linter-reference.md"
LINT_CLI = REPO_ROOT / "skill" / "skill-linter" / "scripts" / "lint_skills.py"

#: A near-duplicate pair: identical first two lines, a slightly edited third.
NEAR_BLOCK_ALPHA = (
    "Operators approve each recommendation before any rewrite is applied.",
    "The linter reports the file, section, line span and rationale.",
    "Nothing in the recommendation class is ever written automatically.",
)
NEAR_BLOCK_BETA = (
    "Operators approve each recommendation before any rewrite is applied.",
    "The linter reports the file, section, line span and rationale.",
    "Nothing in the recommendation class is written automatically by the tool.",
)


def _integration_tree(tmp_path):
    """A fixture skill tree carrying every finding class the CLI reports."""
    alpha = _doc(
        "# Alpha skill",  # 1
        "",  # 2
        "## Usage",  # 3
        "",  # 4
        *DUP_BLOCK,  # 5-7
        "",  # 8
        f"Tracking note for {WORK_ITEM_ID}.",  # 9
        f"implement - Session ID: {SESSION_ID} - {SESSION_PATH}",  # 10
        "",  # 11
        *DUP_BLOCK,  # 12-14
        "",  # 15
        *NEAR_BLOCK_ALPHA,  # 16-18
        "",
    )
    beta = _doc(
        "# Beta skill",  # 1
        "",  # 2
        "## Rules",  # 3
        "",  # 4
        *NEAR_BLOCK_BETA,  # 5-7
        "",
    )
    return _make_repo(
        tmp_path,
        {"skill/alpha/SKILL.md": alpha, "skill/beta/SKILL.md": beta},
    )


def _run_real_cli(root, *argv: str):
    """Run the CLI as a subprocess, exactly as an operator or cron would."""
    return subprocess.run(
        [sys.executable, str(LINT_CLI), "--root", str(root), *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def _snapshot(root) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("SKILL.md"))
    }


class TestIntegrationLifecycle:
    def test_scan_report_dry_run_apply_and_cron_over_a_fixture_tree(self, tmp_path):
        root = _integration_tree(tmp_path)
        before = _snapshot(root)

        # 1. Scan → text report: every finding class arrives with context.
        text = _run_real_cli(root)
        assert text.returncode == lint_skills.EXIT_FINDINGS
        assert "skill/alpha/SKILL.md" in text.stdout
        assert "duplicate" in text.stdout
        assert WORK_ITEM_ID in text.stdout
        assert "recommendation" in text.stdout
        assert "rationale:" in text.stdout

        # 2. Report → JSON: parseable, complete and stable in schema.
        report = _run_real_cli(root, "--json")
        assert report.returncode == lint_skills.EXIT_FINDINGS
        payload = json.loads(report.stdout)
        assert payload["schema_version"] == lint_skills.REPORT_SCHEMA_VERSION
        assert payload["dry_run"] is True
        assert {finding["kind"] for finding in payload["findings"]} == {
            lint_skills.KIND_DUPLICATE,
            lint_skills.KIND_NEAR_DUPLICATE,
            lint_skills.KIND_NONESSENTIAL_ID,
            lint_skills.KIND_WORKFLOW_COMMENT,
        }
        assert payload["summary"]["applied"] == 0

        # 3. Dry-run immutability: neither report changed a byte.
        assert _snapshot(root) == before

        # 4. Apply: safe fixes are written; the recommendation is untouched.
        applied = _run_real_cli(root, "--apply", "--json")
        assert applied.returncode == lint_skills.EXIT_FINDINGS  # recommendation remains
        result = json.loads(applied.stdout)
        assert result["dry_run"] is False
        assert result["summary"]["applied"] > 0
        assert {finding["kind"] for finding in result["findings"]} == {
            lint_skills.KIND_NEAR_DUPLICATE
        }

        after = _snapshot(root)
        alpha = after["skill/alpha/SKILL.md"]
        assert alpha.count(DUP_BLOCK[0]) == 1
        assert WORK_ITEM_ID not in alpha
        assert "Session ID:" not in alpha
        assert "\n".join(NEAR_BLOCK_ALPHA) in alpha
        assert "\n".join(NEAR_BLOCK_BETA) in after["skill/beta/SKILL.md"]
        assert after["skill/beta/SKILL.md"] == before["skill/beta/SKILL.md"]

        # 5. Re-applying changes nothing: the safe fixes are idempotent.
        again = _run_real_cli(root, "--apply", "--json")
        assert json.loads(again.stdout)["summary"]["applied"] == 0
        assert _snapshot(root) == after

        # 6. Cron helper prints a weekly dry-run line, never claiming it is active.
        cron = _run_real_cli(root, "--install-cron", "weekly")
        assert cron.returncode == lint_skills.EXIT_CRON_OK
        line = next(line for line in cron.stdout.splitlines() if "lint_skills.py" in line)
        assert line.split()[:5] == ["0", "3", "*", "*", "1"]
        assert "--dry-run" in line and "--json" in line
        assert str(root) in line
        assert "NOT installed" in cron.stdout


class TestSkillMdDocumentation:
    def _text(self) -> str:
        return SKILL_MD.read_text(encoding="utf-8")

    def test_frontmatter_and_invocation_follow_sibling_conventions(self):
        text = self._text()
        assert text.startswith("---\n")
        frontmatter, separator, body = text.partition("\n---\n")
        assert separator, "SKILL.md must carry a closing frontmatter fence"
        assert "name: skill-linter" in frontmatter
        assert "description:" in frontmatter
        assert "/skill:skill-linter" in body
        assert "$(skill_path skill-linter)/scripts/lint_skills.py" in body

    def test_scope_rules_permission_flow_and_cron_usage_are_documented(self):
        body = self._text()
        for phrase in (
            "skill/<name>/SKILL.md",
            "AGENTS.md",
            "AGENTS_GLOBAL.md",
            "--apply",
            "--dry-run",
            "--install-cron weekly",
            "Permission flow",
            "Recommendations",
            "Exit codes",
        ):
            assert phrase in body, f"SKILL.md does not document {phrase!r}"

    def test_links_the_reference_without_restating_its_contract(self):
        body = self._text()
        reference = REFERENCE_DOC.read_text(encoding="utf-8")
        assert "docs/dev/skill-linter-reference.md" in body
        # The brief summarises; the rule tables, schema and constants live in
        # the reference (AC2: no wholesale duplication).
        assert len(body) < len(reference) / 2
        for internal in ("MIN_DUPLICATE_LINES", "REPORT_SCHEMA_VERSION", "schema_version"):
            assert internal not in body


class TestSkillMdSelfConsistency:
    def test_skill_md_passes_the_linters_own_checks(self):
        result = _run_real_cli(REPO_ROOT, "--target", str(SKILL_MD), "--json")

        assert result.returncode == lint_skills.EXIT_CLEAN, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"] == []
        assert payload["summary"]["total"] == 0
