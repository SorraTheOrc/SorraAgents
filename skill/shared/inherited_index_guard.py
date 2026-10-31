"""Per-test guard: fail the test that writes an inherited ``GIT_INDEX_FILE``.

``implement.py finish`` stages the *would-be commit* in a temporary index and
exports ``GIT_INDEX_FILE`` for the suite it runs (SA-0MVQHVGFR006W8LG). That
variable deliberately survives the repository-override scrub
(SA-0MVQHV9Y3001RNNW), so it is inherited by **every** ``git`` subprocess a
test spawns. A fixture helper that runs raw ``git init``/``add``/``commit`` in
its own throwaway repository therefore stages into the *gate's* index instead
of the fixture's own: the gate's post-suite integrity check then reports
``gate evidence invalid`` and refuses a verdict it can no longer attribute to
the would-be-commit tree — with no indication of *which* test did it
(SA-0MVWQ4FZ4003S0GE).

This module is the missing attribution layer. :class:`IndexWatch` captures the
inherited index (path, staged tree, content digest) when the session starts and
:meth:`IndexWatch.poll` — called from an autouse fixture in the repo-root
``conftest.py`` after every test — reports the violation **naming that test**,
before the gate fails anonymously at the end of the suite.

Design constraints (mirroring ``live_repo_guard.py``, which the same conftest
loads by file location):

- **stdlib-only, loaded by file location.** The shared git helpers are loaded
  on demand from the sibling ``git_sandbox.py`` (exactly as
  ``live_repo_guard`` does) so this module never mutates ``sys.path``;
- **bound to the inherited index, not the current environment.** The watched
  path is recorded once at session start. A test that deliberately exports its
  own ``GIT_INDEX_FILE`` (``skill/implement/tests/test_finish_gate_*.py``) is
  exercising that contract on purpose and must never trip the guard;
- **no false positives.** A movement of the file's stat signature is only a
  *trigger*; the verdict compares the staged tree (falling back to a content
  digest), so an index refresh that leaves the staged tree intact (git
  rewriting stat data) is not a violation;
- **no cascading failures.** After reporting, the watch re-baselines on the
  current state so only the first offending test is named — the culprit, not
  every later test in the session;
- **a no-op without an inherited index.** A plain ``pytest`` run (no gate) has
  no watched index and the guard is inert.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The variable the finish gate exports and hermetic fixtures must not inherit.
INDEX_ENV_VAR = "GIT_INDEX_FILE"

#: Actionable remediation appended to every violation report.
REMEDIATION = (
    "Fixture git commands must be hermetic: run them through "
    "shared.git_sandbox.run_git(...) (or pass "
    "shared.git_sandbox.fixture_git_env() as the subprocess ``env``) so an "
    "inherited GIT_INDEX_FILE is dropped. A command that intentionally "
    "targets a specific index must pass that path explicitly."
)

#: Headline of a violation, stable so tests and log greps can pin it.
VIOLATION_HEADLINE = "inherited GIT_INDEX_FILE was modified by a test"

_GIT_SANDBOX_FILENAME = "git_sandbox.py"


def _load_git_sandbox() -> Any:
    """Load the sibling ``git_sandbox`` module by file location."""
    path = Path(__file__).resolve().parent / _GIT_SANDBOX_FILENAME
    spec = importlib.util.spec_from_file_location(
        "_inherited_index_git_sandbox", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load git sandbox helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_env(index_path: Path) -> dict[str, str]:
    """Environment for the guard's own ``git`` calls.

    Repository overrides are scrubbed through the shared choke point so the
    guard measures the repository it names, and the watched index is pinned
    explicitly rather than trusting the (possibly monkeypatched) ambient value.
    """
    env = _load_git_sandbox().scrub_repository_overrides(os.environ)
    env[INDEX_ENV_VAR] = str(index_path)
    return env


def _stat_signature(index_path: str | os.PathLike[str]) -> tuple[int, int, int] | None:
    """A cheap change trigger for the index file (``None`` when absent)."""
    try:
        stat = os.stat(index_path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _content_digest(index_path: str | os.PathLike[str]) -> str | None:
    """sha256 of the index file's bytes (``None`` when unreadable)."""
    try:
        return hashlib.sha256(Path(index_path).read_bytes()).hexdigest()
    except OSError:
        return None


