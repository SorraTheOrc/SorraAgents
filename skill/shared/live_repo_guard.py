"""Pytest plugin: fail a session that mutates its own checkout (F5, SA-0MUG30JSE0069JEQ).

This is the inner net for **direct** ``pytest`` invocations (the ``run_tests.py``
guard in F4 is the outer net for the release path). It is deliberately a
*reusable plugin module* — not a conftest — so the repo-root ``conftest.py`` and
a test's ``tmp_path`` conftest can arm the **same hooks** without a
self-referential simulation.

Behaviour:

- ``pytest_configure`` → ``register(config, root=...)`` snapshots the checkout.
- ``pytest_runtest_teardown`` → compares a cheap **refs + local-config**
  fingerprint and, when it moves, fails the offending test immediately only
  if the authoritative full diff is non-empty, naming its node id.
- ``pytest_sessionfinish`` → compares the **full** surface (refs, config,
  tracked/untracked working tree) and exits non-zero, naming the last test if
  only the file surface changed.

Safety / no-false-positive rules (F5 AC2/AC5/AC6/AC7):

- loaded by **file location** so it never mutates ``sys.path``;
- a recursion marker env var makes nested pytest/``run_tests.py`` stand down;
- a no-op under ``--collect-only`` and under xdist workers;
- a no-op for non-git roots;
- refs checked out in a registered worktree **other than the guard's own
  root** (ownership-based exclusion) are excluded (derived from
  ``git worktree list --porcelain``, never a ``wl-*`` pattern), together with
  the ``branch.<name>.*`` config those worktrees own, retained worktree-track
  config (``branch.<name>.remote=.``, surviving ``git worktree remove`` for a
  register→remove cycle wholly inside the guard window) and the
  remote-tracking refs of an excluded branch; the guard root's own branch
  stays monitored (SA-0MV8311B3005DNG8);
- the cheap fingerprint is only a **trigger**: a movement is confirmed against
  the authoritative diff before a test is failed, so excluded-set churn
  (worktree registered/removed mid-run) cannot fail a clean run with
  ``(no differences)`` (SA-0MUINEW6X0034C65);
- the guard covers **every checkout the guarded run can reach**
  (WL-0MVPE2BY9001CR9O): the guarded root plus, when it is a linked worktree,
  its main checkout. A direct ``pytest`` run started inside
  ``<main>/.worklog/worktrees/<wl-…>`` therefore also fails when it unlinks a
  tracked file in ``<main>``, and the message names the mutated checkout and
  path — the 2026-10-26 `packages/shared` wipe was invisible to both guards for
  exactly this reason.

Guard-ownership contract (SA-0MUH1MJL6003767Q)
----------------------------------------------

There is exactly one **outer** guard implementation: ``guarded_run_all`` in
``skill/test/scripts/run_tests.py``. Both the ``run_tests.py`` CLI and
``implement.py``'s finish-gate test step delegate to it, so no entry point is
left unguarded and no path can diverge into a second implementation. The
outer guard owns the checkout (snapshot → run → mutation check) and sets
``LIVE_REPO_GUARD_ACTIVE`` for the duration of the run, which makes this
inner plugin stand down. When an outer guard has already set the marker (a
nested/cascaded run), ``guarded_run_all`` stands down in turn and the
outermost guard remains the single owner — never both, never neither.

Both implementations take their snapshot/diff surface from the same shared
helpers (``snapshot_guarded_roots`` / ``diff_guarded_roots`` /
``describe_guarded_diff`` in ``git_sandbox``), so the main-checkout coverage
cannot drift between them: this plugin is the inner net for direct ``pytest``
invocations, the outer guard for the release/implement path, and neither
reimplements the other's surface.

Working-tree attribution (SA-0MUIJAHXS003M7BG)
----------------------------------------------

Both guards inherit the shared attribution rule from ``git_sandbox``'s
``diff_snapshots``: a working-tree path that was **already non-clean at
snapshot start** and is clean at session end was *resolved* during the run (a
sibling session committing work it had itself dirtied in the shared
checkout), so it is churn rather than a mutation. This plugin therefore runs
that churn as an informational note (``excluded: concurrently resolved
working-tree change …``, accumulated across tests and printed at session end)
instead of failing a test. Blind spots, pinned by
``TestWorkingTreeAttributionBoundaries`` in
``skill/shared/tests/test_git_sandbox.py`` and documented in
``docs/dev/test-isolation.md`` §8.5: masking a suite that reverts or deletes
another session's *uncommitted* work, and invisible content-only changes to
an already-dirty path. The clean→dirty direction is **role-scoped** by the
rule below: full strength on the guarded root, tracked-file deletions only in
the shared main checkout. Derivation is git plumbing (``git status`` codes
across the two snapshots), never a ``wl-*`` or path-name pattern.

Role-scoped main-checkout verdict (WL-0MWR1OXHX001KJC6)
-----------------------------------------------------

This plugin inherits the role-scoped rule straight from
``diff_guarded_roots`` — it runs no comparison of its own. The guarded root
keeps full clean→dirty sensitivity, while a clean→dirty path in the shared
**main checkout** is verdict-bearing only when it reports a tracked-file
deletion (``D`` in either status column, the 2026-10-26 ``packages/shared``
wipe class). A sibling session's mid-run `` M``/``??``/``M `` write to the
main checkout is therefore informational: it becomes an ``excluded: …`` note
accumulated across tests and printed at session end, never a failure. Refs and
config stay fully verdict-bearing on both roots.

Actor second signal (WL-0MWR1OYAJ000UVJS, wired by WL-0MWR1P1F3008AH3V)
-------------------------------------------------------------------------
An excluded note names *what* was excluded — the checkout, path and status
code — and additionally renders the labelled, informational likely owner of
the shared main checkout (``[likely actor: …]``, or an explicit ``unknown``).
The evidence comes from the shared ``concurrent_actor`` helper, loaded by file
location like the git sandbox, and is looked up at most once per session (never
in the per-test fingerprint path). It never changes the verdict: a mutation
still fails and names every mutated checkout and path, with the discoverable
owner appended when the main checkout itself is the mutated checkout.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

#: Marker propagated to subprocesses so nested runs stand down.
MARKER_ENV = "LIVE_REPO_GUARD_ACTIVE"


def _load_git_sandbox() -> Any:
    """Load ``git_sandbox.py`` by file location (stdlib-only, no sys.path)."""
    path = Path(__file__).resolve().parent / "git_sandbox.py"
    spec = importlib.util.spec_from_file_location("_live_repo_git_sandbox", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load git sandbox helper from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_concurrent_actor() -> Any | None:
    """Load the shared actor helper by file location, or ``None``.

    The guard is armed by file location (``conftest.py`` deliberately never
    mutates ``sys.path``), so it cannot rely on a package import. The actor
    signal is informational only: a partial install or a load failure degrades
    to no annotation, never to a guard failure (WL-0MWR1P1F3008AH3V).
    """
    path = Path(__file__).resolve().parent / "concurrent_actor.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "_live_repo_concurrent_actor", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        # Register before execution: the helper uses ``@dataclass``, and
        # ``dataclasses`` resolves the owning class's module through
        # ``sys.modules`` while the class is being built (Python 3.14).
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - evidence must fail open
        sys.modules.pop(spec.name, None)
        return None
    return module


class LiveRepoMutationGuard:
    """A registered pytest plugin that fails a session which mutates a root.

    The guarded surface is every checkout the session can reach — the guarded
    root plus, when it is a linked worktree, its main checkout
    (WL-0MVPE2BY9001CR9O). Cheap per-test fingerprints only *trigger* a
    confirmation against the authoritative multi-root diff.
    """

    def __init__(self, root: Path) -> None:
        self._gs = _load_git_sandbox()
        self.root = Path(root).resolve()
        self.before = self._gs.snapshot_guarded_roots(self.root)
        self._baseline = self._gs.fingerprint_guarded_roots(
            self._gs.snapshot_guarded_roots(self.root, include_status=False)
        )
        self.active = any(
            entry["snapshot"].get("git") for entry in self.before.get("roots", ())
        )
        self.last_test: str | None = None
        self.offender: str | None = None
        #: Informational notes for working-tree churn attributed to a
        #: concurrent session (never a failure) — accumulated across tests so
        #: re-baselining cannot drop them (SA-0MUIJAHXS003M7BG AC3).
        self.excluded: list[str] = []
        #: Shared actor-evidence helper (loaded by file location, fail-open).
        self._actor_module = _load_concurrent_actor()
        #: Cached actor evidence so the bounded lookup runs at most once.
        self._actor_evidence_cached: str | None = None

    def _record_excluded(self, diff: dict[str, Any]) -> None:
        """Accumulate the excluded-churn notes of *diff* (deduplicated)."""
        for note in self._gs.excluded_churn_notes(diff):
            if note not in self.excluded:
                self.excluded.append(note)

    # -- actor evidence ------------------------------------------------------

    def _linked_main_checkout(self) -> Path | None:
        """The shared main checkout of this worktree run, or ``None``.

        A run started in an ordinary checkout has no foreign checkout to
        attribute, so the actor signal is only rendered for a linked worktree
        (where a sibling session may be writing the main checkout).
        """
        main = self._gs.main_checkout_root(self.root)
        if main is None:
            return None
        main = Path(main).resolve()
        return main if main != self.root else None

    def _actor_evidence(self) -> str:
        """Best-effort owner of the main checkout's churn, cached per session.

        Delegates to the shared ``concurrent_actor`` helper (loaded by file
        location, like the git sandbox) so the inner plugin and the outer
        guard render one evidence contract rather than forking it. Computed
        lazily — never in the per-test fingerprint path — and fail-open to
        ``unknown``.
        """
        if self._actor_evidence_cached is not None:
            return self._actor_evidence_cached
        evidence = "unknown"
        main = self._linked_main_checkout()
        module = self._actor_module
        if module is not None and main is not None:
            try:
                evidence = module.describe_concurrent_actor(
                    main,
                    own_worktree=self.root,
                    own_work_item_id=module.work_item_id_from_state(self.root),
                    worklog_dir=main / ".worklog",
                )
            except Exception:  # noqa: BLE001 - evidence must fail open
                evidence = "unknown"
        self._actor_evidence_cached = evidence
        return evidence

    def _excluded_notes_with_actor(self) -> list[str]:
        """The accumulated notes, each carrying the labelled actor signal."""
        notes = list(self.excluded)
        module = self._actor_module
        if module is None or not notes or self._linked_main_checkout() is None:
            return notes
        return module.annotate_excluded_notes(notes, self._actor_evidence())

    def _mutation_actor_line(self, diff: dict[str, Any]) -> str:
        """Name the discoverable owner when the main checkout is mutated."""
        if self._linked_main_checkout() is None:
            return ""
        main_mutated = any(
            entry.get("role") == self._gs.MAIN_CHECKOUT_ROLE
            and (entry.get("diff") or {}).get("changed")
            for entry in diff.get("roots", ())
        )
        if not main_mutated:
            return ""
        return f"\nConcurrent actor evidence: {self._actor_evidence()}"

    # -- hooks ---------------------------------------------------------------

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        self.last_test = nodeid

    # ``pytest_runtest_teardown`` is the hook pytest uses to finalise fixtures
    # (including ``monkeypatch``). ``trylast=True`` makes this implementation run
    # *after* those finalisers, so a test that patch-replaces ``subprocess``
    # (e.g. the audit queue tests) cannot make the guard's own ``subprocess``
    # call observe the replacement and crash the session (SA-0MUH5KFDV002ORSS).
    @pytest.hookimpl(trylast=True)
    def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
        if not self.active:
            return
        current = self._gs.snapshot_guarded_roots(
            self.root, include_status=False
        )
        if self._gs.fingerprint_guarded_roots(current) == self._baseline:
            return
        # The cheap fingerprint moved, but it is only a *trigger*: confirm the
        # movement against the authoritative full diff before failing. An
        # agent worktree registered or removed mid-run moves the fingerprint
        # (its branch enters/leaves the excluded set) yet yields no reportable
        # diff; failing on that produced the bogus
        # ``(no differences)`` failure (SA-0MUINEW6X0034C65).
        after = self._gs.snapshot_guarded_roots(self.root)
        diff = self._gs.diff_guarded_roots(self.before, after)
        # Benign churn is never silent: record the note before re-baselining
        # (the excluded entry leaves ``self.before`` at the reset below).
        self._record_excluded(diff)
        # Reset the baseline so a single mutation is reported once.
        self.before = after
        self._baseline = self._gs.fingerprint_guarded_roots(
            self._gs.snapshot_guarded_roots(self.root, include_status=False)
        )
        if not diff["changed"]:
            return
        self.offender = item.nodeid
        pytest.fail(
            f"live-repo mutation detected during {item.nodeid}:\n"
            + self._gs.describe_guarded_diff(diff)
            + self._mutation_actor_line(diff),
            pytrace=False,
        )

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        if not self.active:
            return
        after = self._gs.snapshot_guarded_roots(self.root)
        diff = self._gs.diff_guarded_roots(self.before, after)
        self._record_excluded(diff)
        # Informational, never a failure: an excluded foreign change must be
        # distinguishable from a clean run (SA-0MUIJAHXS003M7BG AC3), and it
        # says who likely wrote it instead of implying this run corrupted the
        # checkout (WL-0MWR1OYAJ000UVJS, WL-0MWR1P1F3008AH3V).
        for note in self._excluded_notes_with_actor():
            print(f"note: {note}", file=sys.stdout)
        if not diff["changed"]:
            return
        named = self.offender or self.last_test or "<unknown test>"
        session.exitstatus = 1
        print(
            "ERROR: live-repo mutation detected at session end"
            f" (last recorded test: {named}):\n"
            + self._gs.describe_guarded_diff(diff)
            + self._mutation_actor_line(diff),
            file=sys.stderr,
        )


def register(config: Any, root: str | os.PathLike[str] | None = None) -> Any:
    """Arm the guard for *config* unless a stand-down condition applies.

    Returns the guard instance when armed, else None. Called by the repo-root
    ``conftest.py`` and by test fixtures that arm it inside a ``tmp_path`` repo.
    """
    if os.environ.get(MARKER_ENV):
        return None
    if config.getoption("collectonly", False):
        return None
    if hasattr(config, "workerinput"):  # xdist worker: controller owns the check
        return None

    resolved_root = Path(root) if root is not None else _default_root(config)
    guard = LiveRepoMutationGuard(resolved_root)
    if not guard.active:
        return None
    os.environ[MARKER_ENV] = "1"
    config.pluginmanager.register(guard, "live_repo_mutation_guard")
    return guard


def _default_root(config: Any) -> Path:
    for attr in ("rootpath", "rootdir"):
        value = getattr(config, attr, None)
        if value:
            return Path(str(value))
    return Path.cwd()
