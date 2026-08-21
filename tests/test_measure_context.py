"""Tests for skill/context-audit/scripts/measure_context.py (F1).

Pins the measurement contract: per-component bytes + token estimates for
global AGENTS.md, project AGENTS.md, and skills-section description prose;
machine-readable output (JSON / key=value); and the threshold regression gate
(non-zero exit when measured bytes exceed configured limits).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "skill" / "context-audit" / "scripts"
sys.path.insert(0, str(_SCRIPT_DIR))

import measure_context as mc

GLOBAL_MD = "AGENTS_GLOBAL.md"
PROJECT_MD = "AGENTS.md"


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A minimal repo tree with known-size AGENTS files and two skills."""
    (tmp_path / GLOBAL_MD).write_text("G" * 100, encoding="utf-8")   # 100 B
    (tmp_path / PROJECT_MD).write_text("P" * 40, encoding="utf-8")   # 40 B
    skill_a = tmp_path / "skill" / "alpha"
    skill_a.mkdir(parents=True)
    (skill_a / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: \"short prose\"\n---\n# Alpha\n",
        encoding="utf-8",
    )  # description = "short prose" (11 chars)
    skill_b = tmp_path / "skill" / "beta"
    skill_b.mkdir(parents=True)
    (skill_b / "SKILL.md").write_text(
        "---\nname: beta\ndescription: |\n  block prose\n  line two\n---\n# Beta\n",
        encoding="utf-8",
    )  # description = "block prose\nline two" (20 chars)
    # A script dir without SKILL.md must NOT be counted as a skill.
    (tmp_path / "skill" / "scripts").mkdir()
    (tmp_path / "skill" / "scripts" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


# ── Parsing helpers ────────────────────────────────────────────────────────


class TestExtractFrontmatter:
    def test_extracts_block(self):
        text = "---\nname: x\ndescription: y\n---\n# Body\n"
        assert mc.extract_frontmatter(text) == "name: x\ndescription: y"

    def test_missing_frontmatter_returns_empty(self):
        assert mc.extract_frontmatter("# No frontmatter\n") == ""

    def test_frontmatter_requires_leading_dash(self):
        text = "x\n---\nname: y\n---\n"
        assert mc.extract_frontmatter(text) == ""


class TestParseDescription:
    def test_inline_unquoted(self):
        assert mc.parse_description("name: x\ndescription: do the thing\n") == "do the thing"

    def test_inline_quoted(self):
        assert (
            mc.parse_description('name: x\ndescription: "do the thing"\n')
            == "do the thing"
        )

    def test_quoted_with_escaped_quote(self):
        assert (
            mc.parse_description('description: "say \\"hi\\""\n')
            == 'say "hi"'
        )

    def test_literal_block(self):
        frontmatter = "name: x\ndescription: |\n  line one\n  line two\n"
        assert mc.parse_description(frontmatter) == "line one\nline two"

    def test_literal_block_stops_at_next_key(self):
        frontmatter = "description: |\n  prose\nname: x\n"
        assert mc.parse_description(frontmatter) == "prose"

    def test_indented_plain_scalar(self):
        frontmatter = (
            "name: x\n"
            "description:\n"
            "  Use this skill to review local code.\n"
            "  It focuses on correctness.\n"
        )
        assert (
            mc.parse_description(frontmatter)
            == "Use this skill to review local code.\nIt focuses on correctness."
        )

    def test_missing_returns_empty(self):
        assert mc.parse_description("name: x\n") == ""


class TestEstimateTokens:
    def test_zero(self):
        assert mc.estimate_tokens(0) == 0

    def test_rounds_chars_over_ratio(self):
        assert mc.estimate_tokens(100) == 25
        assert mc.estimate_tokens(101) == 25  # round(25.25)
        assert mc.estimate_tokens(102) == 26  # round(25.5)

    def test_small_input_never_zero(self):
        assert mc.estimate_tokens(1) == 1


# ── Measurement ────────────────────────────────────────────────────────────


class TestMeasure:
    def test_components_and_total(self, repo_root: Path):
        comps = mc.measure(repo_root)
        assert comps["global_agents"]["bytes"] == 100
        assert comps["project_agents"]["bytes"] == 40
        assert comps["skills_prose"]["bytes"] == 31  # 11 + 20
        assert comps["skills_prose"]["skill_count"] == 2
        assert comps["total"]["bytes"] == 171

    def test_token_estimates(self, repo_root: Path):
        comps = mc.measure(repo_root)
        assert comps["total"]["tokens"] == mc.estimate_tokens(171)
        assert comps["skills_prose"]["tokens"] == 8  # round(31/4)

    def test_missing_agents_files_count_as_zero(self, tmp_path: Path):
        (tmp_path / "skill" / "alpha").mkdir(parents=True)
        (tmp_path / "skill" / "alpha" / "SKILL.md").write_text(
            "---\ndescription: x\n---\n", encoding="utf-8"
        )
        comps = mc.measure(tmp_path)
        assert comps["global_agents"]["bytes"] == 0
        assert comps["project_agents"]["bytes"] == 0
        assert comps["total"]["bytes"] == 1

    def test_script_dirs_without_skill_md_excluded(self, repo_root: Path):
        comps = mc.measure(repo_root)
        assert comps["skills_prose"]["skill_count"] == 2

    def test_hidden_skills_excluded_by_default(self, tmp_path: Path):
        """disable-model-invocation skills are excluded from the startup
        surface (they do not appear in the session skills block)."""
        (tmp_path / GLOBAL_MD).write_text("G" * 10, encoding="utf-8")
        hidden = tmp_path / "skill" / "triage"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text(
            "---\nname: triage\ndisable-model-invocation: true\n"
            "description: hidden prose\n---\n# Triage\n",
            encoding="utf-8",
        )
        visible = tmp_path / "skill" / "audit"
        visible.mkdir(parents=True)
        (visible / "SKILL.md").write_text(
            "---\nname: audit\ndescription: visible prose\n---\n# Audit\n",
            encoding="utf-8",
        )
        comps = mc.measure(tmp_path)
        assert comps["skills_prose"]["skill_count"] == 1
        assert comps["skills_prose"]["hidden_skill_count"] == 1
        assert comps["skills_prose"]["bytes"] == len("visible prose")

    def test_include_hidden_counts_all(self, tmp_path: Path):
        (tmp_path / GLOBAL_MD).write_text("G" * 10, encoding="utf-8")
        hidden = tmp_path / "skill" / "triage"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text(
            "---\nname: triage\ndisable-model-invocation: true\n"
            "description: hidden prose\n---\n# Triage\n",
            encoding="utf-8",
        )
        comps = mc.measure(tmp_path, include_hidden=True)
        assert comps["skills_prose"]["skill_count"] == 1
        assert comps["skills_prose"]["bytes"] == len("hidden prose")

    def test_hidden_skill_names(self, tmp_path: Path):
        (tmp_path / GLOBAL_MD).write_text("G" * 10, encoding="utf-8")
        hidden = tmp_path / "skill" / "triage"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text(
            "---\nname: triage\ndisable-model-invocation: true\n"
            "description: x\n---\n",
            encoding="utf-8",
        )
        assert mc.hidden_skill_names(tmp_path) == ["triage"]


# ── Output formats ─────────────────────────────────────────────────────────


class TestOutput:
    def test_keyvalue_output(self, repo_root: Path, capsys):
        code = mc.main(["--repo-root", str(repo_root), "--keyvalue"])
        out = capsys.readouterr().out
        assert code == mc.EXIT_OK
        lines = out.strip().splitlines()
        assert "global_agents.bytes=100" in lines
        assert "project_agents.bytes=40" in lines
        assert "skills_prose.bytes=31" in lines
        assert "skills_prose.skill_count=2" in lines
        assert "total.bytes=171" in lines

    def test_json_output(self, repo_root: Path, capsys):
        code = mc.main(["--repo-root", str(repo_root), "--json"])
        assert code == mc.EXIT_OK
        data = json.loads(capsys.readouterr().out)
        assert data["components"]["global_agents"]["bytes"] == 100
        assert data["components"]["total"]["bytes"] == 171
        assert data["threshold_exceeded"] == []

    def test_text_output_shows_all_components(self, repo_root: Path, capsys):
        code = mc.main(["--repo-root", str(repo_root)])
        out = capsys.readouterr().out
        assert code == mc.EXIT_OK
        for name in ("global_agents", "project_agents", "skills_prose", "total"):
            assert name in out


# ── Threshold regression gate (AC4) ────────────────────────────────────────


class TestThresholds:
    def test_within_budget_exits_zero(self, repo_root: Path, capsys):
        code = mc.main(
            ["--repo-root", str(repo_root), "--json",
             "--threshold", "total=200", "--threshold", "skills_prose=50"]
        )
        assert code == mc.EXIT_OK
        assert capsys.readouterr().err == ""

    def test_exceeding_threshold_exits_two(self, repo_root: Path, capsys):
        code = mc.main(
            ["--repo-root", str(repo_root), "--json",
             "--threshold", "skills_prose=20"]
        )
        assert code == mc.EXIT_THRESHOLD_EXCEEDED
        err = capsys.readouterr().err
        assert "skills_prose" in err
        assert "31" in err  # measured value reported

    def test_thresholds_json_file(self, repo_root: Path, tmp_path: Path, capsys):
        thresholds = tmp_path / "thresholds.json"
        thresholds.write_text(
            json.dumps({"global_agents": 50, "total": 1000}), encoding="utf-8"
        )
        code = mc.main(
            ["--repo-root", str(repo_root), "--json",
             "--thresholds", str(thresholds)]
        )
        assert code == mc.EXIT_THRESHOLD_EXCEEDED
        data = json.loads(capsys.readouterr().out)
        assert data["threshold_exceeded"] == ["global_agents"]

    def test_json_reports_exceeded_list(self, repo_root: Path, capsys):
        mc.main(
            ["--repo-root", str(repo_root), "--json",
             "--threshold", "project_agents=10"]
        )
        data = json.loads(capsys.readouterr().out)
        assert data["threshold_exceeded"] == ["project_agents"]

    def test_invalid_threshold_spec_is_usage_error(self, repo_root: Path):
        with pytest.raises(SystemExit):
            mc.main(["--repo-root", str(repo_root), "--threshold", "nonsense"])

    def test_missing_repo_root_is_error(self, tmp_path: Path):
        assert (
            mc.main(["--repo-root", str(tmp_path / "nope")])
            == mc.EXIT_ERROR
        )


# ── Threshold generation helper (AC2, SA-0MT1WO815009KXUC) ─────────────────


class TestGenerateThresholds:
    """Tests for the `--generate-thresholds` and `--write-thresholds` flags.

    AC1: `--generate-thresholds` prints a JSON file to stdout with the
        current measurements as threshold values.
    AC2: `--write-thresholds <path>` writes a valid JSON file at the path.
    AC3: Output includes all four keys: global_agents, project_agents,
        skills_prose, total (byte values).
    AC4: Output values match the measured components from the same repo.
    """

    def test_generate_thresholds_prints_valid_json(self, repo_root: Path, capsys):
        """`--generate-thresholds` prints a JSON object with all four keys."""
        code = mc.main(["--repo-root", str(repo_root), "--generate-thresholds"])
        assert code == mc.EXIT_OK
        data = json.loads(capsys.readouterr().out)
        for component in ("global_agents", "project_agents", "skills_prose", "total"):
            assert component in data, f"missing threshold component {component!r}"
            assert isinstance(data[component], int), (
                f"threshold {component!r} must be an int, got {data[component]!r}"
            )
            assert data[component] > 0, f"threshold {component!r} must be positive"

    def test_generate_thresholds_match_measured(self, repo_root: Path, capsys):
        """Generated threshold values match the measured component bytes."""
        code = mc.main(["--repo-root", str(repo_root), "--generate-thresholds"])
        assert code == mc.EXIT_OK
        threshold_data = json.loads(capsys.readouterr().out)

        comps = mc.measure(repo_root)
        for name, comp in comps.items():
            if name == "total":
                continue
            assert threshold_data[name] == comp["bytes"], (
                f"threshold {name}={threshold_data[name]} != measured {comp['bytes']}"
            )
        assert threshold_data["total"] == comps["total"]["bytes"]

    def test_generate_thresholds_tracks_include_hidden(self, tmp_path: Path, capsys):
        """`--generate-thresholds --include-hidden` counts hidden skills."""
        (tmp_path / "AGENTS_GLOBAL.md").write_text("G" * 20, encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("P" * 20, encoding="utf-8")
        hidden = tmp_path / "skill" / "hidden_skill"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text(
            "---\ndisable-model-invocation: true\ndescription: secret prose\n---\n",
            encoding="utf-8",
        )

        code = mc.main(
            ["--repo-root", str(tmp_path), "--generate-thresholds", "--include-hidden"]
        )
        assert code == mc.EXIT_OK
        data = json.loads(capsys.readouterr().out)
        # hidden prose = "secret prose" (12 chars), matching measure(include_hidden=True)
        comps = mc.measure(tmp_path, include_hidden=True)
        assert data["skills_prose"] == comps["skills_prose"]["bytes"]

    def test_write_thresholds_writes_file(self, repo_root: Path, tmp_path: Path, capsys):
        """`--write-thresholds <path>` writes a valid JSON file."""
        out_file = tmp_path / "thresholds-out.json"
        code = mc.main(
            ["--repo-root", str(repo_root), "--write-thresholds", str(out_file)]
        )
        assert code == mc.EXIT_OK
        assert out_file.exists(), "--write-thresholds should create the file"
        data = json.loads(out_file.read_text(encoding="utf-8"))
        for component in ("global_agents", "project_agents", "skills_prose", "total"):
            assert component in data

    def test_write_thresholds_contents_match_measured(
        self, repo_root: Path, tmp_path: Path, capsys
    ):
        """The written file contains the measured byte values."""
        out_file = tmp_path / "thresholds-out.json"
        mc.main(["--repo-root", str(repo_root), "--write-thresholds", str(out_file)])
        data = json.loads(out_file.read_text(encoding="utf-8"))
        comps = mc.measure(repo_root)
        assert data["total"] == comps["total"]["bytes"]
        assert data["global_agents"] == 100
        assert data["project_agents"] == 40
        assert data["skills_prose"] == 31

    def test_mutually_exclusive_with_other_output_flags(
        self, repo_root: Path, capsys
    ):
        """`--generate-thresholds` and `--json` are mutually exclusive output modes."""
        try:
            mc.main(["--repo-root", str(repo_root), "--generate-thresholds", "--json"])
        except SystemExit:
            # argparse rejects mutually exclusive flags with exit 2
            pass
        else:
            raise AssertionError("expected SystemExit for mutually exclusive flags")