def _staged_tree(repo: str | os.PathLike[str], index_path: str | os.PathLike[str]) -> str | None:
    """``git write-tree`` of *index_path* inside *repo* (``None`` if it fails).

    The tree reflects the *entries* staged in the index and ignores the stat
    cache git writes on an index refresh, so an index rewrite that stages the
    same entries is not reported. It fails (``None``) when a fixture has
    staged blobs from another repository — the poisoning case — which is why
    :func:`IndexWatch.poll` falls back to the content digest.
    """
    try:
        proc = subprocess.run(
            ["git", "write-tree"],
            cwd=str(repo),
            env=_git_env(Path(index_path)),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


@dataclass(frozen=True)
class IndexSnapshot:
    """One observation of the watched index."""

    signature: tuple[int, int, int] | None
    digest: str | None
    tree: str | None


@dataclass
class IndexWatch:
    """Watches an inherited ``GIT_INDEX_FILE`` for writes during a session.

    Use :meth:`capture` once at session start and :meth:`poll` after each test.
    """

    repo: Path
    index_path: Path
    snapshot: IndexSnapshot = field(default_factory=lambda: IndexSnapshot(None, None, None))

    @classmethod
    def capture(
        cls,
        repo: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
    ) -> IndexWatch | None:
        """Arm a watch on the inherited index, or ``None`` when there is none.

        Args:
            repo: The checkout the inherited index belongs to (the gate's
                worktree). Staged-tree comparisons run there.
            env: Environment to read ``GIT_INDEX_FILE`` from (defaults to
                ``os.environ``). Only the *inherited* value is watched; a test
                that sets its own index later is out of scope by design.
        """
        source = os.environ if env is None else env
        index_file = source.get(INDEX_ENV_VAR)
        if not index_file:
            return None
        index_path = Path(index_file).expanduser()
        if not index_path.is_file():
            return None
        resolved_repo = Path(repo).resolve()
        return cls(
            repo=resolved_repo,
            index_path=index_path,
            snapshot=IndexSnapshot(
                signature=_stat_signature(index_path),
                digest=_content_digest(index_path),
                tree=_staged_tree(resolved_repo, index_path),
            ),
        )

    def poll(self) -> str | None:
        """Return a violation report when the index changed, else ``None``.

        On a violation the watch **re-baselines** on the current state, so the
        next poll reports ``None``: only the test that wrote the index is
        named, not every later test in the session.
        """
        signature = _stat_signature(self.index_path)
        if signature == self.snapshot.signature:
            return None

        digest = _content_digest(self.index_path)
        if digest == self.snapshot.digest:
            # Stat churn only (e.g. a re-stat of an identical index).
            self.snapshot = IndexSnapshot(signature, digest, self.snapshot.tree)
            return None

        tree = _staged_tree(self.repo, self.index_path)
        if tree is not None and tree == self.snapshot.tree:
            # The file was rewritten but stages the same entries: an index
            # refresh, not a foreign staging operation.
            self.snapshot = IndexSnapshot(signature, digest, tree)
            return None

        report = self._report(tree)
        self.snapshot = IndexSnapshot(signature, digest, tree)
        return report

    def _report(self, tree: str | None) -> str:
        """Render the actionable violation report."""
        before = self.snapshot.tree or "unreadable"
        after = tree or "unreadable"
        return (
            f"{VIOLATION_HEADLINE}:\n"
            f"  index:       {self.index_path}\n"
            f"  repo:        {self.repo}\n"
            f"  tested tree: {before}\n"
            f"  index tree:  {after}\n"
            "  A fixture git command staged into the index it inherited from "
            "the finish gate instead of using its own repository's index, so "
            "the gate's verdict can no longer be attributed to the "
            "would-be-commit tree.\n"
            f"  {REMEDIATION}"
        )
