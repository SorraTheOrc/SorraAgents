"""Unit tests for the skill-linter scan-scope and configuration contract.

Work item: SA-0MWGAYVUI008D8RC — "Implement duplicate & safe-fix detection
core" (child of SA-0MWFP1E9P0027EOE). The sibling test module
``test_lint_skills.py`` (SA-0MWGAYPMX002Z0VC) pins the detector primitives;
this module pins the part of AC2 they leave open:

- the **default scan scope** — ``skill/*/SKILL.md`` plus the repo-root
  ``AGENTS.md`` / ``AGENTS_GLOBAL.md``, nothing else;
- the **``--target`` override** — an explicit file or directory replaces the
  default scope (directories expand recursively over the same document set,
  hidden directories are never traversed);
- **configurable thresholds** and **allow-list defaults** carried by
  ``ScanConfig``, and the ``scan_paths`` convenience entry point the CLI
  wraps.

Every fixture is written to ``tmp_path``; no repository file is read.
"""

from __future__ import annotations

import lint_skills
import pytest

DUP_BLOCK = (
    "The linter scans SKILL.md files for repeated blocks of prose.",
    "It normalises whitespace before comparing candidate windows.",
    "Every finding reports the file path and the exact line span.",
)

DUP_TEXT = "\n".join(DUP_BLOCK)

WORK_ITEM_ID = "SA-0MWGAYVUI008D8RC"

SESSION_ID = "01a20d32-86b2-7622-ab9e-1b7288db91fe"
SESSION_PATH = "/home/operator/.pi/agent/sessions/session.jsonl"


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo_with_skills(tmp_path):
    """A miniature repository carrying every document in the default scope."""
    _write(tmp_path / "skill" / "alpha" / "SKILL.md", "# Alpha\n")
    _write(tmp_path / "skill" / "beta" / "SKILL.md", "# Beta\n")
    _write(tmp_path / "AGENTS.md", "# Project AGENTS\n")
    _write(tmp_path / "AGENTS_GLOBAL.md", "# Global AGENTS\n")
    # Out of scope: a non-SKILL.md skill file and a nested AGENTS.md.
    _write(tmp_path / "skill" / "alpha" / "README.md", "# Alpha readme\n")
    _write(tmp_path / "skill" / "alpha" / "nested" / "AGENTS.md", "# Nested\n")
    return tmp_path


# ── AC2: default scan scope ───────────────────────────────────────────────


class TestDefaultScope:
    def test_default_scope_reads_skill_documents_and_root_agents_docs(self, tmp_path):
        root = _repo_with_skills(tmp_path)

        documents = lint_skills.discover_documents(root)

        assert list(documents) == [
            "AGENTS.md",
            "AGENTS_GLOBAL.md",
            "skill/alpha/SKILL.md",
            "skill/beta/SKILL.md",
        ]
        assert documents["AGENTS.md"] == "# Project AGENTS\n"
        assert documents["skill/alpha/SKILL.md"] == "# Alpha\n"

    def test_absent_root_documents_are_omitted_not_errors(self, tmp_path):
        _write(tmp_path / "skill" / "alpha" / "SKILL.md", "# Alpha\n")

        documents = lint_skills.discover_documents(tmp_path)

        assert list(documents) == ["skill/alpha/SKILL.md"]

    def test_default_scope_is_empty_for_a_tree_without_scanned_documents(
        self, tmp_path
    ):
        _write(tmp_path / "skill" / "alpha" / "README.md", "# Not scanned\n")

        assert lint_skills.discover_documents(tmp_path) == {}

    def test_default_scope_does_not_traverse_hidden_directories(self, tmp_path):
        _write(tmp_path / "skill" / "alpha" / "SKILL.md", "# Alpha\n")
        _write(tmp_path / ".worklog" / "skill" / "ghost" / "SKILL.md", "# Ghost\n")

        documents = lint_skills.discover_documents(tmp_path)

        assert list(documents) == ["skill/alpha/SKILL.md"]


# ── AC2: --target override ────────────────────────────────────────────────


class TestTargetOverride:
    def test_file_target_replaces_the_default_scope(self, tmp_path):
        root = _repo_with_skills(tmp_path)
        target = root / "skill" / "beta" / "SKILL.md"

        documents = lint_skills.discover_documents(root, targets=[target])

        assert list(documents) == ["skill/beta/SKILL.md"]
        assert documents["skill/beta/SKILL.md"] == "# Beta\n"

    def test_directory_target_expands_recursively_over_scan_documents(self, tmp_path):
        root = tmp_path
        _write(root / "skill" / "alpha" / "SKILL.md", "# Alpha\n")
        _write(root / "skill" / "alpha" / "AGENTS.md", "# Alpha AGENTS\n")
        _write(root / "skill" / "alpha" / "README.md", "# Not scanned\n")
        _write(root / ".cache" / "SKILL.md", "# Hidden\n")

        documents = lint_skills.discover_documents(
            root, targets=[root / "skill" / "alpha"]
        )

        assert list(documents) == [
            "skill/alpha/AGENTS.md",
            "skill/alpha/SKILL.md",
        ]

    def test_relative_targets_resolve_against_the_root(self, tmp_path):
        root = _repo_with_skills(tmp_path)

        documents = lint_skills.discover_documents(
            root, targets=["skill/alpha/SKILL.md"]
        )

        assert list(documents) == ["skill/alpha/SKILL.md"]

    def test_missing_target_raises_an_actionable_error(self, tmp_path):
        root = _repo_with_skills(tmp_path)
        missing = root / "skill" / "missing" / "SKILL.md"

        with pytest.raises(FileNotFoundError) as excinfo:
            lint_skills.discover_documents(root, targets=[missing])

        assert str(missing) in str(excinfo.value)

    def test_overlapping_targets_read_each_document_once(self, tmp_path):
        root = _repo_with_skills(tmp_path)
        skill_dir = root / "skill" / "alpha"

        documents = lint_skills.discover_documents(
            root, targets=[skill_dir, skill_dir / "SKILL.md", "skill/alpha/SKILL.md"]
        )

        # The directory target covers the nested AGENTS.md too; neither it nor
        # the SKILL.md named three ways appears twice.
        assert list(documents) == [
            "skill/alpha/SKILL.md",
            "skill/alpha/nested/AGENTS.md",
        ]


