
# <!-- REFACTOR-SA-0MVU2G0Y3002UHQK
# smell: unknown
# severity: medium
# description: `subprocess.run` without explicit `check` argument
# -->
"""Subprocess-level two-holder concurrency regression for the per-child lock.

One child work item, two concurrent ``implement.py start`` invocations, one
worklog store: exactly one may enter the child's critical section
(SA-0MVQ5AI56001QXA4; parent SA-0MVOABYW00004978 — "prevent duplicate drive
child sessions").

Everything runs **across the real subprocess boundary** (AC4): each contender
is a real ``python3 skill/implement/scripts/implement.py start <id> --json``
process against a throwaway git repository and a throwaway ``wl`` store. The
lock, the claim, the worktree creation and the item store are the production
implementations — no in-process mock of ``phase_start`` or of the lock.

Deterministic two-holder overlap (no sleep-based race window)
-------------------------------------------------------------
Racing two plain ``start`` processes is inherently nondeterministic: the
winner may finish before the loser even opens the lock file. The race fixture
therefore puts the *winner* on a deliberate gate inside its critical section:
a ``git`` shim on the winner's ``PATH`` signals readiness on a FIFO and then
blocks on an exclusive ``flock`` immediately before ``git worktree add``. The
test holds that flock, starts the winner, waits (``select``, no fixed sleep)
for the readiness signal, starts the loser, joins it, and only then releases
the winner. While the loser runs, the winner is provably alive and holding
the child lock, so "who overlaps whom" is fixed by construction.

AC mapping
----------
- AC1 / parent-AC1 (mutual exclusion): ``test_only_one_..._acquires`` — the
  winner records its pid in the lock file and an independent cross-process
  acquire is refused while it runs; the loser reports
  ``action: "skipped-held"`` with ``held_by`` naming the live holder and
  exits non-zero.
- AC2 (the loser is side-effect free): ``test_loser_performs_no_writes...`` —
  a content snapshot of the worklog store plus ``git status`` and
  ``git worktree list`` is byte-identical before and after the loser runs.
- AC3 (no divergence from the winner's writes): ``test_winner_files...`` —
  exactly one worktree exists and holds only the winner's
  ``.implement_state.json``; the item carries exactly one "Implementation
  started" comment; no state/worktree artefact appeared while only the winner
  was blocked.
- AC5 (SIGTERM release and stale-dead-pid reclamation with a warning) is
  covered by the shared unit slice
  ``skill/shared/tests/test_work_item_lock.py`` (AC4/AC5 there) and is
  deliberately **not** duplicated here; the subprocess-level counterpart
  asserted in this slice is ``test_lock_is_released_when_the_winner_exits``.
- parent-AC5 (regression test): this module, driven by a single deterministic
  two-contender race that every assertion above reads.

Capability gate (test-first slice)
----------------------------------
``phase_start`` is wired to the shared lock by SA-0MVQ5AXLF002PAQ3. Until that
lands, :func:`wired_project` probes one real ``start`` run in a throwaway
fixture and skips cleanly (same convention as ``test_concurrency_guard.py``
and ``test_work_item_lock.py``) instead of failing the suite. Once wired, a
broken integration fails loudly: a lock file recorded by a *failing* start,
or no lock file at the documented path, is an error — not a skip.

Pinned integration contract (consumed by SA-0MVQ5AXLF002PAQ3)
------------------------------------------------------------
- ``<worklog>/locks/child-<id>.lock`` is written by ``phase_start`` before the
  claim/worktree steps and is absent from the worktree.
- A contender that cannot acquire prints its JSON report on stdout with
  ``success: false``, ``action: "skipped-held"`` and
  ``held_by: {agent, pid, session_id, started_at}``, leaves
  ``worktree_path`` empty, exits non-zero, and performs no store write (no
  claim, no comment, no lock-metadata rewrite).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT_FOR_TESTS = REPO_ROOT / "skill"
if str(_SKILLS_ROOT_FOR_TESTS) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT_FOR_TESTS))

pytest.importorskip("shared.work_item_lock")

from shared.work_item_lock import (
    ENV_LOCK_TOKEN,
    WorkItemLock,
    lock_file_path,
)

IMPLEMENT_PY = REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"
REAL_GIT = shutil.which("git")

#: 2-letter prefix, deliberately *not* ``SA``: the shared worklog resolver
#: matches an item-id prefix against sibling projects, and an ``SA`` fixture
#: item would resolve to this repository's real store.
WORK_ITEM_PREFIX = "QZ"

START_TIMEOUT = 300
READY_TIMEOUT = 120


pytestmark = pytest.mark.skipif(
    REAL_GIT is None or shutil.which("wl") is None,
    reason="requires git and wl on PATH to build a real fixture project",
)


# ---------------------------------------------------------------------------
# Throwaway project fixture (real git repo + real wl store)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    """A throwaway git repository with its own initialised worklog store."""

    root: Path
    worklog_dir: Path


def _run(cmd: list[str], cwd: Path, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run *cmd* in *cwd*; raise a descriptive assertion on failure."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"fixture command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def _make_project(base: Path) -> Project:
    """Create a git repo on ``dev`` with an initialised worklog store (``QZ``)."""
    root = base / "repo"
    root.mkdir(parents=True)

    _run([REAL_GIT, "init", "-b", "dev"], root)
    _run([REAL_GIT, "config", "user.email", "child-lock-test@example.com"], root)
    _run([REAL_GIT, "config", "user.name", "child-lock test"], root)
    (root / "README.md").write_text(
        "child-lock two-holder concurrency fixture\n", encoding="utf-8"
    )
    _run([REAL_GIT, "add", "README.md"], root)
    _run([REAL_GIT, "commit", "-m", "fixture: initial commit"], root)

    worklog_dir = root / ".worklog"
    _run(
        [
            "wl", "init",
            "--worklog-dir", str(worklog_dir),
            "--prefix", WORK_ITEM_PREFIX,
            "--project-name", "child-lock fixture",
            "--auto-export", "no",
            "--auto-sync", "no",
            "--agents-template", "skip",
            "--json",
        ],
        root,
    )
    # ``wl init`` writes AGENTS.md / .gitignore / .githooks; commit them so the
    # main-checkout dirty-tree gate sees a clean tree (it protects the main
    # checkout, which is this fixture repo for the spawned starts).
    _run([REAL_GIT, "add", "-A"], root)
    _run([REAL_GIT, "commit", "-m", "fixture: worklog init"], root)
    return Project(root=root, worklog_dir=worklog_dir)


def _create_item(project: Project, title: str) -> str:
    """Create one work item in the fixture store and return its id."""
    proc = _run(
        [
            "wl", "--worklog-dir", str(project.worklog_dir), "create",
            "--title", title,
            "--description", "Child-lock concurrency regression fixture item.",
            "--json",
        ],
        project.root,
    )
    return json.loads(proc.stdout)["workItem"]["id"]


def _show_item(project: Project, item_id: str) -> dict:
    """Fetch the fixture work item (with comments) from the store."""
    proc = _run(
        ["wl", "--worklog-dir", str(project.worklog_dir), "show", item_id, "--json"],
        project.root,
    )
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Contender processes and the deterministic gate
# ---------------------------------------------------------------------------


_GIT_SHIM = '''#!/usr/bin/env python3
"""Test-only git shim (SA-0MVQ5AI56001QXA4).

