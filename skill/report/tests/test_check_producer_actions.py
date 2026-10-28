
# <!-- REFACTOR-SA-0MVRBPY7J004GJAI
# smell: unknown
# severity: low
# description: Unparenthesized implicit string concatenation in collection
# -->
"""Tests for the next-step producer-action template consistency check.

The check (``skill/report/scripts/check_producer_actions.py``) is the
deterministic acceptance gate for the report-contract change
(SA-0MVNQ3RLW006XEFW AC5 / SA-0MVQ1QK1R0052WGP): no ``skill/*/SKILL.md``
final-step report template may instruct or exemplify a *routine next-step*
producer action ("Proceed to /skill:plan", "review the brief, then run
/skill:plan", "Next step: implement"), because routine workflow progression
belongs in the report's ``Ready for <next_action>`` conclusion line.

Coverage:

- AC1/AC4 — the scanner reports zero matches on the real tree and yields a
  single unambiguous pass/fail (in-process and via the documented CLI/JSON
  command).
- AC2 — every verb/target/lead-in in the shared routine-next-step vocabulary
  (``skill/shared/herdr_pane.py``) is detected, proving the check mirrors the
  shared classifier vocabulary and stays in sync with it.
- AC3 — offline, deterministic, pure-Python: the check only reads the real
  markdown tree (no network, no ``wl``) and always returns the same verdict.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "skill" / "report" / "scripts"
SHARED_DIR = REPO_ROOT / "skill" / "shared"
for _path in (SCRIPTS_DIR, SHARED_DIR, REPO_ROOT / "skill", REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import check_producer_actions as cpa
from shared import herdr_pane

from tests.skill_inventory import tracked_skill_names

CLI = SCRIPTS_DIR / "check_producer_actions.py"


def _tracked_skill_files() -> list[Path]:
    """Every **tracked** ``skill/*/SKILL.md`` under the repo.

    Untracked ``skill/<name>/`` dirs are a concurrent agent's uncommitted
    WIP and must not turn this gate red (SA-0MUU206QT001M66F); fall back to
    scanning every directory when git is unavailable.
    """
    files = sorted((REPO_ROOT / "skill").glob("*/SKILL.md"))
    tracked = tracked_skill_names(REPO_ROOT)
    if tracked is None:
        return files
    return [f for f in files if f.parent.name in tracked]


def _tracked_skill_tree(tmp_path: Path) -> Path:
    """A temp ``skill/`` tree symlinking the tracked SKILL.md files, so CLI
    runs against it are immune to concurrent untracked WIP."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    for path in _tracked_skill_files():
        sub_dir = skill_dir / path.parent.name
        sub_dir.mkdir()
        (sub_dir / "SKILL.md").symlink_to(path)
    return skill_dir


FIXTURE_TEMPLATE = """\
# Demo skill

## 9. Finishing

Unrelated guidance that mentions no producer actions.

## Final step: standardized end-of-session report

Render the canonical report:

```bash
python3 $(skill_path report)/scripts/render_report.py <id> \\
  --skill-name demo \\
{producer_line}
```

## Related

- --producer-actions "Proceed to /skill:ship" (outside the final-step block)
"""


def _template(producer_line: str) -> str:
    return FIXTURE_TEMPLATE.format(producer_line=producer_line)


# ---------------------------------------------------------------------------
# Final-step block extraction
# ---------------------------------------------------------------------------


def test_extracts_final_step_block_and_bounds_it_at_same_level_heading():
    text = _template('  --ac "1|x|met"')
    start_line, block_lines = cpa.extract_final_step_block(text)
    header = "## Final step: standardized end-of-session report"
    assert block_lines[0] == header
    assert start_line == text.splitlines().index(header) + 1
    # The same-level "## Related" heading terminates the block, so the
    # out-of-block producer-actions line is never scanned.
    assert "## Related" not in block_lines


def test_skill_without_final_step_block_is_out_of_scope():
    assert cpa.extract_final_step_block("# Helper\n\nNo report template.\n") is None


# ---------------------------------------------------------------------------
# Detection of routine next-step producer actions (AC1/AC2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "producer_line",
    [
        '  --producer-actions "Proceed to /skill:plan" \\',
        '  --producer-actions "Review the intake brief and then run /skill:plan" \\',
        '  --producer-actions "Next step: implement" \\',
        '  --producer-actions "Continue with the next stage" \\',
        '  --producer-actions "Then start planning" \\',
    ],
)
def test_detects_next_step_producer_action_examples(producer_line):
    block = cpa.extract_final_step_block(_template(producer_line))[1]
    matches = cpa.scan_block(block)
    assert len(matches) == 1
    assert matches[0].text == producer_line.strip()


@pytest.mark.parametrize(
    "producer_line",
    [
        # Current template wording (pre-contract-tightening).
        '  [--producer-actions "<actions for the producer, or omit for '
        '\'None needed\'>"] \\',
        # Tightened wording the doc change introduces.
        '  [--producer-actions "<blocking, current-stage actions only; omit '
        "for 'None needed'>\"] \\",
        # Explicit no-action value.
        "  --producer-actions \"None needed\" \\",
        # Genuine blocking actions must survive verbatim (no over-eager match).
        '  --producer-actions "Answer the OPEN QUESTION in comment SA-C123 '
        'before /skill:plan can start" \\',
        '  --producer-actions "Approve the remediation list in comment #4" \\',
    ],
)
def test_compliant_guidance_is_not_flagged(producer_line):
    block = cpa.extract_final_step_block(_template(producer_line))[1]
    assert cpa.scan_block(block) == []


def test_lines_unrelated_to_producer_actions_are_outside_the_check_scope():
    """Block prose about the renderer is not producer-action guidance."""
    block = cpa.extract_final_step_block(
        _template('  --ac "1|x|met"')
    )[1] + ["Run the renderer, then run /skill:ship for the release step."]
    assert cpa.scan_block(block) == []


# ---------------------------------------------------------------------------
# Shared vocabulary sync (AC2)
# ---------------------------------------------------------------------------


def test_vocabulary_is_imported_from_the_shared_classifier_module():
    """Single source of truth: the check consumes herdr_pane's constants."""
    assert cpa.ROUTINE_STEP_VERBS is herdr_pane.ROUTINE_STEP_VERBS
    assert cpa.ROUTINE_STEP_TARGETS is herdr_pane.ROUTINE_STEP_TARGETS
    assert cpa.ROUTINE_STEP_LEADINS is herdr_pane.ROUTINE_STEP_LEADINS