# ── AC2: configurable thresholds and allow-list defaults ─────────────────


class TestScanConfig:
    def test_defaults_match_the_module_constants(self):
        config = lint_skills.ScanConfig()

        assert config.min_lines == lint_skills.MIN_DUPLICATE_LINES
        assert config.min_chars == lint_skills.MIN_DUPLICATE_CHARS
        assert config.near_ratio == lint_skills.NEAR_DUPLICATE_RATIO
        assert config.include_near is False
        assert config.allow_ids == ()

    def test_invalid_thresholds_are_rejected_loudly(self):
        with pytest.raises(ValueError, match="min_lines"):
            lint_skills.ScanConfig(min_lines=1)
        with pytest.raises(ValueError, match="near_ratio"):
            lint_skills.ScanConfig(near_ratio=1.5)

    def test_scan_uses_the_configured_thresholds(self):
        text = (
            "# Alpha\n"
            "\n"
            "First repeated line of the short block.\n"
            "Second repeated line of the short block.\n"
            "\n"
            "Separator.\n"
            "\n"
            "First repeated line of the short block.\n"
            "Second repeated line of the short block."
        )
        documents = {"skill/alpha/SKILL.md": text}

        assert lint_skills.ScanConfig().scan(documents) == []

        permissive = lint_skills.ScanConfig(min_lines=2, min_chars=10)
        kinds = [finding.kind for finding in permissive.scan(documents)]
        assert kinds == [lint_skills.KIND_DUPLICATE]

    def test_scan_applies_the_allow_list(self):
        documents = {"AGENTS.md": f"Guarded by {WORK_ITEM_ID}.\n"}

        assert lint_skills.ScanConfig().scan(documents) != []
        assert lint_skills.ScanConfig(allow_ids=(WORK_ITEM_ID,)).scan(documents) == []

    def test_scan_can_enable_near_duplicate_recommendations(self):
        variant = (
            DUP_BLOCK[0],
            "It normalises indentation before comparing every candidate window.",
            DUP_BLOCK[2],
        )
        documents = {
            "skill/alpha/SKILL.md": "\n".join(
                ["# Alpha", "", *DUP_BLOCK, "", "---", "", *variant]
            )
        }

        assert lint_skills.ScanConfig().scan(documents) == []

        findings = lint_skills.ScanConfig(include_near=True).scan(documents)
        assert [finding.kind for finding in findings] == [
            lint_skills.KIND_NEAR_DUPLICATE
        ]
        assert findings[0].fix_class == lint_skills.FIX_RECOMMENDATION


# ── AC2: scan_paths end-to-end plumbing ───────────────────────────────────


class TestScanPaths:
    def test_scan_paths_reports_findings_for_the_default_scope(self, tmp_path):
        _write(
            tmp_path / "skill" / "alpha" / "SKILL.md",
            "\n".join(
                ["# Alpha", "", *DUP_BLOCK, "", f"Tracking note for {WORK_ITEM_ID}."]
            )
            + "\n",
        )
        _write(
            tmp_path / "skill" / "beta" / "SKILL.md",
            "\n".join(["# Beta", "", *DUP_BLOCK]) + "\n",
        )
        _write(
            tmp_path / "AGENTS.md",
            "\n".join(
                [
                    "# Project AGENTS",
                    f"implement - Session ID: {SESSION_ID} - {SESSION_PATH}",
                ]
            )
            + "\n",
        )

        findings = lint_skills.scan_paths(tmp_path)

        assert {finding.kind for finding in findings} == {
            lint_skills.KIND_DUPLICATE,
            lint_skills.KIND_NONESSENTIAL_ID,
            lint_skills.KIND_WORKFLOW_COMMENT,
        }
        assert {finding.fix_class for finding in findings} == {lint_skills.FIX_SAFE}
        assert {span.path for finding in findings for span in finding.spans} == {
            "AGENTS.md",
            "skill/alpha/SKILL.md",
            "skill/beta/SKILL.md",
        }
        assert findings == sorted(
            findings,
            key=lambda finding: (
                finding.spans[0].path,
                finding.spans[0].start_line,
                finding.kind,
            ),
        )

    def test_scan_paths_honours_targets_and_config(self, tmp_path):
        root = _repo_with_skills(tmp_path)
        _write(
            root / "skill" / "alpha" / "SKILL.md",
            f"# Alpha\n\nGuarded by {WORK_ITEM_ID}.\n",
        )

        default_findings = lint_skills.scan_paths(root)
        assert any(
            finding.kind == lint_skills.KIND_NONESSENTIAL_ID
            for finding in default_findings
        )

        targeted = lint_skills.scan_paths(
            root,
            targets=[root / "skill" / "beta"],
            config=lint_skills.ScanConfig(allow_ids=(WORK_ITEM_ID,)),
        )
        assert targeted == []

    def test_scan_paths_on_an_empty_scope_returns_no_findings(self, tmp_path):
        assert lint_skills.scan_paths(tmp_path) == []
