"""Regression sentinels for the defer-full-suite testing regime.

Covers SA-0MVECQUGF0064U81: during implementation only changed-scope tests
run; the full suite first runs at the dev-push integration gate (pre-push
hook) and again at the ship release gate.

Covers SA-0MWFJS3820032YB4: the dev-push gate's verdict is resolved by
``implement.py finish`` **before** it takes the exclusive main-checkout lock,
so the ~5 min suite never runs inside that window. The one full-scope run in
``finish`` is therefore the *gate's* run — tree-keyed and pre-lock — never a
second, independent finish gate.

Static greps guard the deliberate removals/insertions (matching the repo's
policy-sentinel style, cf. test_install_pi_hook_wiring.py); behaviour is
covered end-to-end by test_pre_push_test_gate.py (dev/main pushes),
test_release_full_suite_gate.py (release gate) and
skill/implement/tests/test_finish_main_checkout_lock.py (lock ordering).
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
        """implement.py must not run an independent full-suite finish gate."""
        source = IMPLEMENT_PY.read_text(encoding="utf-8")
        for marker in _REMOVED_MARKERS:
            assert marker not in source, f"removed marker still present: {marker!r}"
        # The precise finish-gate invocation must be gone (comments may still
        # reference `--scope full`, the deferred push/release gate).
        assert 'run_tests(worktree_path, scope="full")' not in source, (
            "finish must not invoke an unbound full-scope suite"
        )

    def test_full_scope_run_is_the_dev_push_gate_bound_to_the_would_be_tree(
        self,
    ) -> None:
        """SA-0MWFJS3820032YB4: the one full-scope run is tree-bound and pre-lock.

        A full-suite run in ``finish`` is only legitimate as the *dev-push
        gate's* verdict, resolved for the would-be-commit tree before the
        main-checkout lock. Anything else re-introduces the serialised suite
        (or a second, unbound finish gate).
        """
        source = IMPLEMENT_PY.read_text(encoding="utf-8")
        assert "def resolve_prelock_full_suite_verdict(" in source
        assert 'run_tests(worktree_path, scope="full", fingerprint=tree)' in source, (
            "the pre-lock full-suite run must be keyed on the would-be-commit "
            "tree so the pre-push hook can serve it"
        )
        prelock_call = source.index(
            "prelock_verdict = resolve_prelock_full_suite_verdict("
        )
        lock_call = source.index(
            "lock_fd, lock_refusal = acquire_main_checkout_lock("
        )
        assert prelock_call < lock_call, (
            "the full-suite verdict must be resolved before the main-checkout "
            "lock is acquired"
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
