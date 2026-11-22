
# <!-- REFACTOR-SA-0MWRQZQ210043IPM
# smell: unknown
# severity: low
# description: Unparenthesized implicit string concatenation in collection
# -->
"""Unit tests for ``skill/shared/concurrent_actor.py`` (WL-0MWR1OYAJ000UVJS).

The helper names the *likely* concurrent actor behind a dirty main checkout.
Its contract is deliberately narrow:

- attribution is a documented second signal, informational only — it never
  changes a guard verdict and is never used to excuse a tracked-file deletion;
- the evidence sources are the ``.implement_state.json`` files (the main
  checkout's own and its sibling worktrees'), one bounded ``wl`` claim lookup,
  the main-checkout lock holder, and (supplementarily) live processes whose cwd
  is the main checkout;
- every undiscoverable case degrades to ``unknown`` and malformed evidence is
  never raised into the caller. The four required fallbacks are pinned below:
  no state file, several candidate owners, unreadable JSON, and a lock held by
  an unrelated process.

The tests are hermetic file fixtures: the ``wl`` lookup is always injected, and
the ``/proc`` scan is disabled unless a test drives it explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

from shared import concurrent_actor
from shared.concurrent_actor import (
    DEFAULT_WL_LOOKUP_LIMIT,
    DEFAULT_WL_LOOKUP_TIMEOUT_SECONDS,
    STATE_FILE_NAME,
    UNKNOWN,
    describe_concurrent_actor,
)
from shared.repo_lock import DEFAULT_LOCK_RELPATH


def _wl_items(*items: dict) -> subprocess.CompletedProcess:
    """A successful ``wl list`` result carrying *items*."""
    return subprocess.CompletedProcess(
        args=["wl"],
        returncode=0,
        stdout=json.dumps({"success": True, "workItems": list(items)}),
        stderr="",
    )


def _empty_wl(_command: list[str], _timeout: float) -> subprocess.CompletedProcess:
    return _wl_items()


def _describe(repo: Path, **kwargs) -> str:
    """Call the helper with the ambient ``/proc`` scan and real ``wl`` off."""
    kwargs.setdefault("include_process_scan", False)
    kwargs.setdefault("wl_runner", _empty_wl)
    return describe_concurrent_actor(repo, **kwargs)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A plain main checkout with an initialised ``.worklog`` store."""
    root = tmp_path / "main"
    store = root / ".worklog"
    (store / "worktrees").mkdir(parents=True)
    # ``initialized`` is the marker ``wl`` uses; its presence makes the
    # bounded claim lookup eligible (otherwise the source is skipped).
    (store / "initialized").write_text("", encoding="utf-8")
    return root


