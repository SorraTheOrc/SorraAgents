"""Regression test: no legacy bootstrap/import patterns in skill scripts (F3, SA-0MSWJ9ZEU001HDVT).

F2 (SA-0MSWJ9Z02005DP8X) made every ``skill/*/scripts/*.py`` self-contained:
the shared-import bootstrap resolves the skills root via
``Path(__file__).resolve().parents[2]`` and skill modules are imported as
top-level packages (``from shared.status_lifecycle import ...``), never via
the legacy ``skill.<name>`` prefix.

This regression has recurred in the parent's history (SA-0MSW6PG6Q002S4M6):
the old bootstrap used ``Path(__file__).resolve().parents[3]`` with
``from skill.X import ...``, which breaks any copy of the skill tree to a
fresh project (the file-depth changes and the ``skill`` package no longer
resolves). This test scans every ``skill/*/scripts/*.py`` and fails on
either legacy pattern so the regression cannot recur undetected.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skill"

# Legacy bootstrap: repo-root depth assumption (`parents[3]` or deeper).
# The F2-correct bootstrap uses exactly `parents[2]` (the skills root).
LEGACY_BOOTSTRAP_RE = re.compile(r"\.resolve\(\)\.parents\[\s*([3-9]|\d{2,})\s*\]")

# Legacy import prefix: `from skill.<name>` / `import skill.<name>`.
LEGACY_IMPORT_RE = re.compile(r"(?m)^\s*(?:from\s+skill\.|import\s+skill\.)")

# Canonical no-cross-repo-copy rule must be present in the guard helper so a
# missing-shared failure always points agents at the canonical install.
GUARD_HELPER = REPO_ROOT / "skill" / "import_guard.py"


def _all_script_files() -> list[Path]:
    """Every script under the repo's skill/*/scripts/ directories."""
    return sorted(SCRIPTS_DIR.glob("*/scripts/*.py"))


def _legacy_patterns(text: str) -> list[str]:
    """Return every legacy bootstrap/import pattern match found in *text*."""
    found: list[str] = []
    for match in LEGACY_BOOTSTRAP_RE.finditer(text):
        found.append(f"legacy parents[{match.group(1)}] bootstrap")
    found.extend(f"legacy import: {m.group(0).strip()}" for m in LEGACY_IMPORT_RE.finditer(text))
    return found


def test_no_legacy_bootstrap_or_import_patterns_in_scripts() -> None:
    """No skill script may use the legacy repo-depth bootstrap or skill. prefix."""
    offenders: list[str] = []
    for script in _all_script_files():
        text = script.read_text(encoding="utf-8")
        patterns = _legacy_patterns(text)
        if patterns:
            offenders.append(f"{script.relative_to(REPO_ROOT)}: {patterns}")
    assert not offenders, (
        "Legacy bootstrap/import patterns found in skill scripts: " + "; ".join(offenders)
    )


def test_guard_helper_exists_and_states_no_copy_rule() -> None:
    """The graceful-failure guard must exist and carry the no-copy rule."""
    assert GUARD_HELPER.is_file(), f"missing {GUARD_HELPER.relative_to(REPO_ROOT)}"
    text = GUARD_HELPER.read_text(encoding="utf-8")
    for required in (
        "Do NOT copy skill scripts between repositories",
        "skill_path",
        "sys.exit(1)",
    ):
        assert required in text, f"guard helper missing '{required}'"
    assert "could not be imported" in text