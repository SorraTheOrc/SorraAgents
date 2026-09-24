#!/usr/bin/env python3
"""Regression tests for ``_resolve_owning_checkout_root`` / ``_resolve_repo_root``.

Covers SA-0MT6D0CFG004EA55: the tracked nested ``skill/skill/shared/`` copy
(commit aea4c741) made ``_resolve_repo_root()`` resolve one level too deep
when launched from the framework's main checkout (``<repo>/skill`` instead of
``<repo>``). The audit runner's prefix-to-sibling scan
(``SIBLING_SCAN_ROOT = REPO_ROOT.parent``) then could not find the owning
project's ``.worklog`` without an explicit ``--worklog-dir``.
"""  # noqa: EXE001

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from shared import status_lifecycle

RESOLVE = status_lifecycle._resolve_owning_checkout_root


def _write_module_copies(checkout: Path, with_nested: bool = True) -> Path:
    """Create ``skill/shared/status_lifecycle.py`` (and the tracked nested
    ``skill/skill/shared/`` copy) under *checkout*. Returns the module file.
    """
    real = checkout / "skill" / "shared" / "status_lifecycle.py"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("# module\n", encoding="utf-8")
    if with_nested:
        nested = checkout / "skill" / "skill" / "shared" / "status_lifecycle.py"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("# nested copy\n", encoding="utf-8")
    return real


class TestResolveOwningCheckoutRoot:
    def test_main_checkout_with_nested_copy_resolves_to_repo_root(self, tmp_path):
        """AC1: a nested skill/skill copy must not shift the resolved root."""
        main = tmp_path / "SorraAgents"
        (main / ".git").mkdir(parents=True)
        module = _write_module_copies(main)
        assert RESOLVE(module) == main

    def test_main_checkout_without_nested_copy_resolves(self, tmp_path):
        """The conventional layout (no nested copy) still resolves."""
        main = tmp_path / "SorraAgents"
        (main / ".git").mkdir(parents=True)
        module = _write_module_copies(main, with_nested=False)
        assert RESOLVE(module) == main

    def test_worktree_launch_resolves_to_main_checkout(self, tmp_path):
        """AC2: launches from a worktree keep resolving to the main checkout.

        The worktree sits under ``<main>/.worklog/worktrees/<name>/`` with a
        ``.git`` FILE (gitdir pointer) at its root. Before the nested copy
        was introduced this resolved to the main checkout; the fix must
        restore exactly that.
        """
        main = tmp_path / "SorraAgents"
        (main / ".git").mkdir(parents=True)
        _write_module_copies(main)
        wt = main / ".worklog" / "worktrees" / "wt-foo"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /some/place\n", encoding="utf-8")
        module = _write_module_copies(wt)
        assert RESOLVE(module) == main

    def test_worktree_launch_without_nested_copy_resolves_to_main_checkout(
        self, tmp_path
    ):
        """The pre-aea4c741 worktree behavior (no nested copies at all)."""
        main = tmp_path / "SorraAgents"
        (main / ".git").mkdir(parents=True)
        _write_module_copies(main, with_nested=False)
        wt = main / ".worklog" / "worktrees" / "wt-foo"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /some/place\n", encoding="utf-8")
        module = _write_module_copies(wt, with_nested=False)
        assert RESOLVE(module) == main

    def test_plain_folder_without_git_falls_back(self, tmp_path):
        """A module copy outside any git checkout falls back to parents[2]."""
        module_file = tmp_path / "a" / "b" / "c" / "skill" / "shared" / "status_lifecycle.py"
        module_file.parent.mkdir(parents=True)
        module_file.write_text("# module\n", encoding="utf-8")
        assert RESOLVE(module_file) == module_file.parents[2]


class TestResolveRepoRootRealRepo:
    def test_framework_root_resolves_to_main_checkout_here(self):
        """Integration: in this repo the nested copy is present, so a correct
        resolution equals the framework MAIN checkout (not the nested-copy
        parent and not the worktree root when tests run inside a worktree).
        """
        import subprocess

        module = Path(status_lifecycle.__file__).resolve()
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(module.parent),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (module.parent / common_path).resolve()
        expected_main = common_path.parent
        root = status_lifecycle._resolve_repo_root()
        assert root == expected_main
        assert (root / ".git").is_dir()
        assert (root / "skill" / "shared" / "status_lifecycle.py").is_file()
        # SIBLING_SCAN_ROOT points at the sibling-projects directory
        # (parent of the framework repo), not inside it.
        assert status_lifecycle.SIBLING_SCAN_ROOT == root.parent