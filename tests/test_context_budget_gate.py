"""Regression gate test for the pi session startup context budget.

F6 (SA-0MSLK7XNZ00366YY) of the context-reduction epic
(SA-0MSJI53RX006E2PS): the F1 measurement tooling
(skill/context-audit/scripts/measure_context.py) ships a non-zero-exit
regression gate. This test enforces the committed thresholds in
docs/dev/context-budget.thresholds.json on every full-suite run, so a PR/commit
that regresses the startup context surface fails CI and pre-push test runs.

Related: F1 (SA-0MSLK72KI006N9KP) measurement tooling + baseline.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MEASURE = (
    REPO_ROOT / "skill" / "context-audit" / "scripts" / "measure_context.py"
)
THRESHOLDS = REPO_ROOT / "docs" / "dev" / "context-budget.thresholds.json"


def test_context_budget_gate_within_committed_thresholds() -> None:
    """The measured startup context must not exceed the committed thresholds.

    Runs the F1 gate exactly as CI would: exit code 0 = within budget,
    exit code 2 = a threshold was exceeded.
    """
    assert MEASURE.exists(), f"measure_context.py not found at {MEASURE}"
    assert THRESHOLDS.exists(), f"thresholds file not found at {THRESHOLDS}"

    result = subprocess.run(
        [
            sys.executable,
            str(MEASURE),
            "--include-hidden",
            "--thresholds",
            str(THRESHOLDS),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        "Context budget exceeded committed thresholds "
        f"(exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )


def test_context_budget_thresholds_file_is_valid() -> None:
    """The thresholds file must parse and contain the expected components."""
    data = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    for component in ("global_agents", "project_agents", "skills_prose", "total"):
        assert component in data, f"thresholds missing component {component!r}"
        assert isinstance(data[component], int) and data[component] > 0, (
            f"threshold {component!r} must be a positive integer"
        )
