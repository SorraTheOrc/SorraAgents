"""Doc-contract tests for the worktree-isolation guidance.

Verifies SA-0MUY9PSRS003V5CM (AC1/AC5/AC6): driven/child sessions must verify
their worktree root before the first write, every write/edit path must resolve
inside the worktree, and the ``wl``-needs-main-checkout vs edits-need-worktree
split is documented with the worktree-safe ``wl`` invocation.

The deliverable is documentation, so these tests assert the documented
contract, in the established style of ``tests/test_agents_global_session_per_child.py``
and ``tests/test_implement_skill_session_per_child_doc.py``.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_AGENTS_GLOBAL = _REPO_ROOT / "AGENTS_GLOBAL.md"
_README = _REPO_ROOT / "README.md"
_ISOLATION_DOC = _REPO_ROOT / "docs" / "dev" / "worktree-isolation.md"


def _read(path: Path) -> str:
    assert path.exists(), f"File not found: {path}"
    return path.read_text(encoding="utf-8")


def _skill_worktree_block() -> str:
    """The Step 5 MANDATORY worktree-requirement block, normalised."""
    content = _read(_SKILL_MD)
    marker = "**MANDATORY — worktree requirement:**"
    start = content.find(marker)
    assert start != -1, f"{_SKILL_MD.name} must contain {marker!r}"
    # The Step 5 blockquote runs until the node_modules auto-symlink note.
    end = content.find("> **`node_modules` is auto-symlinked:**", start)
    assert end != -1 and end > start, "Step 5 block must precede the node_modules note"
    return " ".join(content[start:end].split())


class TestSkillStep5WorktreeVerification:
    """AC1: SKILL.md Step 5 requires verifying the worktree root."""

    def test_cwd_not_sufficient(self) -> None:
        assert "`cwd` is not sufficient" in _skill_worktree_block()

    def test_verify_command_documented(self) -> None:
        block = _skill_worktree_block()
        assert "git rev-parse --show-toplevel" in block
        assert "IMPLEMENT_WORKTREE_PATH" in block

    def test_forbids_absolute_main_checkout_paths(self) -> None:
        block = _skill_worktree_block()
        assert "absolute path under the main checkout" in block

    def test_cross_links_isolation_doc(self) -> None:
        assert "docs/dev/worktree-isolation.md" in _skill_worktree_block()


class TestWorktreeIsolationDoc:
    """AC5: the doc explains the wl-vs-edits split and worktree-safe wl."""

    def test_documents_wl_needs_main_checkout_split(self) -> None:
        content = " ".join(_read(_ISOLATION_DOC).split())
        assert "main checkout" in content
        assert "edits stay in the worktree" in content or "edits live in the worktree" in content

    def test_documents_worktree_safe_wl_invocation(self) -> None:
        content = " ".join(_read(_ISOLATION_DOC).split())
        assert "wl --worklog-dir" in content

    def test_documents_verify_command(self) -> None:
        content = " ".join(_read(_ISOLATION_DOC).split())
        assert "git rev-parse --show-toplevel" in content
        assert "IMPLEMENT_WORKTREE_PATH" in content

    def test_warns_against_cd_to_main_to_edit(self) -> None:
        content = " ".join(_read(_ISOLATION_DOC).split())
        assert "DO NOT DO THIS" in content
        assert "cd" in content


class TestCrossLinks:
    """AC6: related documentation points at the isolation doc."""

    def test_readme_links_isolation_doc(self) -> None:
        assert "docs/dev/worktree-isolation.md" in _read(_README)

    def test_agents_global_links_isolation_doc(self) -> None:
        assert "docs/dev/worktree-isolation.md" in _read(_AGENTS_GLOBAL)


class TestMainCheckoutGuardedSurface:
    """WL-0MVPADZ1Z000FOSG: the main checkout is documented as a guarded surface.

    The normative section is the audit-facing contract for the 2026-10-26
    main-checkout wipe, so it is pinned like the rest of this document: the
    allow-list, one enforcement point per refused mutation class, the RCA
    link, the residual-risk statement and the recovery procedure must all be
    present and consistent with the implemented guards.
    """

    def _section(self) -> str:
        content = _read(_ISOLATION_DOC)
        start = content.find("## The main checkout is a guarded surface")
        assert start != -1, (
            "worktree-isolation.md must contain the normative "
            "'The main checkout is a guarded surface' section"
        )
        end = content.find("## Recovery when work landed in the main checkout", start)
        assert end != -1 and end > start, "the normative section must precede the recovery section"
        return content[start:end]

    def test_documents_the_normative_invariant(self) -> None:
        content = " ".join(_read(_ISOLATION_DOC).split())
        assert "Normative invariant" in content
        assert "MUST NOT mutate the **main checkout**" in content

    def test_documents_the_allow_list(self) -> None:
        section = self._section()
        assert "Legitimate mutations (allow-list)" in section
        # The two legitimate finish/wl mutations are named explicitly.
        assert "Fast-forward of the main checkout's `dev`" in section
        assert "Writes under `<main-checkout>/.worklog/`" in section
        assert "expected-dirty" in section

    def test_documents_each_enforcement_point_with_its_test(self) -> None:
        section = " ".join(self._section().split())
        for enforcement in (
            "guarded_run_all",
            "live_repo_guard",
            "main-checkout guard",
            "main-checkout.lock",
            "post-push ancestry assertion",
            "push-failure restore",
            "placement guard",
            "hygiene_check.sh",
            "shared workspace build output",
        ):
            assert enforcement in section, f"enforcement point missing: {enforcement}"
        for test_pin in (
            "test_finish_main_checkout_guard.py",
            "test_finish_main_checkout_lock.py",
            "test_implement_finish_local_dev_verified_sync.py",
            "test_guarded_run_all.py",
            "test_implement_stash_hygiene.py",
            "missing-shared-dist.test.ts",
        ):
            assert test_pin in section, f"test pin missing: {test_pin}"

    def test_links_the_rca_and_states_residual_risk(self) -> None:
        section = " ".join(self._section().split())
        assert "rca-main-checkout-wipe.md" in section
        assert "Residual risk" in section
        assert "no single command could be reproduced" in section

    def test_documents_the_recovery_procedure(self) -> None:
        content = " ".join(_read(_ISOLATION_DOC).split())
        assert "## Recovery: the main checkout was mutated" in content
        assert "git checkout -- packages/shared" in content
        assert "npm run build:shared" in content
        assert "scripts/hygiene_check.sh" in content

    def test_references_finish_report_keys(self) -> None:
        doc = _read(_REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md")
        assert 'report["steps"]["main_checkout_guard"]' in doc
        assert 'report["steps"]["main_checkout_lock"]' in doc
        assert 'report["steps"]["local_dev_fast_forward"]' in doc
        assert 'report["steps"]["local_dev_restore"]' in doc
        assert 'report["steps"]["ancestry_assertion"]' in doc


_REFERENCE_DOC = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"


class TestWorktreeRemovalOwnership:
    """SA-0MVTE8XAJ002IUDR AC8: the removal-ownership rule is documented.

    A session must never be told to delete a worktree it is standing in. The
    contract is stated in three places — the skill's finish step, the
    implementation reference and the isolation doc — and the retention
    mechanism (`--keep-worktree`, driver-side removal, the retained-worktree
    report) is named in each.
    """

    def _skill_finish_block(self) -> str:
        content = _read(_SKILL_MD)
        start = content.find("After pushing, clean up the worktree")
        assert start != -1, (
            "SKILL.md must keep a finish-step worktree-cleanup instruction"
        )
        end = content.find("**Why rebuild?**", start)
        assert end != -1 and end > start, "the cleanup block must precede the rebuild note"
        return " ".join(content[start:end].split())

    def _reference_section(self) -> str:
        content = _read(_REFERENCE_DOC)
        start = content.find("## Worktree removal ownership (SA-0MVTE8XAJ002IUDR)")
        assert start != -1, (
            "implement-skill-reference.md must contain the "
            "'Worktree removal ownership' section"
        )
        end = content.find("## Drive failure recovery", start)
        assert end != -1 and end > start, "the ownership section must precede drive recovery"
        return content[start:end]

    def _isolation_section(self) -> str:
        content = _read(_ISOLATION_DOC)
        start = content.find("## Worktree removal ownership")
        assert start != -1, "worktree-isolation.md must contain the ownership section"
        end = content.find("## The placement guard", start)
        assert end != -1 and end > start, "the ownership section must precede the placement guard"
        return content[start:end]

    def test_skill_state_driven_session_must_not_remove_its_worktree(self) -> None:
        block = self._skill_finish_block()
        assert "IMPLEMENT_DRIVE_ACTIVE" in block
        assert "do not remove your own worktree" in block.lower()
        assert "implement.py drive" in block

    def test_skill_documents_the_keep_worktree_hatch(self) -> None:
        block = self._skill_finish_block()
        assert "--keep-worktree" in block
        assert "IMPLEMENT_KEEP_WORKTREE" in block
        assert "last" in block.lower()

    def test_reference_documents_the_owner_table_and_predicate(self) -> None:
        section = self._reference_section()
        for token in (
            "driven-session",
            "keep-worktree-env",
            "keep-worktree-flag",
            "_should_defer_worktree_removal",
            "`--keep-worktree`",
            "`IMPLEMENT_KEEP_WORKTREE=1`",
            "`implement.py drive`",
            "abort",
        ):
            assert token in section, f"missing from the ownership section: {token}"

    def test_reference_documents_the_report_contract(self) -> None:
        section = self._reference_section()
        assert '"deferred": true' in section
        assert "worktree_removed" in section
        assert "retained_worktrees" in section
        assert "worktree_sweep" in section
        assert "git worktree remove" in section
        assert "git worktree prune" in section

    def test_isolation_doc_documents_the_ownership_rule(self) -> None:
        section = " ".join(self._isolation_section().split())
        assert "IMPLEMENT_DRIVE_ACTIVE" in section
        assert "--keep-worktree" in section
        assert "IMPLEMENT_KEEP_WORKTREE" in section
        assert "last" in section.lower()
        assert "Interruption safety" in section
        assert "implement-skill-reference.md" in section

    def test_no_instruction_tells_a_session_to_remove_its_live_cwd(self) -> None:
        """The competing instruction must not return unqualified.

        A bare `git worktree remove` step, with no ownership qualification,
        is exactly the drift that produced the incident: driving or
        opted-in sessions would try to delete the tree they are standing in.
        """
        block = self._skill_finish_block()
        assert "ownership" in block.lower()
        assert "defer" in block.lower()
