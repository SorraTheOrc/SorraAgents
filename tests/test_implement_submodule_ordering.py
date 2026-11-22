"""Submodule init must precede the ``node_modules`` symlink passes.

Work item **SA-0MVTOR53E003IIEQ** — *implement.py start: nested node_modules
symlink blocks submodule init (bootstrap ordering)*.

The defect
----------
``implement.py start`` created the ``node_modules`` symlinks **before** running
``git submodule update --init``. When a submodule's own root holds a
``node_modules`` in the main checkout (tce-the-rising's ``core`` submodule
does), the nested-symlink pass created ``<worktree>/core/node_modules`` inside
the still-**empty** submodule directory, so ``git submodule update --init``
could not clone into ``core/``::

    fatal: destination path '<worktree>/core' already exists and is not an empty directory.

Submodule init failures are deliberately fail-open (SA-0MSN52GGN002B0AZ AC2),
so ``start`` reported success while the worktree held an **uninitialised**
submodule; every build then died on the submodule's own imports and each child
session had to hand-repair the checkout. The defect recurred four times
(RISE-0MVZBUE5R0026M95, RISE-0MVTOVFW2009MOLJ, RISE-0MVZDTLEH002KS29,
RISE-0MWRUYNTF0028CS2).

Secondary defect: even after a successful init the nested symlink was not
ignored by the submodule's ``.gitignore`` (``node_modules/`` with a trailing
slash does not match a *symlink*), so the superproject reported ``M core`` and
the worktree looked dirty.

The contract pinned here
------------------------
1. ``_ensure_submodules`` runs **before** the ``node_modules`` passes, so a
   fresh worktree's submodule directory is empty when git clones into it.
2. A nested-symlink candidate inside a **declared but unpopulated** submodule
   path is skipped (a symlink there would block the clone on a resume).
3. A nested symlink that *is* created inside a checked-out submodule is
   covered by the submodule's own work-tree-local exclude entry, so the
   worktree's ``git status --porcelain`` stays clean.
4. A resumed worktree left broken by the old ordering (a placeholder directory
   holding only the ``node_modules`` symlink) is repaired before init.
5. The fail-open contract is unchanged: a genuinely failing
   ``git submodule update`` only logs a warning and never aborts ``start``.

Fixtures are hermetic real-git repositories from
:mod:`skill.shared.git_sandbox`; nothing installs packages and no test touches
the live checkout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from skill.shared import git_sandbox

_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
_SKILL_MD = _REPO_ROOT / "skill" / "implement" / "SKILL.md"
_REFERENCE_MD = _REPO_ROOT / "docs" / "dev" / "implement-skill-reference.md"

_FAKE_WORK_ITEM_ID = "SA-TESTORD"


# ---------------------------------------------------------------------------
# Module/fixture helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def implement_mod():
    """Load ``implement.py`` once for the helper-level (unit) tests."""
    name = "implement_under_test_submodule_ordering"
    spec = importlib.util.spec_from_file_location(name, _IMPLEMENT_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_engine(tmp_path: Path) -> tuple[Path, str]:
    """Create the submodule's own repository (the engine) and its sha.

    The engine's ``.gitignore`` carries the reference ``node_modules/`` pattern
    with a **trailing slash** — the pattern that does not match a symlink and
    therefore reproduces the secondary dirty-submodule defect.
    """
    engine = git_sandbox.init_repo(
        tmp_path / "engine", default_branch="main", sandbox_root=tmp_path
    )
    (engine / "engine.js").write_text("// engine v1\n", encoding="utf-8")
    (engine / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    sha = git_sandbox.commit_all(engine, "engine v1")
    return engine, sha


def _make_superproject(
    tmp_path: Path,
    *,
    with_submodule: bool = True,
    submodule_url: str | None = None,
) -> tuple[Path, str | None, Path]:
    """Create the main checkout, optionally with a local-path submodule.

    Args:
        tmp_path: Per-test sandbox root.
        with_submodule: Track a real submodule at ``core`` when ``True``;
            otherwise just declare one in ``.gitmodules`` (unreachable URL).
        submodule_url: Override the recorded submodule URL.

    Returns:
        ``(repo_root, recorded_submodule_sha_or_None, worktree_dir)``.
    """
    engine, engine_sha = _make_engine(tmp_path)
    repo = git_sandbox.init_repo(
        tmp_path / "repo", default_branch="master", sandbox_root=tmp_path
    )
    (repo / "README.md").write_text("# superproject\n", encoding="utf-8")
    # Mirror the real checkout: the implement state file never dirties a tree.
    (repo / ".gitignore").write_text(".implement_state.json\n", encoding="utf-8")
    git_sandbox.commit_all(repo, "initial commit")

    recorded_sha: str | None = None
    if with_submodule:
        # Local-path submodules need the file transport allowed; a worktree
        # shares this config, so `start`'s `git submodule update` resolves it.
        git_sandbox.run_git(repo, ["config", "protocol.file.allow", "always"], check=True)
        url = submodule_url or str(engine)
        git_sandbox.run_git(
            repo,
            ["submodule", "add", "-q", url, "core"],
            check=True,
        )
        recorded_sha = engine_sha
        git_sandbox.commit_all(repo, "add core submodule")
        # The reference shape: the main checkout's submodule root holds its own
        # `node_modules` (that directory is what the nested pass tried to link).
        nested = repo / "core" / "node_modules"
        nested.mkdir(parents=True)
        (nested / "left-pad.js").write_text("module.exports = 1;\n", encoding="utf-8")
    else:
        (repo / ".gitmodules").write_text(
            textwrap.dedent(
                """\
                [submodule "core"]
                    path = core
                    url = file:///nonexistent/engine.git
                """
            ),
            encoding="utf-8",
        )
        git_sandbox.commit_all(repo, "declare unreachable submodule")

    # The integration branch is cut from the *committed* shape (gitlink and
    # `.gitmodules` included) so the worktree inherits it.
    git_sandbox.run_git(repo, ["branch", "dev"], check=True)

    return repo, recorded_sha, tmp_path / "wt"


def _add_worktree(repo: Path, worktree_dir: Path) -> Path:
    """Create a real ``wl-*`` worktree of *repo* at *worktree_dir*."""
    return git_sandbox.add_worktree(repo, worktree_dir, "wl-test", base="dev")


def _status(worktree: Path) -> str:
    return git_sandbox.run_git(worktree, ["status", "--porcelain=v1"], check=True).stdout


def _module_loader(repo_root: Path) -> str:
    """Python preamble that loads ``implement.py`` as ``mod`` with cwd=repo_root."""
    return textwrap.dedent(f"""\
        import sys, os
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        sys.path.insert(0, {str(_REPO_ROOT / "skill")!r})
        os.chdir({str(repo_root)!r})
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "implement_under_test", {str(_IMPLEMENT_PY)!r},
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "implement_scripts"
        sys.modules["implement_under_test"] = mod
        spec.loader.exec_module(mod)
    """)


def _run_in_subprocess(tmp_path: Path, runner_source: str) -> subprocess.CompletedProcess:
    """Run a snippet against the implement module in an isolated subprocess."""
    runner_path = tmp_path / "_test_runner.py"
    runner_path.write_text(runner_source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(runner_path)],
        cwd=str(tmp_path),
        env=git_sandbox.fixture_git_env(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _phase_start_runner(repo_root: Path, worktree_path: Path) -> str:
    """Runner that calls ``phase_start`` with external integrations stubbed."""
    return _module_loader(repo_root) + textwrap.dedent(f"""\
        import json as _json
        mod.is_code_freeze_active = lambda: False

        class _FakeLifecycle:
            @staticmethod
            def update_status(*a, **k):
                return None

        mod.StatusLifecycle = _FakeLifecycle
        mod.git_status = lambda cwd=None: ""
        mod.git_has_dirty_files = lambda status_output=None, expected_dirty=(): False
        mod.wl_show = lambda i: {{"id": i, "title": "Test item"}}
        mod.wl_add_comment = lambda *a, **k: True

        result = mod.phase_start(
            {_FAKE_WORK_ITEM_ID!r},
            json_output=True,
            worktree_path_override={str(worktree_path)!r},
        )
        print("RESULT_JSON:" + _json.dumps(result))
    """)


def _phase_start(repo: Path, worktree_path: Path, tmp_path: Path) -> dict:
    """Run ``phase_start`` in a subprocess and return its JSON report."""
    proc = _run_in_subprocess(tmp_path, _phase_start_runner(repo, worktree_path))
    assert proc.returncode == 0, proc.stderr
    line = next(
        (l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON:")), None
    )
    assert line is not None, f"no RESULT_JSON line\n{proc.stdout}\n{proc.stderr}"
    return json.loads(line[len("RESULT_JSON:"):])


# ---------------------------------------------------------------------------
# AC: declared-submodule discovery
# ---------------------------------------------------------------------------


class TestDeclaredSubmodulePaths:
    def test_lists_declared_paths(self, tmp_path, implement_mod):
        repo, _sha, _wt = _make_superproject(tmp_path)
        assert implement_mod._iter_declared_submodule_paths(str(repo)) == ("core",)

    def test_repo_without_gitmodules_has_no_paths(self, tmp_path, implement_mod):
        repo = git_sandbox.init_repo(tmp_path / "plain", sandbox_root=tmp_path)
        (repo / "README.md").write_text("# plain\n", encoding="utf-8")
        git_sandbox.commit_all(repo, "initial")
        assert implement_mod._iter_declared_submodule_paths(str(repo)) == ()

    def test_unknown_repo_root_is_empty(self, implement_mod):
        assert implement_mod._iter_declared_submodule_paths(None) == ()


# ---------------------------------------------------------------------------
# AC: the nested-symlink pass must not create a directory inside a gitlink
# ---------------------------------------------------------------------------


class TestUnpopulatedSubmoduleDetection:
    def test_nested_declared_path_is_reported(self, tmp_path, implement_mod):
        """A declared sub-submodule on the path is a clone target too."""
        wt = tmp_path / "wt"
        (wt / "vendor").mkdir(parents=True)
        (wt / "core").mkdir()
        (wt / "core" / ".git").write_text("gitdir: x\n", encoding="utf-8")
        paths = ("core", "vendor/lib")

        assert (
            implement_mod._first_unpopulated_submodule("vendor/lib/x", paths, wt)
            == "vendor/lib"
        )
        assert (
            implement_mod._first_unpopulated_submodule("core/node_modules", paths, wt)
            is None
        )
        assert implement_mod._first_unpopulated_submodule("other/pkg", paths, wt) is None

class TestNestedSymlinkPassRespectsSubmodules:
    def test_unpopulated_submodule_path_is_skipped(self, tmp_path, implement_mod):
        """An uninitialised gitlink holds no symlink to block the clone."""
        repo, _sha, wt_dir = _make_superproject(tmp_path)
        wt = _add_worktree(repo, wt_dir)
        # `git worktree add` materialises the gitlink as an EMPTY directory —
        # exactly the shape the old ordering filled with a symlink. Simulate an
        # uninitialised submodule: the directory exists, `.git` does not.
        assert (wt / "core").is_dir()
        assert not (wt / "core" / ".git").exists()

        created = implement_mod._ensure_nested_node_modules_symlinks(str(wt), str(repo))

        assert created == 0
        assert not os.path.lexists(wt / "core" / "node_modules"), (
            "a symlink inside an unpopulated submodule path would block "
            "`git submodule update --init`"
        )

    def test_checked_out_submodule_gets_the_link_and_stays_clean(
        self, tmp_path, implement_mod
    ):
        """A populated submodule keeps its nested link, without dirtying the tree."""
        repo, _sha, wt_dir = _make_superproject(tmp_path)
        wt = _add_worktree(repo, wt_dir)
        git_sandbox.run_git(
            wt, ["submodule", "update", "--init", "--recursive"], check=True
        )
        assert (wt / "core" / ".git").exists()

        created = implement_mod._ensure_nested_node_modules_symlinks(str(wt), str(repo))

        assert created == 1
        assert (wt / "core" / "node_modules").is_symlink()
        assert (wt / "core" / "node_modules" / "left-pad.js").is_file()
        assert _status(wt) == "", (
            "the nested symlink must not surface as `M core` — cover it with the "
            "submodule's own exclude entry"
        )

    def test_exclude_entry_is_idempotent(self, tmp_path, implement_mod):
        repo, _sha, wt_dir = _make_superproject(tmp_path)
        wt = _add_worktree(repo, wt_dir)
        git_sandbox.run_git(
            wt, ["submodule", "update", "--init", "--recursive"], check=True
        )

        implement_mod._ensure_nested_node_modules_symlinks(str(wt), str(repo))
        # A second run must not duplicate the exclude entry.
        os.unlink(wt / "core" / "node_modules")
        implement_mod._ensure_nested_node_modules_symlinks(str(wt), str(repo))

        exclude = git_sandbox.run_git(
            wt / "core", ["rev-parse", "--git-path", "info/exclude"], check=True
        ).stdout.strip()
        entries = [
            line.strip()
            for line in Path(exclude).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert entries.count("node_modules") == 1, entries

    def test_existing_link_on_resume_gets_the_exclude_entry(
        self, tmp_path, implement_mod
    ):
        """A link left by an older run is covered on the next `start`."""
        repo, _sha, wt_dir = _make_superproject(tmp_path)
        wt = _add_worktree(repo, wt_dir)
        git_sandbox.run_git(
            wt, ["submodule", "update", "--init", "--recursive"], check=True
        )
        os.symlink(str(repo / "core" / "node_modules"), str(wt / "core" / "node_modules"))
        assert _status(wt) != "", "fixture must start dirty, else the test proves nothing"

        created = implement_mod._ensure_nested_node_modules_symlinks(str(wt), str(repo))

        assert created == 0, "the existing link is never replaced"
        assert _status(wt) == "", "the resume path must still cover the link"

    def test_repo_without_submodules_is_unchanged(self, tmp_path, implement_mod):
        """AC5: a plain nested package keeps its symlink, exactly as before."""
        repo = git_sandbox.init_repo(
            tmp_path / "plain", default_branch="master", sandbox_root=tmp_path
        )
        pkg = repo / "packages" / "web"
        (pkg / "node_modules" / "dep").mkdir(parents=True)
        (pkg / "node_modules" / "dep" / "index.js").write_text("module.exports=1\n")
        (pkg / "package.json").write_text("{}\n", encoding="utf-8")
        # Mirror a real checkout: dependencies are gitignored, never tracked.
        (repo / ".gitignore").write_text(
            "node_modules\n.implement_state.json\n", encoding="utf-8"
        )
        git_sandbox.commit_all(repo, "initial")
        git_sandbox.run_git(repo, ["branch", "dev"], check=True)
        wt = _add_worktree(repo, tmp_path / "wt")

        created = implement_mod._ensure_nested_node_modules_symlinks(str(wt), str(repo))

        assert created == 1
        assert (wt / "packages" / "web" / "node_modules").is_symlink()


# ---------------------------------------------------------------------------
# AC: a resumed worktree broken by the old ordering is repaired
# ---------------------------------------------------------------------------


class TestPlaceholderRepair:
    def test_placeholder_dir_is_cleared_before_init(self, tmp_path, implement_mod):
        """A dir holding only our symlink is removed so git can clone into it."""
        repo, sha, wt_dir = _make_superproject(tmp_path)
        wt = _add_worktree(repo, wt_dir)
        # The exact state the old ordering left behind on a resumed worktree:
        # the gitlink directory holds only our `node_modules` symlink.
        assert (wt / "core").is_dir()
        os.symlink(str(repo / "core" / "node_modules"), str(wt / "core" / "node_modules"))

        assert implement_mod._ensure_submodules(str(wt), str(repo)) is True

        status = git_sandbox.run_git(wt, ["submodule", "status"], check=True).stdout
        assert status[:1] != "-", f"submodule still uninitialised: {status!r}"
        assert sha in status
        assert (wt / "core" / "engine.js").is_file()

    def test_real_submodule_content_is_never_deleted(self, tmp_path, implement_mod):
        """Only framework placeholders are cleared — never a real checkout."""
        repo, _sha, wt_dir = _make_superproject(tmp_path)
        wt = _add_worktree(repo, wt_dir)
        (wt / "core" / "local-work.txt").write_text("keep me\n", encoding="utf-8")

        implement_mod._ensure_submodules(str(wt), str(repo))

        assert (wt / "core" / "local-work.txt").is_file()


# ---------------------------------------------------------------------------
# AC1/AC2: end-to-end `start` bootstrap ordering
# ---------------------------------------------------------------------------


class TestPhaseStartBootstrap:
    def test_start_initialises_submodule_with_nested_node_modules(self, tmp_path):
        """AC2: checked-out submodule at the recorded commit; clean status."""
        repo, sha, wt = _make_superproject(tmp_path)

        report = _phase_start(repo, wt, tmp_path)

        assert report["success"] is True, report
        status = git_sandbox.run_git(wt, ["submodule", "status"], check=True).stdout
        assert status[:1] != "-", f"submodule not checked out: {status!r}"
        assert sha in status, f"submodule not at the recorded commit: {status!r}"
        assert _status(wt) == "", "worktree must be clean after start"

    def test_start_is_fail_open_when_submodule_update_genuinely_fails(self, tmp_path):
        """AC3: a real clone failure warns but does not abort `start`."""
        repo, _sha, wt = _make_superproject(
            tmp_path, with_submodule=False
        )

        report = _phase_start(repo, wt, tmp_path)

        assert report["success"] is True, report

    def test_start_without_submodules_is_unchanged(self, tmp_path):
        """AC5: repos without `.gitmodules` behave exactly as before."""
        repo = git_sandbox.init_repo(
            tmp_path / "plain", default_branch="master", sandbox_root=tmp_path
        )
        (repo / "README.md").write_text("# plain\n", encoding="utf-8")
        (repo / ".gitignore").write_text(".implement_state.json\n", encoding="utf-8")
        git_sandbox.commit_all(repo, "initial")
        git_sandbox.run_git(repo, ["branch", "dev"], check=True)

        report = _phase_start(repo, tmp_path / "wt", tmp_path)

        assert report["success"] is True, report
        assert _status(tmp_path / "wt") == ""


# ---------------------------------------------------------------------------
# Structural + documentation guards
# ---------------------------------------------------------------------------


class TestStructuralOrdering:
    SOURCE = _IMPLEMENT_PY.read_text(encoding="utf-8")

    def test_submodule_init_precedes_node_modules_preparation(self):
        sub_idx = self.SOURCE.index("_ensure_submodules(abs_wt_path")
        nm_idx = self.SOURCE.index("_prepare_node_modules(abs_wt_path")
        assert sub_idx < nm_idx, (
            "submodule init must run BEFORE the node_modules link farm: a nested "
            "symlink inside an empty gitlink blocks `git submodule update --init`"
        )

    def test_submodule_init_precedes_nested_symlinks(self):
        sub_idx = self.SOURCE.index("_ensure_submodules(abs_wt_path")
        nested_idx = self.SOURCE.index("_ensure_nested_node_modules_symlinks(abs_wt_path")
        assert sub_idx < nested_idx

    def test_nested_pass_checks_declared_submodules(self):
        func_start = self.SOURCE.index("def _ensure_nested_node_modules_symlinks(")
        func_body = self.SOURCE[func_start:func_start + 6000]
        assert "_iter_declared_submodule_paths(" in func_body, (
            "the nested-symlink pass must know which paths are submodules"
        )
        assert "_first_unpopulated_submodule(" in func_body, (
            "the pass must require a checked-out submodule before linking inside it"
        )
        guard_start = self.SOURCE.index("def _first_unpopulated_submodule(")
        guard_body = self.SOURCE[guard_start:guard_start + 2000]
        assert '".git"' in guard_body, (
            "a populated submodule is detected by its .git entry"
        )


class TestDocumentation:
    def test_skill_md_documents_the_ordering_constraint(self):
        content = _SKILL_MD.read_text(encoding="utf-8")
        lowered = content.lower()
        assert "submodule" in lowered
        assert "before" in lowered and "node_modules" in lowered, (
            "SKILL.md must state that submodule init runs before the "
            "node_modules symlink passes"
        )

    def test_reference_documents_the_ordering_constraint(self):
        content = _REFERENCE_MD.read_text(encoding="utf-8").lower()
        assert "submodule" in content and "before" in content
        assert "sa-0mbtor53e003iieq" in content, (
            "the deep reference must record the ordering constraint and its item"
        )
