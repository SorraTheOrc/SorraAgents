"""Regression guard for the residual ruff findings (SA-0MWRQU7LC008HJBN).

Two rule families survived the refactoring epic (SA-0MW54FB49007Q7OT) and were
only caught by an audit run over the tracked files afterwards:

* ``RUF059`` — unpacked variables that are never used. A `_`-prefix is the
  documented intent signal; without it the next reader cannot tell an unused
  fixture value from a value whose use was accidentally deleted.
* ``PLW1510`` — ``subprocess.run`` without an explicit ``check=`` argument.
  The implicit default (``check=False``) hides a failing command, so the
  argument must always be spelled out.

Neither is visible to pytest — the affected suites stay green either way — so
the linter is the only thing that can catch a relapse. This guard pins the
exact rule/file pairs the chore cleared, mirroring the ISC004 guard in
``tests/test_measure_slot_wait.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# (rule, repo-relative file) pairs cleared by SA-0MWRQU7LC008HJBN.
GUARDED = (
    ("RUF059", "skill/implement/tests/test_start_base_resolution.py"),
    ("RUF059", "skill/implement/tests/test_drive_sibling_worktree_stray.py"),
    ("PLW1510", "skill/audit/tests/test_audit_runner_contaminated_launch.py"),
)


@pytest.mark.skipif(
    shutil.which("ruff") is None, reason="ruff is not installed"
)
@pytest.mark.parametrize(("rule", "relative_path"), GUARDED)
def test_guarded_file_is_free_of_its_rule(rule: str, relative_path: str) -> None:
    """The file carries no finding for the rule the chore cleared."""
    target = REPO_ROOT / relative_path
    assert target.exists(), f"guarded file is missing: {target}"

    result = subprocess.run(
        [
            shutil.which("ruff") or "ruff",
            "check",
            "--isolated",
            "--select",
            rule,
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"{rule} findings remain in {relative_path}:\n"
        f"{result.stdout}{result.stderr}"
    )
    assert rule not in result.stdout + result.stderr
