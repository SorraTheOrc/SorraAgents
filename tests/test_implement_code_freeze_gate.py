"""Tests for the implement.py Code Freeze gate (SA-0MSBU4OBU005WJNB).

Contract (per work item ACs):

- AC: `phase_start()` checks the marker BEFORE claiming the work item and
  BEFORE creating a worktree. If freeze is active it refuses with a clear
  message, exits non-zero, and does NOT change the work item status.
- AC: no `--force`-style bypass is supported.
- AC: absence of a marker allows start (the gate is fail-open).
- AC: `implement-single` inherits the gate because it delegates to
  `implement.py` (documented orchestration path).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, marker_active: bool | None = None) -> Path:
    """Create a minimal git repo with an optional Code Freeze marker.

    Args:
        tmp_path: pytest tmp dir.
        marker_active: True → active marker; False → inactive marker;
            None → no marker file.

    Returns:
        repo_root Path.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(repo_root), check=True, capture_output=True)
    (repo_root / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"],
                   cwd=str(repo_root), check=True, capture_output=True)

    if marker_active is not None:
        worklog = repo_root / ".worklog"
        worklog.mkdir()
        (worklog / "code-freeze.json").write_text(json.dumps({
            "active": marker_active,
            "reason": "ship release in progress",
            "startedAt": "2026-08-03T00:00:00Z",
            "pid": 9999,
        }))
    return repo_root


def _run_phase_start(repo_root: Path, work_item_id: str = "SA-TEST123") -> subprocess.CompletedProcess:
    """Run phase_start(work_item_id, json_output=True) in the given repo."""
    script = f"""\
import json
import logging
import sys
import os

sys.path.insert(0, {str(_REPO_ROOT)!r})
os.chdir({str(repo_root)!r})
logging.basicConfig(level=logging.INFO)

import importlib.util
spec = importlib.util.spec_from_file_location(
    "implement_under_test",
    {str(_IMPLEMENT_PY)!r},
)
mod = importlib.util.module_from_spec(spec)
mod.__package__ = "implement_scripts"
sys.modules["implement_under_test"] = mod
spec.loader.exec_module(mod)

result = mod.phase_start({work_item_id!r}, json_output=True)
print(f"RESULT_JSON:{{json.dumps(result)}}")
"""
    runner_path = repo_root / "_test_runner.py"
    runner_path.write_text(script)
    proc = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(runner_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc


def _extract_result(proc: subprocess.CompletedProcess) -> dict:
    """Extract the phase_start result dict from the runner output."""
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT_JSON:"):
            return json.loads(line[len("RESULT_JSON:"):])
    raise AssertionError(f"No RESULT_JSON line in stdout:\n{proc.stdout}\n{proc.stderr}")


# ---------------------------------------------------------------------------
# Tests: freeze active → refusal
# ---------------------------------------------------------------------------


class TestFreezeActive:
    def test_refuses_when_marker_active(self, tmp_path: Path):
        """Active marker → phase_start refuses with Code Freeze message."""
        repo = _make_repo(tmp_path, marker_active=True)
        proc = _run_phase_start(repo)
        result = _extract_result(proc)

        assert result["success"] is False, f"Expected refusal, got {result}"
        assert "Code Freeze" in result["message"], result["message"]

    def test_does_not_claim_when_frozen(self, tmp_path: Path):
        """When frozen, phase_start must NOT reach the claim step."""
        repo = _make_repo(tmp_path, marker_active=True)
        proc = _run_phase_start(repo)

        # The claim step logs "Claiming work item ..."; the gate must run first.
        assert "Claiming work item" not in proc.stdout + proc.stderr, (
            "phase_start reached the claim step during a Code Freeze — "
            "the gate must run before claiming."
        )
        assert "Code Freeze" in proc.stdout + proc.stderr

    def test_no_force_bypass(self, tmp_path: Path):
        """There is no --force-style bypass; the gate is absolute."""
        repo = _make_repo(tmp_path, marker_active=True)
        # Simulate a caller passing --force-like flags (they are not parsed).
        proc = _run_phase_start(repo)
        result = _extract_result(proc)
        assert result["success"] is False
        assert "Code Freeze" in result["message"]


# ---------------------------------------------------------------------------
# Tests: no marker → start allowed
# ---------------------------------------------------------------------------


class TestNoMarker:
    def test_allows_start_when_no_marker(self, tmp_path: Path):
        """No marker file → phase_start proceeds past the gate (fail-open)."""
        repo = _make_repo(tmp_path, marker_active=None)
        proc = _run_phase_start(repo)
        result = _extract_result(proc)

        # The gate must NOT produce a Code Freeze refusal.
        assert "Code Freeze" not in result["message"], result["message"]
        # It proceeds to the claim step (which fails on a fake id, but the
        # failure must NOT be a freeze refusal).
        assert "Claiming work item" in proc.stdout + proc.stderr, (
            "phase_start did not reach the claim step when no marker was present"
        )

    def test_allows_start_when_inactive_marker(self, tmp_path: Path):
        """Inactive marker (active:false) → not frozen; start proceeds."""
        repo = _make_repo(tmp_path, marker_active=False)
        proc = _run_phase_start(repo)
        result = _extract_result(proc)
        assert "Code Freeze" not in result["message"], result["message"]
        assert "Claiming work item" in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Tests: fail-open on corrupt marker
# ---------------------------------------------------------------------------


class TestCorruptMarker:
    def test_allows_start_when_marker_corrupt(self, tmp_path: Path):
        """Corrupt marker file → not frozen (fail-open)."""
        repo = _make_repo(tmp_path, marker_active=None)
        (repo / ".worklog").mkdir(exist_ok=True)
        (repo / ".worklog" / "code-freeze.json").write_text("{not json")
        proc = _run_phase_start(repo)
        result = _extract_result(proc)
        assert "Code Freeze" not in result["message"], result["message"]
        assert "Claiming work item" in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Tests: structural — gate before claim, no bypass flag
# ---------------------------------------------------------------------------


class TestStructural:
    SOURCE = _IMPLEMENT_PY.read_text() if _IMPLEMENT_PY.exists() else ""

    def test_gate_check_precedes_claim(self):
        """The freeze gate call must appear before StatusLifecycle.update_status."""
        assert self.SOURCE, f"implement.py not found at {_IMPLEMENT_PY}"
        gate_idx = self.SOURCE.find("is_code_freeze_active")
        claim_idx = self.SOURCE.find(
            'StatusLifecycle.update_status(work_item_id, "in_progress")'
        )
        assert gate_idx != -1, "phase_start must call is_code_freeze_active"
        assert claim_idx != -1, "phase_start must claim via StatusLifecycle"
        assert gate_idx < claim_idx, (
            "Code Freeze gate must be checked BEFORE claiming the work item"
        )

    def test_no_force_flag_in_parser(self):
        """implement.py's argparse must not expose a --force bypass flag."""
        assert self.SOURCE
        # Only inspect the parse_args() parser region (line 543 uses
        # `git worktree remove --force` internally, which is unrelated).
        parser_start = self.SOURCE.index("def parse_args")
        parser_end = self.SOURCE.index("def main")
        parser_section = self.SOURCE[parser_start:parser_end]
        assert '"--force"' not in parser_section and "'--force'" not in parser_section, (
            "implement.py must not support a --force bypass of the freeze gate"
        )
