"""Best-effort, read-only attribution of a dirty main checkout to an actor.

When the guard reports a dirty main checkout it is tempting to read the note as
"the running item corrupted the shared checkout", sending an operator hunting a
phantom bug in a clean worktree. This module names the *likely* concurrent
actor instead (WL-0MWR1OYAJ000UVJS, parent WL-0MWFKDFFA009N3OC, producer choice
(c)).

Attribution is a **documented second signal**, never a substitute for the
git-plumbing rule that produced the verdict: it never turns a green run red or
a red run green, and it is never derived from a path name or a ``wl-*`` name
heuristic. When ownership cannot be established the helper returns
:data:`UNKNOWN`, and every error degrades to that same value — evidence is
diagnostic only and is never raised into the verdict path.

Evidence sources, in priority order, each optional:

1. ``.implement_state.json`` — the main checkout's own state file and the
   state files of its registered sibling worktrees
   (``<repo_root>/.worklog/worktrees/*/.implement_state.json``). A state file
   is a candidate only when its ``repo_root`` (when present) resolves to this
   main checkout and its ``worktree_path`` (when present) lives inside it.
2. Claimed work items — one **bounded** ``wl list --status in-progress`` lookup
   (:data:`DEFAULT_WL_LOOKUP_TIMEOUT_SECONDS` timeout, :data:`DEFAULT_WL_LOOKUP_LIMIT`
   rows) filtered to items with a non-empty assignee. A claim is durable
   evidence that a session is working the item; liveness is not directly
   observable, which is why this source is only ever used to *name* an actor.
3. The main-checkout lock holder metadata — ``repo_lock.RepoLock.read_holder``
   / :func:`shared.repo_lock.describe_holder`, consulted only when the holder
   records a live pid and a work item that is not this run's own.
4. Live processes whose ``/proc/<pid>/cwd`` resolves to the main checkout
   (Linux only; bounded to :data:`MAX_PROCESS_EVIDENCE`). These name a session
   (pid) rather than a work item.

A source that yields more than one distinct owner is **ambiguous**: the helper
returns :data:`UNKNOWN` rather than guessing, and does not fall through to a
lower-priority source (a lock holder cannot disambiguate two active siblings).
A source that yields nothing is skipped. Only when no source names a single
owner does the helper fall back to live-process evidence, and only when that is
absent too is the result :data:`UNKNOWN`.

The helper is read-only: it reads state files and the lock file's diagnostic
metadata and runs at most one bounded ``wl`` command. It performs no writes and
does no work in the per-test guard path — callers invoke it only when building
the finished-run note or a failure message (WL-0MWR1OYAJ000UVJS Constraints).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Returned whenever no single owner can be established.
UNKNOWN = "unknown"

#: Label prefixing the actor second signal wherever it is rendered — the outer
#: guard's ``live_repo_excluded`` notes, the inner plugin's session output and
#: the finish report. Shared so every consumer renders one format and a note
#: can be recognised as already annotated (WL-0MWR1P1F3008AH3V).
ACTOR_EVIDENCE_LABEL = "likely actor"

#: The rendered marker of an annotated note, e.g. ``[likely actor: unknown]``.
ACTOR_EVIDENCE_MARKER = f"[{ACTOR_EVIDENCE_LABEL}:"

#: State file written by ``implement.py`` into a worktree (``STATE_FILE_NAME``).
STATE_FILE_NAME = ".implement_state.json"

#: Worktree state files live under the main checkout's ``.worklog`` store.
_WORKTREES_SUBDIR = Path(".worklog") / "worktrees"

#: Hard timeout for the single bounded ``wl`` lookup, in seconds.
DEFAULT_WL_LOOKUP_TIMEOUT_SECONDS = 5.0

#: Upper bound on rows requested from the ``wl`` lookup (never unbounded).
DEFAULT_WL_LOOKUP_LIMIT = 50

#: Upper bound on ``/proc`` cwd matches reported.
MAX_PROCESS_EVIDENCE = 8

#: Injectable command runner: ``(cmd, timeout) -> CompletedProcess``.
WlRunner = Callable[[list[str], float], subprocess.CompletedProcess]


@dataclass(frozen=True)
class ActorCandidate:
    """One named candidate owner and the human-readable evidence for it."""

    work_item_id: str
    description: str
    source: str


def describe_concurrent_actor(
    repo_root: str | os.PathLike[str],
    *,
    own_worktree: str | os.PathLike[str] | None = None,
    own_work_item_id: str | None = None,
    worklog_dir: str | os.PathLike[str] | None = None,
    wl_timeout: float = DEFAULT_WL_LOOKUP_TIMEOUT_SECONDS,
    wl_limit: int = DEFAULT_WL_LOOKUP_LIMIT,
    wl_runner: WlRunner | None = None,
    include_process_scan: bool = True,
) -> str:
    """Describe the likely concurrent actor writing *repo_root*, or ``unknown``.

    Args:
        repo_root: The **main checkout** whose dirty paths are being attributed.
        own_worktree: The caller's own worktree; its state file and any item it
            names are excluded so the caller never attributes itself.
        own_work_item_id: The caller's own work item id, excluded from every
            source (including the main-checkout lock, which the caller holds
            while finishing). ``""`` is treated as no exclusion.
        worklog_dir: The initialised ``.worklog`` store to query. When omitted,
            ``<repo_root>/.worklog`` is used **only when it exists** — an absent
            store skips the claimed-item source rather than resolving an
            unrelated ambient store (which would make a hermetic fixture name
            an unrelated live session).
        wl_timeout: Hard timeout for the single ``wl`` lookup, in seconds.
        wl_limit: Upper bound on rows requested from the ``wl`` lookup.
        wl_runner: Optional injectable runner for tests.
        include_process_scan: Set ``False`` to disable the ``/proc`` scan (used
            by hermetic tests that must not see the host's own processes).

    Returns:
        A human-readable evidence string naming the single likely owner (with a
        ``pid`` clause when a live process is also visible), a ``pid`` clause
        when only a live session is visible, or :data:`UNKNOWN`.
    """
    root = _resolve(repo_root)
    if root is None:
        return UNKNOWN

    own_worktree_path = _resolve(own_worktree) if own_worktree else None
    own_id = str(own_work_item_id or "").strip()

    sources: tuple[Callable[[], list[ActorCandidate]], ...] = (
        lambda: _state_file_candidates(root, own_worktree_path, own_id),
        lambda: _claimed_item_candidates(
            root,
            worklog_dir,
            own_id,
            wl_timeout=wl_timeout,
            wl_limit=wl_limit,
            wl_runner=wl_runner,
        ),
        lambda: _lock_holder_candidates(root, own_id),
    )

    for source in sources:
        try:
            candidates = source()
        except Exception:  # noqa: BLE001 - evidence is diagnostic only
            # Fail-open: a malformed/missing source is never raised into the
            # verdict path; it simply yields no evidence.
            candidates = []
        unique = _unique_by_id(candidates)
        if len(unique) > 1:
            # Several candidate owners: attribution declines rather than guess.
            return UNKNOWN
        if len(unique) == 1:
            evidence = unique[0].description
            if include_process_scan:
                pids = _live_process_pids(root)
                if pids:
                    evidence = f"{evidence}; {_render_pids(pids)}"
            return evidence

    pids = _live_process_pids(root) if include_process_scan else []
    return _render_pids(pids) if pids else UNKNOWN


def work_item_id_from_state(worktree: str | os.PathLike[str]) -> str:
    """The work item id recorded in *worktree*'s ``.implement_state.json``.

    Returns ``""`` when the state file is absent, unreadable, malformed or
    carries no id. Callers use it to exclude their own item from the evidence
    (e.g. from the main-checkout lock a finish run holds), so it is
    intentionally fail-open.
    """
    root = _resolve(worktree)
    if root is None:
        return ""
    try:
        data = json.loads((root / STATE_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("work_item_id") or "").strip()


def render_actor_evidence(evidence: str | None) -> str:
    """Render the labelled actor second signal, falling back to ``unknown``."""
    value = str(evidence or "").strip() or UNKNOWN
    return f"[{ACTOR_EVIDENCE_LABEL}: {value}]"


def annotate_excluded_notes(
    notes: list[str] | tuple[str, ...], evidence: str | None
) -> list[str]:
    """Append the labelled actor second signal to each excluded-churn *note*.

    The note itself (path, status code and checkout) is the verdict-bearing
    plumbing rule; the actor is an explicitly labelled, informational second
    signal that never replaces it. A note that already carries the signal is
    left untouched so the same exclusion cannot be annotated twice as it
    passes from the outer guard to the finish report
    (WL-0MWR1P1F3008AH3V).
    """
    signal = render_actor_evidence(evidence)
    return [
        note if ACTOR_EVIDENCE_MARKER in note else f"{note} {signal}"
        for note in notes
    ]


def actor_evidence_from_note(note: str) -> str:
    """The actor evidence already rendered in *note*, or ``""`` when absent."""
    marker = ACTOR_EVIDENCE_MARKER
    start = note.find(marker)
    if start < 0:
        return ""
    end = note.find("]", start + len(marker))
    if end < 0:
        return ""
    return note[start + len(marker):end].strip()


# ---------------------------------------------------------------------------
# Evidence sources
# ---------------------------------------------------------------------------


def _state_file_candidates(
    repo_root: Path,
    own_worktree: Path | None,
    own_work_item_id: str,
) -> list[ActorCandidate]:
    """Candidates named by the main checkout's and sibling worktrees' state files."""
    candidates: list[ActorCandidate] = []
    for state_path in _state_file_paths(repo_root):
        candidate = _state_file_candidate(
            state_path, repo_root, own_worktree, own_work_item_id
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _state_file_paths(repo_root: Path) -> list[Path]:
    """Every state file that could name a sibling session of *repo_root*.

    The main checkout's own state file is included for the case where a session
    was started directly in the main checkout. Sibling worktree state files are
    discovered by scanning the worktrees directory (never by a ``wl-*`` name
    pattern, so a differently named registered worktree is still seen).
    """
    paths: list[Path] = []
    own_state = repo_root / STATE_FILE_NAME
    if own_state.is_file():
        paths.append(own_state)
    worktrees = repo_root / _WORKTREES_SUBDIR
    try:
        if worktrees.is_dir():
            paths.extend(sorted(worktrees.glob(f"*/{STATE_FILE_NAME}")))
    except OSError:
        pass
    return paths


def _state_file_candidate(
    state_path: Path,
    repo_root: Path,
    own_worktree: Path | None,
    own_work_item_id: str,
) -> ActorCandidate | None:
    """Parse one state file into a candidate, or ``None`` when it is not one.

    Unreadable or malformed JSON is skipped (fail-open, never raised). A state
    file is rejected when it names this run's own item/worktree, or when it
    belongs to another checkout (its ``repo_root`` resolves elsewhere, or its
    ``worktree_path`` lives outside this repository).
    """
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    work_item_id = str(data.get("work_item_id") or "").strip()
    if not work_item_id:
        return None
    if own_work_item_id and work_item_id == own_work_item_id:
        return None

    worktree = _resolve(data.get("worktree_path")) or _resolve(state_path.parent)
    if own_worktree is not None and worktree == own_worktree:
        return None
    if worktree is not None and worktree != repo_root and not _is_within(
        worktree, repo_root
    ):
        return None

    stated_repo_root = _resolve(data.get("repo_root"))
    if stated_repo_root is not None and stated_repo_root != repo_root:
        return None

    started_at = str(data.get("started_at") or "").strip()
    detail = f"work item {work_item_id} (worktree {worktree or state_path.parent}"
    if started_at:
        detail += f", started {started_at}"
    detail += ")"
    return ActorCandidate(work_item_id, detail, "state-file")


def _claimed_item_candidates(
    repo_root: Path,
    worklog_dir: str | os.PathLike[str] | None,
    own_work_item_id: str,
    *,
    wl_timeout: float,
    wl_limit: int,
    wl_runner: WlRunner | None,
) -> list[ActorCandidate]:
    """Candidates from one bounded ``wl list --status in-progress`` lookup.

    Only items carrying a non-empty assignee are candidates: an assignee is the
    durable form of a claim. The lookup is skipped entirely when the repository
    has no initialised ``.worklog`` store, so a hermetic fixture never queries
    an unrelated ambient store (which is what would let a test's clean temp
    repo report a live session from the developer's real store).
    """
    store = _worklog_store(repo_root, worklog_dir)
    if store is None:
        return []

    limit = max(1, int(wl_limit))
    command = [
        "wl",
        "--worklog-dir",
        str(store),
        "list",
        "--status",
        "in-progress",
        "--json",
        "-n",
        str(limit),
    ]
    runner = wl_runner or _default_wl_runner
    try:
        proc = runner(command, max(0.1, float(wl_timeout)))
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if getattr(proc, "returncode", 1) != 0:
        return []
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "")
    except (TypeError, ValueError):
        return []
    items = payload.get("workItems") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    candidates: list[ActorCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        work_item_id = str(item.get("id") or "").strip()
        assignee = str(item.get("assignee") or "").strip()
        if not work_item_id or not assignee:
            continue
        if own_work_item_id and work_item_id == own_work_item_id:
            continue
        candidates.append(
            ActorCandidate(
                work_item_id,
                f"work item {work_item_id} (claimed by {assignee})",
                "claimed-item",
            )
        )
    return candidates


def _lock_holder_candidates(
    repo_root: Path, own_work_item_id: str
) -> list[ActorCandidate]:
    """The main-checkout lock holder, when it names a live, unrelated item.

    Holder metadata is diagnostic only (the kernel ``flock`` is the lock), so a
    holder without a live pid or without a work item is not a candidate — an
    unrelated or stale lock file therefore degrades to ``unknown`` instead of
    naming a phantom owner. This run's own item is excluded because ``finish``
    holds this very lock.
    """
    try:
        from .repo_lock import RepoLock, describe_holder
    except ImportError:  # pragma: no cover - layout fallbacks
        try:
            from shared.repo_lock import (  # type: ignore[no-redef]
                RepoLock,
                describe_holder,
            )
        except ImportError:
            from repo_lock import RepoLock, describe_holder  # type: ignore[no-redef]

    holder = RepoLock(repo_root).read_holder()
    if not isinstance(holder, dict):
        return []

    work_item_id = str(holder.get("work_item_id") or "").strip()
    if not work_item_id:
        return []
    if own_work_item_id and work_item_id == own_work_item_id:
        return []

    stated_repo_root = _resolve(holder.get("repo_root"))
    if stated_repo_root is not None and stated_repo_root != repo_root:
        return []

    pid = holder.get("pid")
    if pid is None or not _pid_alive(pid):
        return []

    return [
        ActorCandidate(
            work_item_id,
            f"main-checkout lock holder: {describe_holder(holder)}",
            "lock-holder",
        )
    ]


# ---------------------------------------------------------------------------
# Live-process evidence
# ---------------------------------------------------------------------------


def _live_process_pids(repo_root: Path) -> list[int]:
    """PIDs whose ``/proc/<pid>/cwd`` resolves to *repo_root* (bounded).

    Linux only; any other platform (or an unreadable ``/proc``) yields an empty
    list. The scan is bounded by :data:`MAX_PROCESS_EVIDENCE` and never raises.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    pids: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            continue
        try:
            if Path(cwd).resolve() != repo_root:
                continue
        except OSError:
            continue
        pids.append(int(entry))
        if len(pids) >= MAX_PROCESS_EVIDENCE:
            break
    return pids


def _render_pids(pids: list[int]) -> str:
    """Render live-process evidence as a single human-readable clause."""
    return "; ".join(f"pid {pid} with cwd in the main checkout" for pid in pids)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _default_wl_runner(
    command: list[str], timeout: float
) -> subprocess.CompletedProcess:
    """Run a ``wl`` command with a hard timeout (never an unbounded lookup)."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _worklog_store(
    repo_root: Path, worklog_dir: str | os.PathLike[str] | None
) -> Path | None:
    """The **initialised** ``.worklog`` store to query, or ``None``.

    A candidate is accepted only when it carries the initialisation marker
    (``initialized`` file or ``worklog.db``), the same contract as
    ``shared.status_lifecycle._is_initialized_worklog``. Pointing ``wl`` at an
    uninitialised directory would fail at best and risk a write at worst, so
    the helper skips the claim source entirely instead — it is read-only by
    contract.
    """
    store = _resolve(worklog_dir) if worklog_dir else repo_root / ".worklog"
    if store is None or not store.is_dir():
        return None
    if (store / "initialized").is_file() or (store / "worklog.db").is_file():
        return store
    return None


def _unique_by_id(candidates: list[ActorCandidate]) -> list[ActorCandidate]:
    """Collapse duplicate owner ids, preserving the first description seen."""
    unique: dict[str, ActorCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.work_item_id, candidate)
    return list(unique.values())


def _pid_alive(pid: Any) -> bool:
    """True when *pid* names a live process (best effort, POSIX ``kill -0``)."""
    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric <= 0:
        return False
    try:
        os.kill(numeric, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another user
    except OSError:
        return True
    return True


def _resolve(value: Any) -> Path | None:
    """Resolve *value* to an absolute path, or ``None`` when it is unusable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    """True when *path* is *root* or lives under it."""
    if path == root:
        return True
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
