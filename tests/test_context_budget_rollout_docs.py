"""Rollout-documentation integrity for the context-budget pre-push gate.

SA-0MSN2ULOF007JJ35 wired the last in-scope framework project (AI_Hell) into
the v2 context-budget pre-push gate and refreshed the rollout record in
``docs/dev/context-budget.md``.

This guards the record itself: every framework project that ships the gate must
appear in the rollout table with the gate's wiring artifacts, and the
non-SorraAgents measurement-scope caveat must stay documented. The gate's
behaviour is asserted (not re-implemented) by
``tests/test_pre_push_context_budget_gate.py`` and
``tests/test_pre_push_hook_fallback.py``; following the repo's existing
documentation tests (e.g. ``tests/test_readme_project_positioning.py``,
``tests/test_skill_path_conventions.py``) this module asserts the documented
claims only.
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC = _REPO_ROOT / "docs" / "dev" / "context-budget.md"

# Framework projects that ship the v2 gate. SorraAgents itself runs v3 (v2 plus
# the repo-local full-suite gate) and is covered by the same table.
_WIRED_PROJECTS = (
    "SorraAgents",
    "ContextHub",
    "dev-scripts",
    "llm-manager",
    "open_source_llm",
    "Tableau-Card-Engine",
    "AI_Hell",
)


def _read_doc() -> str:
    assert _DOC.exists(), f"rollout documentation missing: {_DOC}"
    return _DOC.read_text(encoding="utf-8")


def _rollout_table_rows(doc: str) -> dict[str, str]:
    """Return ``{project: row}`` for rollout-table rows naming a wired project."""
    rows: dict[str, str] = {}
    for line in doc.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] in _WIRED_PROJECTS:
            rows[cells[0]] = line
    return rows


def test_rollout_table_lists_every_wired_project() -> None:
    """AC3: every in-scope framework project is recorded in the rollout table."""
    rows = _rollout_table_rows(_read_doc())
    missing = [project for project in _WIRED_PROJECTS if project not in rows]
    assert not missing, f"rollout table missing wired project(s): {missing}"


def test_rollout_rows_name_hook_and_thresholds_artifacts() -> None:
    """Each wired project's row records its hook directory and thresholds file."""
    rows = _rollout_table_rows(_read_doc())
    for project in _WIRED_PROJECTS:
        row = rows[project]
        assert ".githooks" in row or ".git/hooks" in row, row
        assert "context-budget.thresholds.json" in row, row


def test_non_sorragents_measurement_scope_is_documented() -> None:
    """The doc explains why sibling projects measure 0 global/skills bytes.

    ``install_pi.sh`` symlinks the global guidance to ``~/.pi/agent/AGENTS.md``
    (machine-global), not into each repository, so each project's gate protects
    its repo-local surface only. Without this note the 0-byte components look
    like a wiring bug.
    """
    doc = _read_doc()
    assert "machine-global" in doc
    assert "global_agents = 0" in doc or "global_agents 0" in doc


def test_aihell_end_to_end_verification_is_recorded() -> None:
    """AC6: the real-project simulated-push verification is recorded."""
    doc = _read_doc()
    assert "End-to-end verification" in doc
    assert "Context budget exceeded committed thresholds" in doc
