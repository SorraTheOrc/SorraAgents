"""End-to-end tests for the pre-push hook's context-budget regression gate.

AC1 (SA-0MSN2LIOQ004TON1): a simulated push must run the context-budget gate
committed in ``.githooks/pre-push`` and block the push when
``measure_context.py`` reports a threshold exceed (exit 2).

The hook is executed exactly as git invokes it — inside a throwaway git repo
with a fake ``measure_context.py`` that records its argv and exits with a
configurable code. The scope-aware full-suite gate is bypassed with
``TEST_SCOPE_SKIP=1`` so these tests isolate the context-budget gate; the
full-suite gate itself is covered end-to-end by ``test_pre_push_test_gate.py``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, Path(__file__).resolve().parents[2]):
    if (candidate / ".githooks" / "pre-push").exists():
        REPO_ROOT = candidate
        break
HOOK_PATH = REPO_ROOT / ".githooks" / "pre-push"

# Fake measure_context.py: records the argv it was invoked with, then exits
# with the code supplied via MEASURE_EXIT (2 == threshold exceeded).
_FAKE_MEASURE = """#!/usr/bin/env python3
import os
import sys

marker = os.environ.get("MEASURE_MARKER")
if marker:
    with open(marker, "a", encoding="utf-8") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("MEASURE_EXIT", "0")))
"""


def _run_hook(
    tmp_path: Path,
    measure_exit: int = 0,
    with_measure: bool = True,
    with_thresholds: bool = True,
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the committed pre-push hook with a simulated ``dev`` push on stdin.

    Returns ``(result, marker)`` where *marker* is the fake measure tool's
    invocation record (absent when the gate never invoked it).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "measure-marker.txt"

    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    git_hooks = repo / ".git" / "hooks"
    hook_dst = git_hooks / "pre-push"
    hook_dst.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    hook_dst.chmod(0o755)

    if with_measure:
        measure = repo / "skill" / "context-audit" / "scripts" / "measure_context.py"
        measure.parent.mkdir(parents=True, exist_ok=True)
        measure.write_text(_FAKE_MEASURE, encoding="utf-8")

    if with_thresholds:
        thresholds = repo / "docs" / "dev" / "context-budget.thresholds.json"
        thresholds.parent.mkdir(parents=True, exist_ok=True)
        thresholds.write_text("{}\n", encoding="utf-8")

    env = {
        **os.environ,
        # Isolate the context-budget gate from the other hook stages.
        "BRANCH_POLICY_SKIP": "1",
        "WORKLOG_SKIP_PRE_PUSH": "1",
        "TEST_SCOPE_SKIP": "1",
        "MEASURE_MARKER": str(marker),
        "MEASURE_EXIT": str(measure_exit),
    }
    env.pop("CONTEXT_BUDGET_SKIP", None)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        ["sh", str(hook_dst)],
        cwd=str(repo),
        input="refs/heads/wl-SA-1-x 0000 refs/heads/dev 1111\n",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, marker


# ── AC1: threshold exceed blocks the push ───────────────────────────────────


class TestThresholdExceedBlocksPush:
    def test_threshold_exceed_blocks_dev_push(self, tmp_path: Path):
        """A threshold exceed (exit 2) must fail the simulated dev push."""
        result, marker = _run_hook(tmp_path, measure_exit=2)
        assert result.returncode == 1, (
            f"expected the push to be blocked, got {result.returncode}: "
            f"{result.stderr}"
        )
        assert "Context budget exceeded committed thresholds" in result.stderr, (
            f"expected context-budget error, stderr: {result.stderr}"
        )
        assert marker.exists(), "the context-budget gate must invoke measure_context.py"

    def test_within_budget_allows_dev_push(self, tmp_path: Path):
        """A within-budget measurement (exit 0) must not block the push."""
        result, marker = _run_hook(tmp_path, measure_exit=0)
        assert result.returncode == 0, (
            f"expected the push to proceed, got {result.returncode}: "
            f"{result.stderr}"
        )
        assert marker.exists(), "the context-budget gate must invoke measure_context.py"

    def test_gate_invokes_measure_with_expected_args(self, tmp_path: Path):
        """The gate must measure the pushed repo with committed thresholds."""
        _, marker = _run_hook(tmp_path, measure_exit=0)
        invocation = marker.read_text(encoding="utf-8")
        assert "--repo-root" in invocation, invocation
        assert "--include-hidden" in invocation, invocation
        assert "--thresholds" in invocation, invocation
        assert "context-budget.thresholds.json" in invocation, invocation


# ── Bypass and fail-open behaviour ──────────────────────────────────────────


class TestBypassAndFailOpen:
    def test_skip_bypasses_gate(self, tmp_path: Path):
        """CONTEXT_BUDGET_SKIP=1 must skip the gate even on a failing measure."""
        result, marker = _run_hook(
            tmp_path,
            measure_exit=2,
            env_overrides={"CONTEXT_BUDGET_SKIP": "1"},
        )
        assert result.returncode == 0, result.stderr
        assert not marker.exists(), "bypassed gate must not invoke measure_context.py"

    def test_fail_open_when_measure_absent(self, tmp_path: Path):
        """No measure_context.py (local or global) must fail open.  A HOME
        without the global skill removes the fallback path."""
        result, marker = _run_hook(
            tmp_path,
            with_measure=False,
            env_overrides={"HOME": str(tmp_path / "empty-home")},
        )
        assert result.returncode == 0, result.stderr
        assert not marker.exists()

    def test_fail_open_when_thresholds_absent(self, tmp_path: Path):
        """A missing thresholds file must fail open (gate does not run)."""
        result, marker = _run_hook(tmp_path, with_thresholds=False)
        assert result.returncode == 0, result.stderr
        assert not marker.exists()