Blocks ``git worktree add`` until the test releases its flock, so one
``implement.py start`` contender can be held inside its critical section while
a second contender runs. Every other invocation is passed straight through to
the real git binary.
"""

import fcntl
import os
import sys

args = sys.argv[1:]
if args[:2] == ["worktree", "add"]:
    with open(os.environ["CHILD_LOCK_TEST_READY_FIFO"], "w", encoding="utf-8") as fifo:
        fifo.write("blocked\\n")
    gate_fd = os.open(
        os.environ["CHILD_LOCK_TEST_GATE_FILE"], os.O_RDWR | os.O_CREAT, 0o644
    )
    try:
        fcntl.flock(gate_fd, fcntl.LOCK_SH)  # blocks until the test releases
    finally:
        os.close(gate_fd)

real_git = os.environ["CHILD_LOCK_TEST_REAL_GIT"]
os.execv(real_git, [real_git, *args])
'''


def _install_git_shim(directory: Path) -> Path:
    """Write the gated ``git`` shim into a private PATH directory."""
    shim_dir = directory / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(_GIT_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return shim_dir


def _child_env(
    *,
    shim_dir: Path | None = None,
    ready_fifo: Path | None = None,
    gate_file: Path | None = None,
) -> dict[str, str]:
    """Environment for an ``implement.py start`` contender.

    ``IMPLEMENT_CHILD_LOCK_TOKEN`` is removed unconditionally: a contender must
    never inherit this session's driver token, or the loser would re-enter the
    winner's lock as self-owned instead of being refused.
    """
    env = dict(os.environ)
    env.pop(ENV_LOCK_TOKEN, None)
    env.pop("IMPLEMENT_DRIVE_ACTIVE", None)
    # Keep the contender faithful to a production invocation: the fixture's
    # pytest context must not leak into it.
    env.pop("PYTEST_CURRENT_TEST", None)
    if shim_dir is not None:
        assert ready_fifo is not None and gate_file is not None
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
        env["CHILD_LOCK_TEST_READY_FIFO"] = str(ready_fifo)
        env["CHILD_LOCK_TEST_GATE_FILE"] = str(gate_file)
        env["CHILD_LOCK_TEST_REAL_GIT"] = str(REAL_GIT)
    return env


def _launch_start(project: Project, item_id: str, env: dict[str, str]):
    """Spawn ``implement.py start <id> --json`` as a real subprocess."""
    return subprocess.Popen(
        [sys.executable, str(IMPLEMENT_PY), "start", item_id, "--json"],
        cwd=str(project.root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _communicate(proc, label: str, *, timeout: int = START_TIMEOUT) -> tuple[str, str]:
    """Join *proc*; kill it and fail with its output when it overruns."""
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(
            f"{label} did not finish within {timeout}s\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr[-4000:]}"
        )


def _parse_report(stdout: str, stderr: str, label: str) -> dict:
    """Parse the contender's JSON report; fail with stderr when it is absent."""
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{label} did not print a JSON report ({exc})\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr[-4000:]}"
        )
    assert isinstance(report, dict)
    return report


