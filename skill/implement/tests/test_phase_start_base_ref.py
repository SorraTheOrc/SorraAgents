"""Executable harness for ``implement.py start``'s worktree base-ref choice.

Parent: **SA-0MWFFX49M0016KOK** — ``start`` must never fork a worktree from a
local parent branch that is behind ``origin/<parent>``.  This file is the
test-first slice (SA-0MWFFXKKA005CLVK): it builds the hermetic *diverged base*
fixture and pins the current, **fail-open** behaviour the fix must preserve,
so the new behaviour's red-first cases can be written by the implementation
child (SA-0MWFFXNNM00148IX) without ever committing a red tree.

Everything here is **green against the current implementation** — no test is
skipped or marked xfail (AC3).  The reusable pieces, importable by the
implementation child's cases in this same module:

``build_diverged_base(...)``
    A real bare ``origin`` plus a main checkout whose local parent branch has
    a controllable relationship to ``origin/<parent>`` — diverged (default),
    behind-only, ahead-only — and which can be dirty or have its
    main-checkout lock held by the caller.

``worktree_base_report(report)``
    Tolerant reader for ``report["steps"]["worktree_base"]`` — the key the
    implementation child adds.  Absent today, so the reader returns ``{}``
    instead of raising; asserting on the recorded shape binds only once the
    key exists.

``merge_base_with_origin(...)`` / ``upstream_tracking(...)``
    Base-ref assertions: where the new worktree's fork point sits relative to
    ``refs/remotes/origin/<parent>``, and which merge target the new branch
    tracks (``finish`` pushes to ``origin/<parent>``).

Why a real repository: the behaviour under test *is* git's —
``merge --ff-only`` fast-forwarding, ``--track`` wiring and ``git worktree
add`` semantics — so the fixture uses the ``skill/shared/git_sandbox.py``
factories with a real bare remote.  Only process-external boundaries (the
``wl`` store, lifecycle writes, signal handlers, node_modules linking) are
stubbed; ``_sync_parent_branch``, ``_sync_parent_branch_locked``,
``git_worktree_add`` and the real dirty-tree classifier all run.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

_SKILL_ROOT = REPO_ROOT / "skill"
if str(_SKILL_ROOT) not in sys.path:
    sys.path.append(str(_SKILL_ROOT))

from shared import git_sandbox as gs

from skill.implement.scripts import implement

#: Synthetic work item id used by every case here.  The per-child lock store
#: is isolated by this package's ``conftest.py``.
ITEM_ID = "SA-BASEREF"
TITLE = "Worktree base-ref characterisation"

#: Description whose Key Files are unrelated to the fixture's dirty path, so
#: the Key-Files-aware dirty-tree gate judges the checkout's changes
#: irrelevant and lets the phase proceed — the *refresh* still has to skip.
KEY_FILES_DESCRIPTION = (
    "## Key Files\n\n- `skill/implement/tests/test_phase_start_base_ref.py`\n"
)


# ---------------------------------------------------------------------------
# Real-git helpers
# ---------------------------------------------------------------------------


def _clean_stash_result() -> dict:
    """An empty, non-orphaned stash inspection result."""
    return {
        "total_stashes": 0,
        "orphaned_stashes": [],
        "matched_stashes": [],
        "has_orphaned": False,
        "warning": None,
    }


def _write(repo: Path, relative_path: str, text: str) -> Path:
    """Write *text* to *relative_path* inside *repo* (parents created)."""
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rev(repo: Path, rev: str) -> str | None:
    """Full commit hash for *rev* in *repo*, or ``None`` when it does not resolve."""
    proc = gs.run_git(repo, ["rev-parse", "--verify", f"{rev}^{{commit}}"])
    return proc.stdout.strip() or None


def _is_ancestor(repo: Path, commit: str, ref: str) -> bool:
    """True when *commit* is contained in (or equal to) *ref*."""
    return gs.run_git(
        repo, ["merge-base", "--is-ancestor", commit, ref]
    ).returncode == 0


def _ahead_behind(repo: Path, left: str, right: str) -> tuple[int, int]:
    """``(ahead, behind)`` of *left* relative to *right* (any revs/hashes)."""
    proc = gs.run_git(
        repo, ["rev-list", "--left-right", "--count", f"{left}...{right}"]
    )
    ahead, behind = proc.stdout.split()
    return int(ahead), int(behind)


def _config_get(repo: Path, key: str) -> str | None:
    """``git config --get <key>`` in *repo*, or ``None`` when unset."""
    proc = gs.run_git(repo, ["config", "--get", key])
    return proc.stdout.strip() or None


# ---------------------------------------------------------------------------
# Reusable fixture: a repository with a controllable base gap
# ---------------------------------------------------------------------------


@dataclass
class DivergedBase:
    """A hermetic ``origin`` + main checkout pair with a known base gap.

    Attributes:
        origin: The bare remote every checkout shares.
        repo: The main checkout (checked out on :attr:`parent_branch`).
        seed: A second checkout of :attr:`origin` used to publish commits to
            ``origin/<parent>`` without touching :attr:`repo`.
        parent_branch: The long-lived parent branch (``dev``).
        base: The commit both sides started from (clone time).
        origin_tip: ``origin/<parent>`` tip after the origin advances.
        local_tip: Local ``<parent>`` tip after the local advances.
        lock_fd: The held main-checkout lock descriptor, if requested.
    """

    origin: Path
    repo: Path
    seed: Path
    parent_branch: str
    base: str
    origin_tip: str
    local_tip: str
    lock_fd: int | None = None
    _lock_released: bool = field(default=False, repr=False)

    @property
    def local_ref(self) -> str:
        """Fully-qualified local parent branch ref."""
        return f"refs/heads/{self.parent_branch}"

    @property
    def origin_ref(self) -> str:
        """Fully-qualified remote-tracking ref for the parent branch."""
        return f"refs/remotes/origin/{self.parent_branch}"

    def release(self) -> None:
        """Release a lock taken with ``lock_held=True`` (idempotent)."""
        if self.lock_fd is not None and not self._lock_released:
            implement._release_main_checkout_lock(self.lock_fd)
            self._lock_released = True


def build_diverged_base(
    tmp_path: Path,
    *,
    origin_ahead: int = 1,
    local_ahead: int = 1,
    dirty_checkout: bool = False,
    lock_held: bool = False,
    parent_branch: str = "dev",
) -> DivergedBase:
    """Build a real repository whose local parent branch may lag ``origin``.

    ``origin_ahead`` commits are published on ``origin/<parent>`` **after**
    the main checkout was cloned, so its ``refs/remotes/origin/<parent>``
    starts stale — exactly the shape of the reported incident; ``local_ahead``
    commits are then made on the local parent branch.  The defaults
    (``1``/``1``) produce the diverged ``ahead 1, behind 1`` case;
    ``local_ahead=0`` is behind-only, ``origin_ahead=0`` ahead-only.

    Args:
        tmp_path: Sandbox root (each call uses it directly; callers that build
            several scenarios pass distinct subdirectories).
        origin_ahead: Commits to publish on ``origin/<parent>`` after cloning.
        local_ahead: Commits to make on the local parent branch.
        dirty_checkout: Leave an uncommitted change in the main checkout.
        lock_held: Take the main-checkout lock and keep it until
            :meth:`DivergedBase.release`.
        parent_branch: Parent branch name (default ``dev``).

    Returns:
        The populated :class:`DivergedBase`.
    """
    origin = gs.init_bare_remote(tmp_path / "origin.git")
    gs.run_git(
        origin, ["symbolic-ref", "HEAD", f"refs/heads/{parent_branch}"], check=True
    )

    seed = gs.init_repo(tmp_path / "seed", default_branch=parent_branch)
    _write(seed, "seed.txt", "seed\n")
    base = gs.commit_all(seed, "seed commit")
    gs.add_remote(seed, "origin", str(origin))
    gs.run_git(seed, ["push", "-q", "-u", "origin", parent_branch], check=True)

    repo = tmp_path / "repo"
    gs.run_git(tmp_path, ["clone", "-q", str(origin), str(repo)], check=True)

    for index in range(origin_ahead):
        _write(seed, f"origin-{index}.txt", f"origin {index}\n")
        gs.commit_all(seed, f"origin advance {index}")
        gs.run_git(seed, ["push", "-q", "origin", parent_branch], check=True)
    origin_tip = _rev(origin, f"refs/heads/{parent_branch}") or ""

    for index in range(local_ahead):
        _write(repo, f"local-{index}.txt", f"local {index}\n")
        gs.commit_all(repo, f"local advance {index}")
    local_tip = _rev(repo, f"refs/heads/{parent_branch}") or ""

    if dirty_checkout:
        _write(repo, "seed.txt", "seed\nuncommitted change\n")

    lock_fd = None
    if lock_held:
        lock_fd, refusal = implement.acquire_main_checkout_lock(str(repo), ITEM_ID)
        if lock_fd is None:  # pragma: no cover - defensive fixture guard
            raise AssertionError(
                f"fixture could not take the main-checkout lock: {refusal}"
            )

    return DivergedBase(
        origin=origin,
        repo=repo,
        seed=seed,
        parent_branch=parent_branch,
        base=base,
        origin_tip=origin_tip,
        local_tip=local_tip,
        lock_fd=lock_fd,
    )


@pytest.fixture
def base_factory(tmp_path: Path):
    """Factory building :func:`build_diverged_base` scenarios for one test.

    Each call builds into its own subdirectory; every held lock is released at
    teardown, so a failed assertion can never strand one.
    """
    created: list[DivergedBase] = []

    def _build(**kwargs) -> DivergedBase:
        base = build_diverged_base(tmp_path / f"case-{len(created)}", **kwargs)
        created.append(base)
        return base

    yield _build

    for base in reversed(created):
        base.release()


# ---------------------------------------------------------------------------
# `start` harness: real git, stubbed process-external boundaries
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _start_harness(*, description: str = ""):
    """Patch ``phase_start``'s process-external boundaries only.

    ``_sync_parent_branch``, ``_sync_parent_branch_locked``,
    ``git_worktree_add``, the dirty-tree classifier and every git command run
    for real against the fixture repository; only the worklog store, the
    lifecycle writes, signal handlers, state file and node_modules linking are
    stubbed.  Yields the list of work-item comments the phase recorded.
    """
    comments: list[str] = []
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(implement, "is_code_freeze_active", return_value=False))
        enter(
            patch.object(
                implement,
                "check_orphaned_stashes",
                return_value=_clean_stash_result(),
            )
        )
        enter(patch.object(implement, "wl_dep_blockers", return_value=[]))
        enter(
            patch.object(
                implement,
                "wl_show",
                return_value={
                    "id": ITEM_ID,
                    "title": TITLE,
                    "description": description,
                    "risk": "Medium",
                    "effort": "S",
                },
            )
        )
        enter(
            patch.object(
                implement,
                "_check_and_evaluate_risk_effort",
                return_value={
                    "success": True,
                    "estimates_provided": True,
                    "risk": "Medium",
                    "effort": "S",
                },
            )
        )
        enter(patch.object(implement, "load_implement_extension", return_value=None))
        enter(patch.object(implement, "_parse_expected_dirty", return_value=set()))
        enter(
            patch.object(
                implement,
                "warn_self_referential_upstream",
                return_value={"status": "ok", "defect_count": 0},
            )
        )
        enter(
            patch.object(
                implement,
                "_prepare_node_modules",
                return_value={
                    "link_created": False,
                    "shared_resolution_ok": True,
                    "shared_resolution": {
                        "applicable": False,
                        "ok": True,
                        "resolved_root": None,
                    },
                },
            )
        )
        enter(
            patch.object(
                implement, "_ensure_nested_node_modules_symlinks", return_value=0
            )
        )
        enter(patch.object(implement, "_ensure_submodules", return_value=True))
        enter(patch.object(implement, "_store_signal_globals"))
        enter(patch.object(implement, "_register_signal_handlers"))
        enter(patch.object(implement, "write_state"))
        enter(
            patch.object(
                implement,
                "wl_add_comment",
                side_effect=lambda _item, text: comments.append(text),
            )
        )
        enter(patch.object(implement, "StatusLifecycle", MagicMock()))
        yield comments


def _run_start(
    base: DivergedBase,
    monkeypatch: pytest.MonkeyPatch,
    *,
    description: str = "",
) -> dict:
    """Run a real ``phase_start`` inside *base* and return its report (any verdict)."""
    monkeypatch.chdir(base.repo)
    with _start_harness(description=description):
        return implement.phase_start(ITEM_ID, json_output=True)


def _start(
    base: DivergedBase,
    monkeypatch: pytest.MonkeyPatch,
    *,
    description: str = "",
) -> tuple[dict, Path]:
    """Run ``phase_start`` and require a created worktree; return ``(report, worktree)``."""
    report = _run_start(base, monkeypatch, description=description)
    assert report["success"] is True, report
    worktree = Path(report["worktree_path"])
    assert worktree.is_dir(), report
    return report, worktree


# ---------------------------------------------------------------------------
# Base-ref assertion helpers (reusable by the implementation child)
# ---------------------------------------------------------------------------


def merge_base_with_origin(cwd: Path, parent_branch: str = "dev") -> str | None:
    """Merge base of ``HEAD`` in *cwd* with ``refs/remotes/origin/<parent>``."""
    proc = gs.run_git(
        cwd, ["merge-base", "HEAD", f"refs/remotes/origin/{parent_branch}"]
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def upstream_tracking(repo: Path, branch: str) -> tuple[str | None, str | None]:
    """``(remote, merge)`` tracking config recorded for *branch* in *repo*."""
    return (
        _config_get(repo, f"branch.{branch}.remote"),
        _config_get(repo, f"branch.{branch}.merge"),
    )


def worktree_base_report(report: dict) -> dict:
    """Tolerant reader for ``report["steps"]["worktree_base"]``.

    The current implementation records no base ref, so every report lacks the
    key.  Returning an empty mapping — never raising, never xfail — keeps the
    characterisation cases green today while letting the implementation
    child's cases assert on the recorded ``{ref, reason, ahead, behind}``
    shape without rewriting this helper.
    """
    steps = report.get("steps") or {}
    recorded = steps.get("worktree_base") or {}
    return dict(recorded)


def _forked_from_a_known_parent(base: DivergedBase, worktree: Path) -> bool:
    """True when the worktree's HEAD is a known local or published parent commit.

    The fail-open fork points at the local parent branch today; the fix may
    fork from ``refs/remotes/origin/<parent>``.  Both are accepted — the
    assertion is that the worktree starts inside the parent's history (the
    local tip, the published tip, or a commit contained in one of them),
    never at an unrelated commit.
    """
    tip = _rev(worktree, "HEAD") or ""
    if not tip:
        return False
    return any(
        _is_ancestor(base.repo, tip, ref)
        for ref in (base.local_tip, base.origin_tip)
    )


# ---------------------------------------------------------------------------
# The fixture covers every required scenario shape (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin_ahead", "local_ahead", "expected_counts"),
    [
        (1, 1, (1, 1)),  # diverged: ahead 1, behind 1
        (1, 0, (0, 1)),  # behind-only local parent
        (0, 1, (1, 0)),  # ahead-only local parent
    ],
    ids=["diverged", "behind-only", "ahead-only"],
)
def test_builder_produces_the_requested_base_shape(
    base_factory, origin_ahead, local_ahead, expected_counts
):
    """The builder publishes/clones/advances in the documented order (AC2).

    The main checkout's remote-tracking ref is deliberately stale (clone-time)
    so a refresh has something to fetch, and the local/published relationship
    matches the requested scenario exactly.
    """
    base = base_factory(origin_ahead=origin_ahead, local_ahead=local_ahead)

    assert _rev(base.repo, base.origin_ref) == base.base, (
        "the cloned checkout's origin/<parent> ref must stay at clone time "
        "until a refresh fetches it"
    )
    # Fetch the advanced remote-tracking ref so both tips are in the
    # checkout's object store before counting (the clone predates them).
    gs.run_git(base.repo, ["fetch", "-q", "origin", base.parent_branch], check=True)
    assert _ahead_behind(base.repo, base.local_ref, base.origin_ref) == expected_counts
    assert _rev(base.repo, base.local_ref) == base.local_tip
    if origin_ahead and local_ahead:
        merge_base = gs.run_git(
            base.repo,
            ["merge-base", base.local_tip, base.origin_tip],
        ).stdout.strip()
        assert merge_base == base.base


def test_builder_supports_dirty_and_lock_held_scenarios(base_factory):
    """The dirty-checkout and lock-held knobs are usable independently (AC2)."""
    dirty = base_factory(dirty_checkout=True)
    status = gs.run_git(dirty.repo, ["status", "--porcelain=v1"])
    assert "seed.txt" in status.stdout

    locked = base_factory(lock_held=True)
    assert locked.lock_fd is not None
    second_fd, refusal = implement.acquire_main_checkout_lock(
        str(locked.repo), f"{ITEM_ID}-contest"
    )
    assert second_fd is None
    assert refusal is not None
    locked.release()


# ---------------------------------------------------------------------------
# Characterisation: behaviour that must survive the fix
# ---------------------------------------------------------------------------


def test_behind_only_start_fast_forwards_the_local_parent_and_forks_from_it(
    base_factory, monkeypatch
):
    """A behind-only local parent is fast-forwarded, so the fork point is published.

    The refreshed base must be ``origin/<parent>`` itself: ``start`` claims a
    driven child starts from the published base when the refresh succeeds.
    """
    base = base_factory(origin_ahead=1, local_ahead=0)

    report, worktree = _start(base, monkeypatch)

    sync = report["steps"]["sync_parent_branch"]
    assert sync["method"] == "synced", sync
    assert sync["success"] is True, sync
    assert _rev(base.repo, base.local_ref) == base.origin_tip
    assert _rev(worktree, "HEAD") == base.origin_tip
    assert merge_base_with_origin(worktree, base.parent_branch) == base.origin_tip


def test_ahead_only_local_parent_is_never_force_updated(base_factory, monkeypatch):
    """An ahead-only local parent keeps its commits; ``start`` never moves it.

    Characterises the invariant the fix must preserve (parent AC4), not the
    exact verdict: whether ``start`` refuses today or forks from
    ``origin/<parent>`` after the fix, the local branch must never be
    force-updated, and any worktree created must sit inside the parent's
    known history.
    """
    base = base_factory(origin_ahead=0, local_ahead=1)
    before = _rev(base.repo, base.local_ref)

    report = _run_start(base, monkeypatch)

    sync = report["steps"]["sync_parent_branch"]
    assert sync["method"] == "skipped", sync
    assert sync["reason"] == "diverged", sync
    assert sync["success"] is True, sync
    assert _rev(base.repo, base.local_ref) == before == base.local_tip, (
        "the local parent branch was moved — an ahead-only branch must never "
        "be force-updated or reset by start"
    )
    if report["success"]:
        worktree = Path(report["worktree_path"])
        assert worktree.is_dir(), report
        assert _forked_from_a_known_parent(base, worktree)
    else:
        assert report.get("worktree_refused") is True, report


def test_diverged_local_parent_is_never_force_updated(base_factory, monkeypatch):
    """A diverged local parent keeps its commits; ``start`` never moves it."""
    base = base_factory()  # diverged: ahead 1, behind 1
    before = _rev(base.repo, base.local_ref)

    report = _run_start(base, monkeypatch)

    sync = report["steps"]["sync_parent_branch"]
    assert sync["method"] == "skipped", sync
    assert sync["reason"] == "diverged", sync
    assert _rev(base.repo, base.local_ref) == before == base.local_tip, (
        "the local parent branch was moved — a diverged branch must never be "
        "force-updated or reset by start"
    )
    assert not _is_ancestor(base.repo, base.local_tip, base.origin_tip), (
        "the fixture's local parent should still hold its local-only commit"
    )
    if report["success"]:
        worktree = Path(report["worktree_path"])
        assert worktree.is_dir(), report
        assert _forked_from_a_known_parent(base, worktree)
    else:
        assert report.get("worktree_refused") is True, report


def test_held_main_checkout_lock_skips_the_refresh_and_still_starts(
    base_factory, monkeypatch
):
    """A concurrent lock holder skips the refresh; ``start`` still succeeds (fail open)."""
    base = base_factory(origin_ahead=1, local_ahead=0, lock_held=True)
    # Fail fast instead of waiting out the production 30 s bound.
    monkeypatch.setenv(implement.ENV_START_LOCK_TIMEOUT, "0")

    report, worktree = _start(base, monkeypatch)

    sync = report["steps"]["sync_parent_branch"]
    assert sync["method"] == "skipped", sync
    assert sync["reason"] == "lock_held", sync
    assert sync["success"] is True, sync
    assert _rev(base.repo, base.local_ref) == base.local_tip, (
        "a skipped refresh must leave the local parent branch exactly as it was"
    )
    assert _forked_from_a_known_parent(base, worktree)


def test_dirty_main_checkout_skips_the_refresh_and_still_starts(
    base_factory, monkeypatch
):
    """A dirty main checkout skips the refresh; ``start`` still succeeds (fail open)."""
    base = base_factory(origin_ahead=1, local_ahead=0, dirty_checkout=True)

    report, worktree = _start(base, monkeypatch, description=KEY_FILES_DESCRIPTION)

    sync = report["steps"]["sync_parent_branch"]
    assert sync["method"] == "skipped", sync
    assert sync["reason"] == "dirty_working_tree", sync
    assert sync["success"] is True, sync
    assert report["dirty_decision"]["dirty"] is True, report["dirty_decision"]
    assert report["dirty_decision"]["can_proceed"] is True, report["dirty_decision"]
    assert "seed.txt" in report["dirty_worktree_left_behind"]
    assert _rev(base.repo, base.local_ref) == base.local_tip
    assert _forked_from_a_known_parent(base, worktree)


def test_failed_fetch_fails_open_and_forks_from_the_local_branch(
    base_factory, monkeypatch
):
    """An unreachable origin cannot be refreshed, so the local branch stays the base.

    With no counts available the current base decision must be preserved
    exactly (parent AC4): ``start`` succeeds and the worktree forks from the
    local parent branch.
    """
    base = base_factory(origin_ahead=1, local_ahead=0)
    gs.run_git(
        base.repo,
        ["remote", "set-url", "origin", str(base.repo / "missing-origin.git")],
        check=True,
    )

    report, worktree = _start(base, monkeypatch)

    sync = report["steps"]["sync_parent_branch"]
    assert sync["method"] == "skipped", sync
    assert sync["reason"] == "fetch_failed", sync
    assert sync["success"] is True, sync
    assert _rev(base.repo, base.local_ref) == base.local_tip
    assert _rev(worktree, "HEAD") == base.local_tip, (
        "with the remote unreachable the local parent branch is still the base"
    )


def test_worktree_branch_tracks_the_parent_branch(base_factory, monkeypatch):
    """``--track`` wiring survives: the new branch's upstream is the parent branch.

    ``finish`` pushes to ``origin/<parent>``, so the created branch must keep a
    resolvable upstream naming the parent branch.  Today ``--track`` records
    the *local* start point (``remote="."``); forking from a remote-tracking
    ref records ``remote="origin"`` — both name ``refs/heads/<parent>`` as the
    merge target and both are accepted here.
    """
    base = base_factory(origin_ahead=1, local_ahead=0)  # worktree is created

    report, worktree = _start(base, monkeypatch)

    branch = report["branch"]
    remote, merge = upstream_tracking(base.repo, branch)
    assert merge == f"refs/heads/{base.parent_branch}", (
        f"branch {branch} tracks {merge!r}, expected the parent branch "
        f"{base.parent_branch!r}"
    )
    assert remote in (".", "origin"), (
        f"branch {branch} tracks remote {remote!r}; expected the local parent "
        f"('.') or origin (post-fix remote-tracking fork point)"
    )
    assert _rev(worktree, "@{upstream}") is not None, (
        f"branch {branch}'s configured upstream does not resolve inside the "
        f"created worktree"
    )


# ---------------------------------------------------------------------------
# `report["steps"]["worktree_base"]` reader (introduced by the fix)
# ---------------------------------------------------------------------------


def test_worktree_base_reader_tolerates_the_pre_fix_report(base_factory, monkeypatch):
    """The reader never raises when the base key is absent (pre-fix shape)."""
    assert worktree_base_report({}) == {}
    assert worktree_base_report({"steps": {}}) == {}
    assert worktree_base_report(
        {"steps": {"sync_parent_branch": {"method": "synced"}}}
    ) == {}

    base = base_factory(origin_ahead=1, local_ahead=0)
    report, _ = _start(base, monkeypatch)
    assert isinstance(worktree_base_report(report), dict)


def test_worktree_base_reader_returns_the_recorded_base():
    """When the fix records a base, the reader returns it unchanged."""
    recorded = {
        "ref": "refs/remotes/origin/dev",
        "reason": "behind",
        "ahead": 0,
        "behind": 2,
    }
    assert worktree_base_report({"steps": {"worktree_base": recorded}}) == recorded


def test_recorded_worktree_base_agrees_with_the_created_worktree(
    base_factory, monkeypatch
):
    """The recorded base resolves to a commit the worktree actually contains.

    Today ``start`` records no base ref, so this case asserts the
    characterisation surface only (the worktree was forked from a known parent
    ref).  Once the implementation child records ``steps.worktree_base``, the
    same test binds the recorded ref to the worktree's real fork point.
    """
    base = base_factory(origin_ahead=1, local_ahead=0)

    report, worktree = _start(base, monkeypatch)

    assert _forked_from_a_known_parent(base, worktree)
    recorded = worktree_base_report(report)
    if recorded:
        ref = recorded.get("ref") or recorded.get("base")
        assert ref, recorded
        resolved = _rev(base.repo, ref)
        assert resolved is not None, recorded
        assert _is_ancestor(base.repo, resolved, _rev(worktree, "HEAD") or ""), (
            f"recorded base {ref!r} is not contained in the worktree HEAD"
        )


# ---------------------------------------------------------------------------
# New behaviour (SA-0MWFFXNNM00148IX): fork from origin/<parent> whenever the
# local parent is not the published tip — ahead-only, behind-only or diverged
# — and record the base ref plus ahead/behind counts.  Written red-first
# against the pre-fix tree (which refuses ahead/diverged and keeps a stale
# local base for a skipped behind-only refresh).
# ---------------------------------------------------------------------------


def test_diverged_local_parent_forks_from_origin(base_factory, monkeypatch):
    """Diverged local parent: the worktree forks origin, never the mutant local ref."""
    base = base_factory()  # ahead 1, behind 1
    local_before = _rev(base.repo, base.local_ref)

    report, worktree = _start(base, monkeypatch)

    assert _rev(worktree, "HEAD") == base.origin_tip
    assert merge_base_with_origin(worktree, base.parent_branch) == base.origin_tip
    recorded = worktree_base_report(report)
    assert recorded.get("ref") == base.origin_ref, recorded
    assert recorded.get("reason") == "diverged", recorded
    assert (recorded.get("ahead"), recorded.get("behind")) == (1, 1), recorded
    # Never force-update the diverged local ref.
    assert _rev(base.repo, base.local_ref) == local_before == base.local_tip


def test_ahead_only_local_parent_forks_from_origin(base_factory, monkeypatch):
    """Ahead-only local parent: fork origin (drop the unpushed commit from the base)."""
    base = base_factory(origin_ahead=0, local_ahead=1)
    local_before = _rev(base.repo, base.local_ref)

    report, worktree = _start(base, monkeypatch)

    assert _rev(worktree, "HEAD") == base.origin_tip
    recorded = worktree_base_report(report)
    assert recorded.get("ref") == base.origin_ref, recorded
    assert recorded.get("reason") == "ahead_only", recorded
    assert (recorded.get("ahead"), recorded.get("behind")) == (1, 0), recorded
    assert _rev(base.repo, base.local_ref) == local_before == base.local_tip


def test_behind_only_lock_held_forks_from_origin(base_factory, monkeypatch):
    """A held lock skips the fast-forward; the worktree still forks the published tip."""
    base = base_factory(origin_ahead=1, local_ahead=0, lock_held=True)
    monkeypatch.setenv(implement.ENV_START_LOCK_TIMEOUT, "0")

    report, worktree = _start(base, monkeypatch)

    assert _rev(worktree, "HEAD") == base.origin_tip
    recorded = worktree_base_report(report)
    assert recorded.get("ref") == base.origin_ref, recorded
    assert recorded.get("reason") == "behind_remote", recorded
    assert _rev(base.repo, base.local_ref) == base.local_tip  # not fast-forwarded


def test_behind_only_dirty_checkout_forks_from_origin(base_factory, monkeypatch):
    """A dirty checkout skips the fast-forward; the worktree still forks origin."""
    base = base_factory(origin_ahead=1, local_ahead=0, dirty_checkout=True)

    report, worktree = _start(base, monkeypatch, description=KEY_FILES_DESCRIPTION)

    assert _rev(worktree, "HEAD") == base.origin_tip
    assert worktree_base_report(report).get("ref") == base.origin_ref
    assert _rev(base.repo, base.local_ref) == base.local_tip


def test_ac3_worktree_contains_the_published_commit(base_factory, monkeypatch):
    """AC3: a behind-only local parent's worktree already carries origin's commit.

    The published commit adds ``origin-0.txt``; a driven child forked from the
    stale local parent would lack it and would need a manual rebase before its
    first changed-scope gate.  A held lock forces the refresh to skip so the
    local parent genuinely stays stale.
    """
    base = base_factory(origin_ahead=1, local_ahead=0, lock_held=True)
    monkeypatch.setenv(implement.ENV_START_LOCK_TIMEOUT, "0")

    _report, worktree = _start(base, monkeypatch)

    assert (worktree / "origin-0.txt").exists(), (
        "the worktree must contain the commit origin/dev published after the "
        "local clone — otherwise a driven child starts behind the published base"
    )
    assert not (base.repo / "origin-0.txt").exists(), (
        "sanity: the stale local parent must not have the published commit"
    )


def test_origin_fork_branch_tracks_origin(base_factory, monkeypatch):
    """The origin-forked branch keeps an upstream naming origin/<parent> (finish pushes there)."""
    base = base_factory()  # diverged

    report, _worktree = _start(base, monkeypatch)

    branch = report["branch"]
    remote, merge = upstream_tracking(base.repo, branch)
    assert merge == f"refs/heads/{base.parent_branch}", (remote, merge)
    assert remote == "origin", (remote, merge)


# ---------------------------------------------------------------------------
# AC2: the start comment names the refresh outcome too
# ---------------------------------------------------------------------------


def _start_with_comments(
    base: DivergedBase,
    monkeypatch: pytest.MonkeyPatch,
    *,
    description: str = "",
) -> tuple[dict, list[str]]:
    """Run ``phase_start`` in *base* and return ``(report, recorded comments)``."""
    monkeypatch.chdir(base.repo)
    with _start_harness(description=description) as comments:
        report = implement.phase_start(ITEM_ID, json_output=True)
    return report, comments


def _base_note(comments: list[str]) -> str:
    """The comment line naming the worktree base (``- Base: ...``)."""
    for text in comments:
        for line in text.splitlines():
            if line.strip().startswith("- Base:"):
                return line
    raise AssertionError(f"no '- Base:' line recorded: {comments!r}")


def test_skipped_refresh_reason_is_surfaced_in_the_start_comment(
    base_factory, monkeypatch
):
    """AC2: a skipped refresh states why, not just which base was used.

    The lock-held refresh makes the local parent genuinely stale; the start
    comment must name the base ref, the base-ref reason, the ahead/behind
    counts AND the refresh skip reason — otherwise an operator cannot tell a
    deliberate ``origin`` fork from a failed refresh.
    """
    base = base_factory(origin_ahead=1, local_ahead=0, lock_held=True)
    monkeypatch.setenv(implement.ENV_START_LOCK_TIMEOUT, "0")

    report, comments = _start_with_comments(base, monkeypatch)

    assert report["success"] is True, report
    note = _base_note(comments)
    assert "refs/remotes/origin/dev" in note, note
    assert "behind_remote" in note, note
    assert "ahead 0 behind 1" in note, note
    assert "lock_held" in note, note
    recorded = worktree_base_report(report)
    assert recorded.get("refresh") == {"method": "skipped", "reason": "lock_held"}, (
        recorded
    )


def test_synced_refresh_note_names_the_sync_and_not_a_skip(
    base_factory, monkeypatch
):
    """AC2: a successful refresh is recorded as synced, with no skip wording."""
    base = base_factory(origin_ahead=1, local_ahead=0)  # refresh fast-forwards

    report, comments = _start_with_comments(base, monkeypatch)

    assert report["success"] is True, report
    recorded = worktree_base_report(report)
    assert recorded.get("refresh", {}).get("method") == "synced", recorded
    note = _base_note(comments)
    assert "lock_held" not in note, note
    assert "refresh skipped" not in note, note
