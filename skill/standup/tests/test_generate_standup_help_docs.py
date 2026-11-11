#!/usr/bin/env python3
# <!-- REFACTOR-SA-0MW6N2S7G008GRFX
# smell: formatting
# severity: low
# description: Shebang is present but file is not executable
# -->
"""Documentation sweep: the help text and README describe the impact-first contract.

The standup epic (SA-0MW6ER1NX0094QCG) moved the narrative out of the
deterministic script and into the invoking agent, and added an explicit
``## Impact`` section to work items. That leaves three documentation surfaces
which must say so (SA-0MW6EUX5L0081WSV):

  - ``generate_standup.py --help`` — printed straight from the module
    docstring — documents the structured ``impact`` field, where it comes from
    (``## Impact``) and the two-layer render contract: the script is the
    deterministic data layer, the invoking agent clusters and renders grouped
    paragraphs;
  - ``format_report()`` is documented as the deterministic fallback renderer
    (one impact-first line per item) used by scheduled, non-agent runs, so
    nobody mistakes it for the clustered narrative;
  - ``skill/standup/README.md`` describes the same contract and points at
    ``SKILL.md`` as the authoritative rendering instructions.

The behaviour pinned is the *observable* help output and the documented
contract, not the source text: the help text is run as a subprocess exactly as
an operator would run it.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
README = Path(__file__).resolve().parents[1] / "README.md"

#: Phrases from the retired flat-bullet template; none may resurface.
STALE_TEMPLATE_PHRASES = (
    "Work item description — primary use case",
    "## Report Structure",
)


def _help_output() -> str:
    """Run ``generate_standup.py --help`` and return what the operator sees."""
    assert SCRIPT.is_file(), f"missing standup script at {SCRIPT}"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    assert out.strip(), "--help printed nothing"
    return out


def _reload_standup():
    """Import a fresh generate_standup module from the repo skill tree."""
    spec = importlib.util.spec_from_file_location("generate_standup_help_docs", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── --help: structured impact data ─────────────────────────────────────


def test_help_documents_the_structured_impact_field():
    """AC3: ``--help`` names the ``impact`` field available in ``--json``."""
    out = _help_output()
    lowered = out.lower()

    assert "impact" in lowered, "--help never mentions impact"
    assert "## Impact" in out, "--help does not name the `## Impact` source section"
    # The field is documented as part of the structured JSON payload.
    assert "--json" in out
    assert "field" in lowered or "key" in lowered


def test_help_documents_missing_impact_yields_empty_field():
    """AC1/AC3: an absent ``## Impact`` section yields an empty field, not an error."""
    lowered = _help_output().lower()

    assert "empty" in lowered
    assert "fall back" in lowered or "fallback" in lowered


def test_help_documents_the_two_layer_render_contract():
    """AC3: the script is the data layer; the invoking agent clusters/renders."""
    lowered = _help_output().lower()

    assert "deterministic data layer" in lowered
    assert "invoking agent" in lowered
    assert "cluster" in lowered
    assert "paragraph" in lowered


def test_help_documents_the_impact_first_sentence_and_fallback():
    """AC1/AC3: one spoken sentence per item, Impact first, user story second."""
    out = _help_output()
    lowered = out.lower()

    assert "`## Impact`" in out
    assert "`Title — user story`" in out
    assert "one sentence per item" in lowered


def test_help_retires_the_flat_bullet_template():
    """AC5: the help text carries no reference to the old bullet template."""
    out = _help_output()

    for phrase in STALE_TEMPLATE_PHRASES:
        assert phrase not in out, f"stale template phrase in --help: {phrase!r}"


# ── format_report(): the deterministic fallback ────────────────────────


def test_format_report_is_documented_as_the_fallback_renderer():
    """AC1: the code says what ``format_report`` is *not* (the narrative)."""
    mod = _reload_standup()
    doc = (mod.format_report.__doc__ or "").strip()

    assert doc, "format_report() has no docstring"
    lowered = doc.lower()
    assert "fallback" in lowered
    assert "impact" in lowered
    assert "agent" in lowered
    assert "`## Impact`" in doc or "## Impact" in doc


# ── README.md: the same contract for a human reader ────────────────────


def test_readme_exists_and_points_at_the_skill_instructions():
    """AC2: the standup README documents the format and defers to SKILL.md."""
    assert README.is_file(), f"missing standup README at {README}"
    text = README.read_text(encoding="utf-8")

    assert "SKILL.md" in text
    assert "generate_standup.py" in text


def test_readme_describes_the_grouped_impact_first_format():
    """AC2: grouped paragraphs, impact-first sentences, no work item ids."""
    lowered = README.read_text(encoding="utf-8").lower()

    assert "grouped" in lowered
    assert "paragraph" in lowered
    assert "impact" in lowered
    assert "## Impact" in README.read_text(encoding="utf-8")
    assert "fall" in lowered  # "falls back to Title — user story"
    assert "never" in lowered and "id" in lowered


def test_readme_retires_the_flat_bullet_template():
    """AC5: the README describes the grouped format, not a bullet template."""
    text = README.read_text(encoding="utf-8")

    for phrase in STALE_TEMPLATE_PHRASES:
        assert phrase not in text, f"stale template phrase in README: {phrase!r}"
