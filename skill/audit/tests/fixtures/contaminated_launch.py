"""Reusable offline fixture: a contaminated launch checkout.

Reproduces the 2026-11-06 RISE-0MW19AROF0035FRH incident deterministically
(SA-0MW4ZRLSI005PHL6 AC4): a local ``dev`` checkout carrying an un-pushed
in-flight commit that touched a harness file **and** sitting several commits
behind ``origin/dev``. An audit launched from that checkout must refuse or
isolate rather than run the F3 full suite against the contaminated tree and
emit a misleading ``partial``/``No`` verdict.

The fixture builds everything under a caller-supplied ``tmp_path`` — no
network access, no reads from the host repository. It reuses the shared
:mod:`shared.git_sandbox` helpers so every real-``git`` call is hermetic
(repository-overriding env neutralised, fixture-local identity pinned;
``skill/shared/test-writing-guidelines.md`` anti-pattern 7).

Layout
------
``<root>/``
    ``submodule_src/``   — the tracked submodule's source repository.
    ``origin.git/``      — bare remote whose ``dev`` holds the delivered tip.
    ``clean/``           — a clean clone tracking ``origin/dev``
                           (``HEAD == origin/dev``).
    ``dirty/``           — a clone carrying **1 un-pushed commit** touching
                           ``src/harness.py`` **and** ``6`` commits behind
                           ``origin/dev`` (the incident shape).

Neither clone has a ``node_modules`` directory (it is git-ignored), and
neither has its submodule initialised — a fresh ``git worktree add`` therefore
starts with no toolchain, exercising the AC1 initialisation step.

Delivered commits
-----------------
``origin/dev`` holds ``delivered_commits`` — the item work already merged. A
caller passes those shas as the "delivered-commit candidates" to
``_audit_base_freshness`` and asserts:

* ``clean/`` (HEAD == origin/dev) is **fresh**;
* ``dirty/`` (HEAD == base + un-pushed commit, behind origin/dev) is **stale**
  — both because the delivered commits are absent from HEAD and because the
  un-pushed commit is ahead of ``origin/dev``.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ``skill`` root on sys.path for ``shared.git_sandbox`` imports. The audit
# test conftest bootstraps this too, but the fixture module is importable on
# its own so keep it self-contained.
_SKILL_ROOT = Path(__file__).resolve().parents[3]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from shared.git_sandbox import (
    commit_all,
    fixture_git_env,
    init_bare_remote,
    init_repo,
)

#: Number of commits the dirty clone is behind origin/dev (the incident was 6).
BEHIND_COMMITS = 6


@dataclass
class ContaminatedLaunchScenario:
    """Paths and shas describing the contaminated-launch scenario."""

    root: Path
    submodule_src: Path
    origin: Path
    clean: Path
    dirty: Path
    delivered_commits: list[str] = field(default_factory=list)
    """Shas present on ``origin/dev`` but absent from the dirty clone's HEAD."""
    dirty_head: str = ""
    """The dirty clone's un-pushed in-flight commit (touches the harness)."""
    dirty_base: str = ""
    """The dirty clone's base — ``BEHIND_COMMITS`` behind ``origin/dev``."""
    origin_dev: str = ""
    """The delivered tip on ``origin/dev``."""

    def clean_clone(self) -> Path:
        """The clean clone (``HEAD == origin/dev``)."""
        return self.clean

    def dirty_clone(self) -> Path:
        """The dirty clone (ahead by one, behind by ``BEHIND_COMMITS``)."""
        return self.dirty