def _await_gated_worktree_add(winner, ready_fifo: Path) -> None:
    """Block until the winner reaches the gated ``git worktree add``.

    Uses ``select`` on the readiness FIFO (no fixed sleep) and fails fast when
    the winner dies before the gate instead of waiting out the timeout.
    """
    fd = os.open(ready_fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            if winner.poll() is not None:
                stdout, stderr = winner.communicate()
                pytest.fail(
                    "the winner exited before reaching the gated "
                    "`git worktree add`, so the two-holder race never overlapped\n"
                    f"rc={winner.returncode}\nstdout:\n{stdout}\n"
                    f"stderr:\n{stderr[-4000:]}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                winner.kill()
                stdout, stderr = winner.communicate()
                pytest.fail(
                    f"the winner did not reach the gated `git worktree add` "
                    f"within {READY_TIMEOUT}s\nstdout:\n{stdout}\n"
                    f"stderr:\n{stderr[-4000:]}"
                )
            readable, _, _ = select.select([fd], [], [], min(0.1, remaining))
            if readable:
                assert os.read(fd, 64), "gate writer closed without signalling"
                return
    finally:
        os.close(fd)


@contextlib.contextmanager
def _without_lock_token():
    """Temporarily unset this session's driver token (probe acquires must not match)."""
    token = os.environ.pop(ENV_LOCK_TOKEN, None)
    try:
        yield
    finally:
        if token is not None:
            os.environ[ENV_LOCK_TOKEN] = token


def _lock_metadata(project: Project, item_id: str) -> dict:
    """Read the child-lock metadata (the winner's recorded ownership)."""
    lock_path = lock_file_path(item_id, project.worklog_dir)
    assert lock_path.is_file(), f"no child lock file at {lock_path}"
    return json.loads(lock_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Side-effect snapshot (AC2)
# ---------------------------------------------------------------------------


_SQLITE_SIDECAR_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")


def _snapshot(project: Project) -> dict[str, str]:
    """Content snapshot of everything the loser must not touch.

    Covers the worklog store (one SHA-256 per file, so any rewritten claim,
    comment or lock metadata shows up), the main checkout's ``git status`` and
    the git worktree registry (worktree creation/removal).
    """
    snapshot: dict[str, str] = {}
    for path in sorted(project.worklog_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in _SQLITE_SIDECAR_SUFFIXES:
            # Transient sqlite sidecars: created/removed by read-only opens and
            # outside the "no file writes" contract.
            continue
        rel = path.relative_to(project.worklog_dir).as_posix()
        snapshot[f"worklog:{rel}"] = hashlib.sha256(path.read_bytes()).hexdigest()

    snapshot["git:status"] = _run(
        [REAL_GIT, "status", "--porcelain"], project.root
    ).stdout
    snapshot["git:worktrees"] = _run(
        [REAL_GIT, "worktree", "list", "--porcelain"], project.root
    ).stdout
    return snapshot


def _snapshot_diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return the snapshot keys whose value changed (or appeared/disappeared)."""
    return [
        key
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wired_project(tmp_path_factory) -> Project:
    """Capability probe: skip until ``phase_start`` takes the child lock."""
    project = _make_project(tmp_path_factory.mktemp("child-lock-probe"))
    item_id = _create_item(project, "Child lock capability probe")
    proc = _launch_start(project, item_id, _child_env())
    stdout, stderr = _communicate(proc, "capability probe start")
    lock_path = lock_file_path(item_id, project.worklog_dir)

    if not lock_path.exists():
        if proc.returncode != 0:
            pytest.skip(
                "phase_start could not run in the throwaway fixture, so the "
                "child-lock capability cannot be probed:\n"
                f"{stderr[-2000:]}"
            )
        pytest.skip(
            "phase_start is not yet wired to the shared per-child lock "
            "(SA-0MVQ5AXLF002PAQ3): no lock file at "
            f"{lock_path.relative_to(project.root).as_posix()} after a "
            "successful start"
        )

    # The lock file exists, so the integration is present: any failure below is
    # a genuine regression, never a skip.
    assert proc.returncode == 0, (
        "phase_start took the child lock but then failed:\n"
        f"stdout:\n{stdout[-2000:]}\nstderr:\n{stderr[-4000:]}"
    )
    report = _parse_report(stdout, stderr, "capability probe start")
    assert report["success"] is True
    assert lock_path.parent == project.worklog_dir / "locks"
    worktree = Path(report["worktree_path"])
    assert not (worktree / "locks").exists(), (
        "the child lock must live in the worklog store, never inside the worktree"
    )
    return project


@dataclass(frozen=True)
class RaceOutcome:
    """Observations of one deterministic two-holder race."""

    project: Project
    item_id: str
    winner_pid: int
    winner_report: dict
    winner_stderr: str
    loser_report: dict
    loser_stderr: str
    loser_returncode: int
    before_loser: dict[str, str]
    after_loser: dict[str, str]
    probe_held_by: dict | None
    winner_alive_after_loser: bool


@pytest.fixture(scope="module")
def race(wired_project, tmp_path_factory) -> RaceOutcome:
    """Run one deterministic two-holder race and collect every observation.

    Depends on :func:`wired_project` so the suite skips (rather than fails) while
    the integration slice is still outstanding.
    """
    base = tmp_path_factory.mktemp("child-lock-race")
    project = _make_project(base)
    item_id = _create_item(project, "Two-holder race child")

    gate_file = base / "worktree-add.gate"
    gate_file.touch()
    ready_fifo = base / "worktree-add.ready"
    os.mkfifo(ready_fifo)
    shim_dir = _install_git_shim(base)

    gate_fd = os.open(gate_file, os.O_RDWR)
    fcntl.flock(gate_fd, fcntl.LOCK_EX)
    winner = _launch_start(
        project,
        item_id,
        _child_env(shim_dir=shim_dir, ready_fifo=ready_fifo, gate_file=gate_file),
    )
    try:
        _await_gated_worktree_add(winner, ready_fifo)

        # The winner is alive *inside* its critical section: the lock file names
        # its pid, and an independent acquire from this process is refused.
        owner = _lock_metadata(project, item_id)
        assert owner["pid"] == winner.pid, (
            f"lock file records pid {owner['pid']!r}, winner is {winner.pid}"
        )
        with _without_lock_token():
            probe_lock = WorkItemLock(
                item_id,
                agent="two-holder-regression-probe",
                session_id="two-holder-regression-probe",
                worklog_dir=project.worklog_dir,
            )
            probe = probe_lock.acquire()
        try:
            assert probe.acquired is False, (
                "an independent holder acquired the child lock while "
                "phase_start was inside its critical section"
            )
            probe_held_by = probe.held_by
        finally:
            probe_lock.release()

        before_loser = _snapshot(project)
        loser = _launch_start(project, item_id, _child_env())
        loser_stdout, loser_stderr = _communicate(loser, "loser start")
        winner_alive_after_loser = winner.poll() is None
        after_loser = _snapshot(project)
    except BaseException:
        if winner.poll() is None:
            winner.kill()
        winner.communicate()
        raise
    finally:
        fcntl.flock(gate_fd, fcntl.LOCK_UN)
        os.close(gate_fd)

    winner_stdout, winner_stderr = _communicate(winner, "winner start")

    return RaceOutcome(
        project=project,
        item_id=item_id,
        winner_pid=winner.pid,
        winner_report=_parse_report(winner_stdout, winner_stderr, "winner start"),
        winner_stderr=winner_stderr,
        loser_report=_parse_report(loser_stdout, loser_stderr, "loser start"),
        loser_stderr=loser_stderr,
        loser_returncode=loser.returncode,
        before_loser=before_loser,
        after_loser=after_loser,
        probe_held_by=probe_held_by,
        winner_alive_after_loser=winner_alive_after_loser,
    )


# ---------------------------------------------------------------------------
# AC1 / parent-AC1 - mutual exclusion
# ---------------------------------------------------------------------------


def test_only_one_of_two_concurrent_starts_acquires_the_child_lock(race: RaceOutcome):
    """Two overlapping starts: the winner proceeds, the loser is refused."""
    assert race.winner_alive_after_loser, (
        "the loser was not concurrent: the winner had already exited"
    )

    # The winner entered the critical section and completed normally.
    assert race.winner_report["success"] is True, race.winner_stderr[-2000:]
    assert Path(race.winner_report["worktree_path"]).is_dir()

    # The loser reports the held child, naming the live holder, and fails.
    loser = race.loser_report
    assert loser["success"] is False, "the held contender must not report success"
    assert loser.get("action") == "skipped-held", (
        f"held contender reported action={loser.get('action')!r}, "
        "expected 'skipped-held'"
    )
    assert race.loser_returncode != 0, "a refused start must exit non-zero"

    held_by = loser.get("held_by") or {}
    for key in ("agent", "pid", "session_id", "started_at"):
        assert key in held_by, f"held_by is missing {key!r}: {held_by!r}"
    assert held_by["pid"] == race.winner_pid, (
        f"held_by names pid {held_by['pid']!r}, the holder is {race.winner_pid}"
    )

    # An independent cross-process acquire while the winner ran was refused too.
    assert race.probe_held_by is not None
    assert race.probe_held_by.get("pid") == race.winner_pid


# ---------------------------------------------------------------------------
# AC2 - the loser is side-effect free
# ---------------------------------------------------------------------------


def test_loser_performs_no_writes_worktree_or_status_mutation(race: RaceOutcome):
    """The refused contender leaves no trace: no file write, no worktree, no update."""
    changed = _snapshot_diff(race.before_loser, race.after_loser)
    assert not changed, (
        "the held contender mutated state it must never touch: "
        f"{changed}\nloser stderr tail:\n{race.loser_stderr[-2000:]}"
    )

    assert not race.loser_report.get("worktree_path"), (
        "the held contender reported a worktree: "
        f"{race.loser_report.get('worktree_path')!r}"
    )
    assert not any(
        key.endswith(".implement_state.json") for key in race.after_loser
    ), "the held contender wrote an implementation state file"


# ---------------------------------------------------------------------------
# AC3 - the winner's writes are not diverged by the loser
# ---------------------------------------------------------------------------


def test_winner_files_are_not_diverged_by_the_loser(race: RaceOutcome):
    """After the winner finishes, the shared location holds only its writes."""
    project = race.project
    item_id = race.item_id
    winner_worktree = Path(race.winner_report["worktree_path"])

    worktrees_dir = project.root / ".worklog" / "worktrees"
    item_worktrees = sorted(
        path for path in worktrees_dir.iterdir() if path.is_dir()
    )
    assert [path.name for path in item_worktrees] == [winner_worktree.name], (
        "exactly one worktree may exist for the child: "
        f"{[path.name for path in item_worktrees]}"
    )

    # The state file is the winner's own record — no loser overwrite.
    state = json.loads(
        (winner_worktree / ".implement_state.json").read_text(encoding="utf-8")
    )
    assert state["work_item_id"] == item_id
    assert state["worktree_path"] == str(winner_worktree)
    assert state["repo_root"] == str(project.root.resolve())
    assert state["parent_branch"] == "dev"

    # The worktree holds exactly the winner's single write (the state file).
    status = _run([REAL_GIT, "status", "--porcelain"], winner_worktree).stdout
    assert status.strip() == "?? .implement_state.json", (
        f"unexpected worktree contents:\n{status}"
    )

    # The item was claimed once by the winner, with one start comment only.
    item = _show_item(project, item_id)
    comments = [comment["comment"] for comment in item["comments"]]
    assert sum(
        comment.startswith("Implementation started") for comment in comments
    ) == 1, f"expected exactly one start comment, got: {comments}"
    assert not any(
        comment.startswith("Implementation resumed") for comment in comments
    ), "a second contender resumed the worktree"
    assert not any("Start phase aborted" in comment for comment in comments), (
        "a contender aborted the start phase"
    )
    assert item["workItem"]["status"] == "in-progress"


# ---------------------------------------------------------------------------
# Release semantics across the boundary
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("wired_project")
def test_lock_is_released_when_the_winner_exits(tmp_path):
    """A completed ``start`` leaves no stale holder: the next one acquires.

    Complements the SIGTERM / dead-pid unit coverage in
    ``skill/shared/tests/test_work_item_lock.py`` with the real
    ``implement.py`` process boundary (normal exit → kernel release → the
    recorded dead pid is reclaimed without blocking).
    """
    project = _make_project(tmp_path)
    item_id = _create_item(project, "Release on exit child")

    first = _launch_start(project, item_id, _child_env())
    first_stdout, first_stderr = _communicate(first, "first start")
    first_report = _parse_report(first_stdout, first_stderr, "first start")
    assert first_report["success"] is True
    assert _lock_metadata(project, item_id)["pid"] == first.pid

    second = _launch_start(project, item_id, _child_env())
    second_stdout, second_stderr = _communicate(second, "second start")
    second_report = _parse_report(second_stdout, second_stderr, "second start")

    assert second_report["success"] is True, second_stderr[-2000:]
    assert second_report.get("resumed") is True, (
        "the second start should resume the existing worktree, not be blocked "
        f"by the exited holder: {second_report}"
    )
    assert _lock_metadata(project, item_id)["pid"] == second.pid
