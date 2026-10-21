"""Regression sentinels for the defer-full-suite testing regime.

Covers SA-0MVECQUGF0064U81: during implementation only changed-scope tests
run; the full suite first runs at the dev-push integration gate (pre-push
hook) and again at the ship release gate.

Static greps guard the deliberate removals/insertions (matching the repo's
policy-sentinel style, cf. test_install_pi_hook_wiring.py); behaviour is
covered end-to-end by test_pre_push_test_gate.py (dev/main pushes) and
test_release_full_suite_gate.py (release gate).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, Path(__file__).resolve().parents[2]):
    if (candidate / "skill" / "implement" / "scripts" / "implement.py").exists():
        REPO_ROOT = candidate
        break

IMPLEMENT_PY = REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
RELEASE_SCRIPT = REPO_ROOT / "skill" / "ship" / "scripts" / "release" / "merge-dev-to-main.sh"
PRE_PUSH_HOOK = REPO_ROOT / ".githooks" / "pre-push"
IMPLEMENT_SKILL = REPO_ROOT / "skill" / "implement" / "SKILL.md"
TEST_SKILL = REPO_ROOT / "skill" / "test" / "SKILL.md"

# Markers that must NO LONGER exist (the removed finish full-suite gate).
_REMOVED_MARKERS = (
    "final-full",
    "Running final full-suite gate",
    "Final full-suite gate",
)
# Stale doc phrasing that must no longer appear anywhere in the skill docs.
_STALE_DOC_PHRASES = (
    "final full-suite gate",
    "full-suite gate before commit",
    "runs a final `--scope full` gate before commit",
)


class TestFinishFullGateRemoved:
    def test_no_full_gate_marker_in_implement_py(self) -> None:
        """implement.py must not run a full-suite gate during finish."""
        source = IMPLEMENT_PY.read_text(encoding="utf-8")
        for marker in _REMOVED_MARKERS:
            assert marker not in source, f"removed marker still present: {marker!r}"
        # The precise finish-gate invocation must be gone (comments may still
        # reference `--scope full`, the deferred push/release gate).
        assert 'run_tests(worktree_path, scope="full")' not in source, (
            "finish must not invoke a full-scope suite"
        )

    def test_stale_doc_phrasing_removed(self) -> None:
        """No skill doc may still describe a pre-commit full-suite gate."""
        docs = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (IMPLEMENT_SKILL, TEST_SKILL)
        )
        for phrase in _STALE_DOC_PHRASES:
            assert phrase not in docs, f"stale doc phrasing present: {phrase!r}"


class TestIntegrationGatesPresent:
    def test_pre_push_hook_still_runs_full_suite(self) -> None:
        """AC2: the dev-push hook keeps the full-suite integration gate."""
        hook = PRE_PUSH_HOOK.read_text(encoding="utf-8")
        assert "--scope full" in hook
        assert "refs/heads/dev|main" in hook or "dev|main" in hook

    def test_release_script_has_merged_tree_gate(self) -> None:
        """AC4: the release script gates the merged tree before the release."""
        script = RELEASE_SCRIPT.read_text(encoding="utf-8")
        assert "--scope full --strict-git-env" in script
        assert "SHIP_TEST_SKIP" in script
        assert "the release is blocked" in script
