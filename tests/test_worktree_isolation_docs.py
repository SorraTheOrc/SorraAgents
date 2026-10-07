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