def _git(cwd: Path, *args: str) -> str:
    """Run one hermetic git command in *cwd* and return stripped stdout."""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=False, env=fixture_git_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd} (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def build_contaminated_launch_scenario(
    root: Path,
    *,
    behind_commits: int = BEHIND_COMMITS,
    harness_path: str = "src/harness.py",
) -> ContaminatedLaunchScenario:
    """Build the contaminated-launch fixture under *root*.

    Deterministic and offline. See the module docstring for the layout and
    the assertion contract.

    Args:
        root: A ``tmp_path``-rooted directory the fixture owns entirely.
        behind_commits: How many delivered commits the dirty clone is behind
            ``origin/dev`` (default 6 — the incident count).
        harness_path: The repo-relative path the dirty clone's un-pushed
            commit touches (default ``src/harness.py``).

    Returns:
        A :class:`ContaminatedLaunchScenario` describing every path and sha.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    submodule_src = _build_submodule_src(root)
    origin = _build_origin(root, submodule_src, behind_commits=behind_commits)
    origin_dev = _git(origin, "rev-parse", "dev")
    dirty_base = _git(origin, "rev-parse", f"dev~{behind_commits}")
    delivered_commits = [
        _git(origin, "rev-parse", f"dev~{i}") for i in range(behind_commits)
    ][::-1]  # oldest first

    clean = root / "clean"
    _clone(origin, clean)
    _git(clean, "checkout", "-q", "dev")
    assert _git(clean, "rev-parse", "HEAD") == origin_dev

    dirty = root / "dirty"
    _clone(origin, dirty)
    _git(dirty, "checkout", "-q", "dev")
    # Rewind to the base (BEHIND origin/dev) then park one un-pushed commit
    # touching the harness file — the incident's d538e51 shape.
    _git(dirty, "reset", "-q", "--hard", dirty_base)
    harness = dirty / harness_path
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text(
        "# un-pushed in-flight harness change (contaminates the main checkout)\n"
        "def run_harness():\n    return 'dirty'\n",
        encoding="utf-8",
    )
    commit_all(dirty, "un-pushed in-flight harness change")
    dirty_head = _git(dirty, "rev-parse", "HEAD")
    assert dirty_head != dirty_base

    return ContaminatedLaunchScenario(
        root=root,
        submodule_src=submodule_src,
        origin=origin,
        clean=clean,
        dirty=dirty,
        delivered_commits=delivered_commits,
        dirty_head=dirty_head,
        dirty_base=dirty_base,
        origin_dev=origin_dev,
    )


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------


def _build_submodule_src(root: Path) -> Path:
    """A tiny source repository for the tracked git submodule."""
    src = init_repo(root / "submodule_src", sandbox_root=root)
    (src / "core.py").write_text("# submodule core\nVALUE = 1\n", encoding="utf-8")
    commit_all(src, "submodule: initial core")
    return src


def _build_origin(root: Path, submodule_src: Path, *,
                  behind_commits: int) -> Path:
    """Bare origin whose ``dev`` holds the delivered tip.

    The delivered history is ``1 base commit + ``behind_commits`` further
    commits``. The dirty clone rewinds to the base, so it is exactly
    ``behind_commits`` behind ``origin/dev``.
    """
    work = init_repo(root / "delivered", sandbox_root=root)
    (work / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (work / "src").mkdir()
    (work / "src" / "main.py").write_text("print('base')\n", encoding="utf-8")
    # Track the submodule as a gitlink + .gitmodules entry.
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add",
         str(submodule_src), "core"],
        cwd=str(work), capture_output=True, text=True, check=True,
        env=fixture_git_env(),
    )
    commit_all(work, "base: project + tracked submodule")
    for i in range(behind_commits):
        (work / "src" / "main.py").write_text(
            f"print('delivered {i + 1}')\n", encoding="utf-8"
        )
        commit_all(work, f"delivered: change {i + 1}")

    origin = init_bare_remote(root / "origin.git", sandbox_root=root)
    # Push the delivered history as the remote's dev branch.
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/dev")
    # A local ``dev`` in the bare remote, so clones track it.
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/dev")
    return origin


def _clone(origin: Path, target: Path) -> None:
    """Clone *origin* into *target* using the hermetic environment."""
    proc = subprocess.run(
        ["git", "clone", "-q", str(origin), str(target)],
        capture_output=True, text=True, check=False, env=fixture_git_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git clone {origin} -> {target} failed "
            f"(rc={proc.returncode}): {proc.stderr.strip()}"
        )
    # Pin the fixture identity in the clone so later commits never rely on
    # ambient config.
    _git(target, "config", "user.email", "fixture@example.invalid")
    _git(target, "config", "user.name", "Fixture")