def _write_state(
    worktree: Path,
    work_item_id: str,
    *,
    repo_root: Path,
    started_at: str = "2026-01-01T00:00:00Z",
) -> Path:
    """Write an ``.implement_state.json`` into *worktree* and return its path."""
    worktree.mkdir(parents=True, exist_ok=True)
    path = worktree / STATE_FILE_NAME
    path.write_text(
        json.dumps(
            {
                "work_item_id": work_item_id,
                "worktree_path": str(worktree),
                "repo_root": str(repo_root),
                "started_at": started_at,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_lock(
    repo_root: Path,
    metadata: dict,
) -> Path:
    """Write the main-checkout lock file's diagnostic holder metadata."""
    path = repo_root / DEFAULT_LOCK_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# State-file evidence
# ---------------------------------------------------------------------------


def test_sibling_worktree_state_file_names_the_owner(repo: Path):
    """A registered sibling's state file names its work item and worktree."""
    sibling = repo / ".worklog" / "worktrees" / "wl-OTHER"
    _write_state(sibling, "WL-SIBLING", repo_root=repo)

    evidence = _describe(repo)

    assert "work item WL-SIBLING" in evidence
    assert str(sibling) in evidence
    assert "started 2026-01-01T00:00:00Z" in evidence


def test_main_checkout_state_file_names_the_owner(repo: Path):
    """A session started directly in the main checkout is named too."""
    _write_state(repo, "WL-MAIN", repo_root=repo, started_at="")

    evidence = _describe(repo)

    assert "work item WL-MAIN" in evidence
    assert "started" not in evidence


def test_no_state_file_is_unknown(repo: Path):
    """Required fallback: no state file, no claim, no lock -> ``unknown``."""
    assert _describe(repo) == UNKNOWN


def test_several_candidate_owners_is_unknown(repo: Path):
    """Required fallback: ambiguous sibling owners -> ``unknown`` (never guess)."""
    _write_state(
        repo / ".worklog" / "worktrees" / "wl-A", "WL-A", repo_root=repo
    )
    _write_state(
        repo / ".worklog" / "worktrees" / "wl-B", "WL-B", repo_root=repo
    )

    assert _describe(repo) == UNKNOWN


def test_unreadable_json_is_unknown(repo: Path):
    """Required fallback: malformed JSON degrades to ``unknown``, never raises."""
    broken = repo / ".worklog" / "worktrees" / "wl-BROKEN" / STATE_FILE_NAME
    broken.parent.mkdir(parents=True)
    broken.write_text('{"work_item_id": "WL-BROKEN",', encoding="utf-8")

    assert _describe(repo) == UNKNOWN


def test_own_worktree_and_item_are_excluded(repo: Path):
    """The caller's own state file is never attributed to a competing actor."""
    own = repo / ".worklog" / "worktrees" / "wl-OWN"
    _write_state(own, "WL-OWN", repo_root=repo)

    assert _describe(repo, own_worktree=own, own_work_item_id="WL-OWN") == UNKNOWN


def test_state_file_for_another_checkout_is_ignored(repo: Path):
    """A state file whose ``repo_root`` is another checkout is not evidence."""
    other = repo / ".worklog" / "worktrees" / "wl-OTHER"
    _write_state(other, "WL-OTHER", repo_root=repo.parent / "elsewhere")

    assert _describe(repo) == UNKNOWN


# ---------------------------------------------------------------------------
# Lock-holder evidence
# ---------------------------------------------------------------------------


def test_lock_holder_names_its_work_item(repo: Path):
    """A live holder recording a work item and this repo is named."""
    _write_lock(
        repo,
        {
            "work_item_id": "WL-LOCK",
            "pid": os.getpid(),
            "started_at": "2026-01-01T00:00:00Z",
            "repo_root": str(repo),
        },
    )

    evidence = _describe(repo)

    assert "work item WL-LOCK" in evidence
    assert "main-checkout lock holder" in evidence


def test_lock_held_by_an_unrelated_process_is_unknown(repo: Path):
    """Required fallback: a lock holder naming another checkout is not evidence."""
    _write_lock(
        repo,
        {
            "work_item_id": "WL-ELSEWHERE",
            "pid": os.getpid(),
            "repo_root": str(repo.parent / "elsewhere"),
        },
    )

    assert _describe(repo) == UNKNOWN


def test_lock_without_a_work_item_is_unknown(repo: Path):
    """A bare pid cannot name an owner, so the result stays ``unknown``."""
    _write_lock(repo, {"pid": os.getpid(), "repo_root": str(repo)})

    assert _describe(repo) == UNKNOWN


def test_lock_held_by_a_dead_process_is_unknown(repo: Path):
    """Stale holder metadata (dead pid) is never a candidate."""
    _write_lock(
        repo,
        {
            "work_item_id": "WL-DEAD",
            "pid": 2_147_483_646,
            "repo_root": str(repo),
        },
    )

    assert _describe(repo) == UNKNOWN


def test_own_lock_holder_is_excluded(repo: Path):
    """``finish`` holds this very lock; it never names itself as the actor."""
    _write_lock(
        repo,
        {
            "work_item_id": "WL-SELF",
            "pid": os.getpid(),
            "repo_root": str(repo),
        },
    )

    assert _describe(repo, own_work_item_id="WL-SELF") == UNKNOWN


# ---------------------------------------------------------------------------
# Claimed-item evidence (bounded Wl lookup)
# ---------------------------------------------------------------------------


def test_claimed_item_names_the_owner(repo: Path):
    """A claimed in-progress item is named when it is the only candidate."""
    seen: list[tuple[list[str], float]] = []

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        seen.append((command, timeout))
        return _wl_items({"id": "WL-CLAIM", "assignee": "agent-7"})

    evidence = _describe(repo, wl_runner=runner)

    assert "work item WL-CLAIM" in evidence
    assert "claimed by agent-7" in evidence
    # Bounded: exactly one lookup, with the documented limit and timeout.
    assert len(seen) == 1
    command, timeout = seen[0]
    assert command[-2:] == ["-n", str(DEFAULT_WL_LOOKUP_LIMIT)]
    assert timeout == DEFAULT_WL_LOOKUP_TIMEOUT_SECONDS


def test_unclaimed_items_are_not_evidence(repo: Path):
    """An in-progress item with no assignee is not a claimed owner."""

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        return _wl_items({"id": "WL-NOCLAIM", "assignee": ""})

    assert _describe(repo, wl_runner=runner) == UNKNOWN


def test_several_claimed_items_is_unknown(repo: Path):
    """Several live claims cannot be disambiguated, so the result is unknown."""

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        return _wl_items(
            {"id": "WL-A", "assignee": "agent-a"},
            {"id": "WL-B", "assignee": "agent-b"},
        )

    assert _describe(repo, wl_runner=runner) == UNKNOWN


def test_own_claimed_item_is_excluded(repo: Path):
    """The caller's own claim never names itself as the competing actor."""

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        return _wl_items({"id": "WL-OWN", "assignee": "me"})

    assert _describe(repo, wl_runner=runner, own_work_item_id="WL-OWN") == UNKNOWN


def test_wl_timeout_is_fail_open(repo: Path):
    """A timed-out claim lookup degrades to ``unknown``, never raises."""

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    assert _describe(repo, wl_runner=runner) == UNKNOWN


def test_wl_error_is_fail_open(repo: Path):
    """A failed/malformed claim lookup degrades to ``unknown``, never raises."""

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=command, returncode=1, stdout="not-json", stderr="boom"
        )

    assert _describe(repo, wl_runner=runner) == UNKNOWN


def test_absent_worklog_store_skips_the_lookup(tmp_path: Path):
    """A repo without ``.worklog`` never resolves an ambient store."""
    repo = tmp_path / "bare"
    repo.mkdir()
    calls: list[list[str]] = []

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        calls.append(command)
        return _wl_items({"id": "WL-AMBIENT", "assignee": "someone"})

    assert _describe(repo, wl_runner=runner) == UNKNOWN
    assert calls == []


# ---------------------------------------------------------------------------
# Live-process evidence
# ---------------------------------------------------------------------------


def test_live_process_cwd_is_reported_when_no_owner_is_known(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """A pid whose cwd is the main checkout names the session (bounded)."""
    monkeypatch.setattr(concurrent_actor.os, "listdir", lambda _path: ["123", "nope"])
    monkeypatch.setattr(
        concurrent_actor.os, "readlink", lambda _path: str(repo)
    )

    evidence = _describe(repo, include_process_scan=True)

    assert evidence == "pid 123 with cwd in the main checkout"


def test_live_process_pid_is_appended_to_a_named_owner(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """When an owner is known, the visible session pid is a second clause."""
    _write_state(repo / ".worklog" / "worktrees" / "wl-OWNER", "WL-OWNER", repo_root=repo)
    monkeypatch.setattr(concurrent_actor.os, "listdir", lambda _path: ["456"])
    monkeypatch.setattr(
        concurrent_actor.os, "readlink", lambda _path: str(repo)
    )

    evidence = _describe(repo, include_process_scan=True)

    assert "work item WL-OWNER" in evidence
    assert "pid 456 with cwd in the main checkout" in evidence


# ---------------------------------------------------------------------------
# Read-only, fail-open contract
# ---------------------------------------------------------------------------


def _tree_digest(root: Path) -> dict[str, bytes]:
    digest: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = path.read_bytes()
    return digest


def test_helper_is_read_only(repo: Path):
    """Attribution never writes: the checkout's bytes are unchanged."""
    _write_state(repo / ".worklog" / "worktrees" / "wl-OTHER", "WL-OTHER", repo_root=repo)
    _write_lock(
        repo,
        {
            "work_item_id": "WL-LOCK",
            "pid": os.getpid(),
            "repo_root": str(repo),
        },
    )
    before = _tree_digest(repo)

    _describe(repo)

    assert _tree_digest(repo) == before


def test_unresolvable_repo_root_is_unknown():
    """A missing/invalid repository path never raises."""
    assert describe_concurrent_actor("/does/not/exist", include_process_scan=False) == UNKNOWN


# ---------------------------------------------------------------------------
# Rendering helpers (WL-0MWR1P1F3008AH3V)
# ---------------------------------------------------------------------------
# The outer guard, the inner plugin and the finish report all render the actor
# signal through these helpers, so the second signal has one format and a note
# that already carries it is never annotated twice as it crosses a boundary.


def test_annotate_excluded_notes_appends_unknown_signal():
    """A note without evidence gains an explicit `unknown` actor signal."""
    notes = ["excluded: concurrently resolved working-tree change in /repo: a.py [ M]"]

    assert concurrent_actor.annotate_excluded_notes(notes, None) == [
        notes[0] + " [likely actor: unknown]"
    ]


def test_annotate_excluded_notes_names_the_evidence():
    notes = ["excluded: concurrently resolved working-tree change in /repo: a.py [ M]"]

    assert concurrent_actor.annotate_excluded_notes(
        notes, "work item WL-SIBLING (claimed by agent)"
    ) == [notes[0] + " [likely actor: work item WL-SIBLING (claimed by agent)]"]


def test_annotate_excluded_notes_does_not_double_annotate():
    """A note the guard already annotated is left untouched."""
    annotated = [
        "excluded: concurrently resolved working-tree change in /repo: a.py [ M] "
        "[likely actor: WL-SIBLING]"
    ]

    assert concurrent_actor.annotate_excluded_notes(annotated, "WL-OTHER") == annotated


def test_actor_evidence_from_note_round_trips():
    note = concurrent_actor.annotate_excluded_notes(["excluded: x"], "WL-X")[0]

    assert concurrent_actor.actor_evidence_from_note(note) == "WL-X"
    assert concurrent_actor.actor_evidence_from_note("excluded: x") == ""


def test_work_item_id_from_state_reads_the_state_file(tmp_path):
    worktree = tmp_path / "wt"
    _write_state(worktree, "WL-OWN", repo_root=tmp_path)

    assert concurrent_actor.work_item_id_from_state(worktree) == "WL-OWN"


def test_work_item_id_from_state_is_empty_for_missing_or_malformed(tmp_path):
    assert concurrent_actor.work_item_id_from_state(tmp_path / "absent") == ""

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / STATE_FILE_NAME).write_text("{not json", encoding="utf-8")
    assert concurrent_actor.work_item_id_from_state(broken) == ""