@pytest.mark.parametrize("verb", herdr_pane.ROUTINE_STEP_VERBS)
@pytest.mark.parametrize("target", herdr_pane.ROUTINE_STEP_TARGETS)
def test_every_shared_verb_target_pair_is_detected(verb, target):
    line = f'  --producer-actions "{verb} {target}" \\'
    block = cpa.extract_final_step_block(_template(line))[1]
    assert cpa.scan_block(block), (
        f"shared vocabulary pair not detected: {verb!r} + {target!r}"
    )


@pytest.mark.parametrize("lead_in", herdr_pane.ROUTINE_STEP_LEADINS)
@pytest.mark.parametrize("target", herdr_pane.ROUTINE_STEP_TARGETS)
def test_every_shared_lead_in_target_pair_is_detected(lead_in, target):
    line = f'  --producer-actions "{lead_in}: {target}" \\'
    block = cpa.extract_final_step_block(_template(line))[1]
    assert cpa.scan_block(block), (
        f"shared lead-in not detected: {lead_in!r} + {target!r}"
    )


# ---------------------------------------------------------------------------
# Real tree: the acceptance gate (AC1/AC4)
# ---------------------------------------------------------------------------


def test_real_skill_templates_contain_no_next_step_producer_actions():
    template_skills: list[str] = []
    matches: list[cpa.Match] = []
    for path in _tracked_skill_files():
        name, found = cpa.scan_skill_file(path)
        if name is None:
            continue
        template_skills.append(name)
        matches.extend(found)
    assert matches == [], (
        "next-step producer actions found in skill templates: "
        + ", ".join(f"{m.line}: {m.text}" for m in matches)
    )
    # Every skill that carries the report template is scanned: 17 work-item
    # skills (16 work-item skills + the report helper) at the time of writing.
    assert len(template_skills) >= 17
    assert "report" in template_skills


def test_every_template_with_a_producer_actions_line_is_scanned():
    """Extraction never silently skips a template that carries the contract."""
    carrying = set()
    scanned = set()
    for path in _tracked_skill_files():
        if "--producer-actions" in path.read_text(encoding="utf-8"):
            carrying.add(path.parent.name)
        name, _ = cpa.scan_skill_file(path)
        if name is not None:
            scanned.add(name)
    assert carrying == scanned


def test_cli_reports_a_clean_pass_offline(tmp_path):
    skill_dir = _tracked_skill_tree(tmp_path)
    result = subprocess.run(
        [sys.executable, str(CLI), "--json", "--skill-dir", str(skill_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["success"] is True
    assert payload["match_count"] == 0
    assert payload["templates_scanned"] >= 17


def test_cli_fails_loudly_and_names_the_offending_template(tmp_path):
    skill_dir = tmp_path / "skill"
    (skill_dir / "demo").mkdir(parents=True)
    (skill_dir / "demo" / "SKILL.md").write_text(
        _template('  --producer-actions "Proceed to /skill:plan" \\'),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(CLI), "--json", "--skill-dir", str(skill_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["success"] is False
    assert payload["match_count"] == 1
    assert payload["matches"][0]["skill"] == "demo"
    assert payload["matches"][0]["phrase"] == "Proceed to /skill:plan"
