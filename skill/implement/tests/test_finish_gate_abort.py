"""F5: the finish gate's abort contract (SA-0MVQHVCRE0070YO6).

What this module pins
---------------------
The finish gate (``phase_finish``'s test step) is about to gain **temp-index
staging** (F6, SA-0MVQHVGFR006W8LG): the change set is applied to a *copy* of
the real index, the suite runs with ``GIT_INDEX_FILE`` pointing at that copy,
and the real index is only touched once the gate is green. That design makes a
contract possible which did not exist before, and which this module specifies
**test-first** so F6 can be implemented against it:

* **AC1/AC4 — no partial commit on abort.** A failing gate (test failure,
  aborted fix loop, or an exception escaping the gate) creates no commit:
  ``HEAD`` is unchanged.
* **AC2 — the real index and working tree are untouched.** On every abort path
  ``git diff --cached --quiet`` is true (nothing staged), the real index is
  byte-identical to its pre-gate state, the staged entry set and the index
  tree are identical, and every worktree file's bytes are identical.
* **AC3 — the temporary index is removed on every exit path** (success,
  failure, abort, exception).

How the invariants are checked without guessing F6's implementation
-------------------------------------------------------------------
Two *independent* detectors, both implementation-agnostic:

1. **Observed-path detector (strongest).** The fixture suite records the
   ``GIT_INDEX_FILE`` it was actually given (plus the real index's staged
   entries, read with ``GIT_INDEX_FILE`` dropped) into an out-of-repo probe
   file. Every observed temp-index path must be gone once the gate returns —
   wherever F6 chooses to put it, whatever it calls it.
2. **Artefact detector (fallback).** The repository's absolute git directory
   is snapshotted for index-shaped files (any name containing ``index``, other
   than the real index) and an isolated, test-owned temp directory is
   snapshotted the same way; a path that appears and is still there afterwards
   is a leak. This catches a leak even when the suite never observed the path
   (for example a temp index created *after* the suite ran).

The prologue test (``test_probe_records_the_gate_suite_environment``) is the
detector's canary: it proves the probe really captures the gate's suite
environment, so an unset ``GIT_INDEX_FILE`` means "no temp index in play"
rather than "the detector is broken".

Tracer-bullet convention
------------------------
``test_every_exit_path_uses_and_removes_the_temporary_index`` is
``xfail(strict=True)`` until F6 lands: today the gate runs the suite against
the loose working tree with no temp index at all, so the "a temp index was
used" assertion cannot hold. When F6 lands and cleans up correctly the test
passes and — being a strict xfail — fails loudly as an ``XPASS``, forcing the
marker's removal in the F6 work item (the same convention as the F1/F2 markers
in ``test_finish_gate_commit_tree.py`` and F4's in
``test_finish_gate_provenance.py``).

The invariant tests themselves pass **today** and must keep passing after F6:
they are the regression guards against a fix that stages into the real index
on a failure path, or that leaks its temp index.

Hermetic by construction: a temp git repository (``git_sandbox`` factories, no
environment leakage) plus the F1 module's stubbed finish boundaries (build,
commit, push, worktree removal, the ``wl``/``StatusLifecycle`` store). The
gate's **test step is real**: the fixture suite genuinely executes.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

from skill.implement.scripts import implement

# The F1 harness: the stubbed finish boundaries and the gate-report accessors.
from skill.implement.tests.test_finish_gate_commit_tree import (
    _assert_real_gate_execution,
    _gate_test_attempt,
    _stubbed_finish_boundaries,
)

#: Out-of-repo file the fixture suite appends its environment record to. The
#: suite only writes when it is set, so the fixture is inert outside these
#: tests.
PROBE_FILE_ENV = "IMPLEMENT_GATE_ABORT_PROBE_FILE"

#: The fixture suite's two tests. The probe always runs; the failure is only
#: part of fixtures built for a red gate.
PROBE_TEST_NAME = "test_gate_probe_records_environment"
FAILING_TEST_NAME = "test_gate_deliberate_failure"
SUITE_TEST_REL = "tests/test_gate_abort_probe.py"

#: The untracked file that forms the aborted change set. Untracked, so a
#: residual ``git add -A`` in the real index is unmistakable.
CHANGE_SET_REL = "change-set.md"

_SUITE_SOURCE = '''"""Fixture suite for the finish gate's abort contract (F5).

Records the environment the gate actually ran the suite in — notably
``GIT_INDEX_FILE`` and the real index's staged entries — into an out-of-repo
file, so the abort contract can be checked from outside without guessing how
the temp index is named or where it lives.
"""

import json
import os
import subprocess


def _git(args, *, drop_git_index):
    """Run git, optionally hiding an exported ``GIT_INDEX_FILE``.

    Dropping the variable is how the *real* index is inspected from inside a
    suite that the gate may have pointed at a temporary index.
    """
    env = dict(os.environ)
    if drop_git_index:
        env.pop("GIT_INDEX_FILE", None)
    completed = subprocess.run(
        ["git", *args], capture_output=True, text=True, env=env, check=False
    )
    return completed.stdout.splitlines()


def __PROBE_NAME__():
    record = {
        "cwd": os.getcwd(),
        "git_index_file": os.environ.get("GIT_INDEX_FILE"),
        "staged_with_ambient_index": _git(
            ["diff", "--cached", "--name-only"], drop_git_index=False
        ),
        "staged_in_real_index": _git(
            ["diff", "--cached", "--name-only"], drop_git_index=True
        ),
        "head": _git(["rev-parse", "HEAD"], drop_git_index=True)[:1],
    }
    target = os.environ.get("__PROBE_ENV__")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\\n")


def __FAILING_NAME__():
    assert False, "deliberate gate failure for the abort contract"
'''


# ---------------------------------------------------------------------------
# Fixture repository
# ---------------------------------------------------------------------------


def _build_fixture_repo(tmp_path: Path, *, gate_fails: bool) -> Path:
    """A hermetic repo whose suite is green (or carries one deliberate failure).

    The seed commit carries the probe suite (and, for a red gate, the failing
    test); the *change set* is the untracked :data:`CHANGE_SET_REL` — exactly
    the shape whose staging a broken abort path would leave behind in the real
    index.
    """
    repo = gs.init_repo(tmp_path / "abort-repo", default_branch="dev")
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Abort-contract fixture\n", encoding="utf-8")

    names = {"PROBE_NAME": PROBE_TEST_NAME, "PROBE_ENV": PROBE_FILE_ENV}
    source = _SUITE_SOURCE
    for token, value in names.items():
        source = source.replace(f"__{token}__", value)
    source = source.replace("__FAILING_NAME__", FAILING_TEST_NAME)
    if not gate_fails:
        source = source.replace(
            f"\n\ndef {FAILING_TEST_NAME}():\n"
            '    assert False, "deliberate gate failure for the abort contract"\n',
            "\n",
        )
        assert FAILING_TEST_NAME not in source, source

    (repo / "tests").mkdir()
    (repo / SUITE_TEST_REL).write_text(source, encoding="utf-8")
    gs.commit_all(repo, "seed abort-contract fixture suite")

    (repo / CHANGE_SET_REL).write_text("# Change set\n", encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# Gate harness — real test step, stubbed external boundaries
# ---------------------------------------------------------------------------


def _drive_gate(
    repo: Path,
    tmp_path: Path,
    monkeypatch,
    *,
    exit_path: str = "success",
) -> dict | None:
    """Drive ``phase_finish`` through *exit_path*; return its report.

    ``exit_path`` selects the gate outcome:

    * ``success`` — green fixture, default retry budget;
    * ``failure`` — red fixture, JSON mode (the report is returned on the
      first red attempt so the agent can loop externally);
    * ``abort`` — red fixture with ``max_retry=0``: the fix loop is exhausted
      immediately and finish aborts;
    * ``exception`` — the suite really runs and then the gate raises: a crash
      *after* the suite (the case in which a temp index has certainly been
      created) must still leave nothing behind. ``None`` is returned because
      finish never produces a report on this path.
    """
    # An ambient override would replace the fixture's suite with an unrelated
    # command, hiding the gate's own tree/temp-index behaviour.
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)

    with _stubbed_finish_boundaries(repo, tmp_path):
        if exit_path == "exception":
            real_run_tests = implement.run_tests

            def _crash_after_the_suite(*args, **kwargs):
                real_run_tests(*args, **kwargs)
                raise RuntimeError("simulated crash after the gate suite ran")

            with patch.object(
                implement, "run_tests", side_effect=_crash_after_the_suite
            ), pytest.raises(RuntimeError, match="simulated crash"):
                implement.phase_finish(
                    "SA-TEST", json_output=True, no_refactor=True
                )
            return None

        kwargs = {"max_retry": 0} if exit_path == "abort" else {}
        return implement.phase_finish(
            "SA-TEST", json_output=True, no_refactor=True, **kwargs
        )


# ---------------------------------------------------------------------------
# Probe records — the observed temp-index detector
# ---------------------------------------------------------------------------


def _probe_path(tmp_path: Path, monkeypatch) -> Path:
    """Point the fixture suite's probe at an out-of-repo file and return it."""
    probe = tmp_path / "gate-probe.jsonl"
    monkeypatch.setenv(PROBE_FILE_ENV, str(probe))
    return probe


def _probe_records(probe: Path) -> list[dict]:
    if not probe.exists():
        return []
    return [
        json.loads(line)
        for line in probe.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _observed_temp_indexes(records: list[dict]) -> list[Path]:
    return [
        Path(record["git_index_file"])
        for record in records
        if record.get("git_index_file")
    ]


# ---------------------------------------------------------------------------
# Checkout snapshots — the real-index / worktree byte-identity detector
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CheckoutState:
    """Everything an aborted gate must leave exactly as it found it."""

    head: str
    index_bytes: bytes
    index_tree: str
    staged_entries: tuple[str, ...]
    cached_diff_clean: bool
    worktree: tuple[tuple[str, bytes], ...]
    status: tuple[str, ...]


def _git_stdout(repo: Path, *args: str) -> str:
    return gs.run_git(repo, list(args), check=True).stdout


def _real_index_path(repo: Path) -> Path:
    git_dir = Path(_git_stdout(repo, "rev-parse", "--absolute-git-dir").strip())
    return git_dir / "index"


def _checkout_state(repo: Path) -> _CheckoutState:
    """Snapshot HEAD, the real index and every change-set/worktree byte.

    The index bytes are read *after* ``git status`` so a stat-cache refresh
    performed by the snapshot itself can never be mistaken for gate churn.
    """
    listing = gs.run_git(
        repo, ["ls-files", "-z", "-co", "--exclude-standard"], check=True
    ).stdout
    worktree = tuple(
        (rel, (repo / rel).read_bytes())
        for rel in sorted(path for path in listing.split("\0") if path)
        if (repo / rel).is_file()
    )
    status = tuple(
        line
        for line in gs.run_git(
            repo, ["status", "--porcelain=v1", "-uall"], check=True
        ).stdout.splitlines()
        if line
    )
    real_index = _real_index_path(repo)
    return _CheckoutState(
        head=_git_stdout(repo, "rev-parse", "HEAD").strip(),
        index_bytes=real_index.read_bytes() if real_index.exists() else b"",
        index_tree=_git_stdout(repo, "write-tree").strip(),
        staged_entries=tuple(
            line
            for line in _git_stdout(repo, "ls-files", "-s").splitlines()
            if line
        ),
        cached_diff_clean=gs.run_git(repo, ["diff", "--cached", "--quiet"]).returncode
        == 0,
        worktree=worktree,
        status=status,
    )


def _isolated_temp_dir(tmp_path: Path, monkeypatch) -> Path:
    """Contain every temp file the gate creates in a directory we own.

    The artefact detector scans this directory instead of the host's shared
    temp root, so a concurrent process creating (or removing) an
    ``*index*``-named file on the same host can never be mistaken for a gate
    leak. ``TMPDIR`` is set for the suite subprocess and
    ``tempfile.tempdir`` is pinned for the gate's own in-process temp use.
    """
    temp_root = tmp_path / "gate-tmp"
    temp_root.mkdir(exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(temp_root))
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return temp_root


def _index_artefacts(repo: Path, temp_root: Path) -> set[str]:
    """Index-shaped paths that must not survive a gate run.

    The real index is excluded (it legitimately exists). Objects are excluded
    by the name filter: ``git add`` writes objects that *must* survive, and a
    leak is an index file, not an object. The temp-directory scan catches a
    temp index created outside the repository, which the suite may never have
    observed.
    """
    git_dir = Path(_git_stdout(repo, "rev-parse", "--absolute-git-dir").strip())
    real_index = (git_dir / "index").resolve()
    artefacts = {
        str(path)
        for path in git_dir.rglob("*")
        if path.is_file()
        and "index" in path.name.lower()
        and path.resolve() != real_index
    }
    if temp_root.is_dir():
        artefacts |= {
            str(path)
            for path in temp_root.rglob("*")
            if path.is_file() and "index" in path.name.lower()
        }
    return artefacts


def _assert_no_temp_index_residue(
    before: set[str], after: set[str], records: list[dict]
) -> None:
    """Both detectors must agree that no temporary index survived the gate."""
    leaks = sorted(after - before)
    assert not leaks, f"temporary index artefact(s) left behind: {leaks}"
    still_present = [path for path in _observed_temp_indexes(records) if path.exists()]
    assert not still_present, (
        f"the gate's temporary index was not removed: {still_present}"
    )


def _assert_abort_left_everything_unchanged(
    repo: Path,
    before: _CheckoutState,
    after: _CheckoutState,
    artefacts_before: set[str],
    artefacts_after: set[str],
    records: list[dict],
) -> None:
    """The abort contract: no commit, no staging, no churn, no leaked index."""
    assert after.head == before.head, (
        "an aborted gate must not create a commit (AC4): "
        f"HEAD moved {before.head} -> {after.head}"
    )
    # AC1/AC2, spelled exactly as the acceptance criterion words it.
    assert gs.run_git(repo, ["diff", "--cached", "--quiet"]).returncode == 0, (
        "an aborted gate must leave nothing staged: git diff --cached is not quiet"
    )
    assert after.cached_diff_clean is True
    assert after.index_bytes == before.index_bytes, (
        "the real index must be byte-identical to its pre-gate state"
    )
    assert after.index_tree == before.index_tree
    assert after.staged_entries == before.staged_entries
    assert after.worktree == before.worktree, (
        "the working tree must be byte-identical to its pre-gate state"
    )
    assert after.status == before.status
    _assert_no_temp_index_residue(artefacts_before, artefacts_after, records)


# ---------------------------------------------------------------------------
# Detector canary — the probe really sees the gate's suite environment
# ---------------------------------------------------------------------------


def test_probe_records_the_gate_suite_environment(tmp_path, monkeypatch):
    """The probe captures the gate's suite environment — the detector works.

    Without this canary an unset ``GIT_INDEX_FILE`` in the tracer test below
    could mean either "no temp index yet" (the pre-F6 baseline) or "the probe
    never ran", and the tracer would xfail for the wrong reason.

    It also pins the invariant that survives F6: while the gate runs the
    suite, the **real** index holds no staged change set — the change set is
    applied to the temp index, never to the real one.
    """
    repo = _build_fixture_repo(tmp_path, gate_fails=False)
    probe = _probe_path(tmp_path, monkeypatch)
    temp_root = _isolated_temp_dir(tmp_path, monkeypatch)
    artefacts_before = _index_artefacts(repo, temp_root)

    report = _drive_gate(repo, tmp_path, monkeypatch)

    assert report is not None and report["success"] is True, report.get("message")
    _assert_real_gate_execution(_gate_test_attempt(report))

    records = _probe_records(probe)
    assert len(records) == 1, f"the probe suite must run exactly once: {records}"
    record = records[0]
    assert Path(record["cwd"]).resolve() == repo.resolve(), record
    assert record["head"], record
    assert record["staged_in_real_index"] == [], (
        "the real index must never hold the change set while the gate runs: "
        f"{record['staged_in_real_index']}"
    )
    # AC3 on the success path: whatever temp index was used is gone again.
    _assert_no_temp_index_residue(
        artefacts_before, _index_artefacts(repo, temp_root), records
    )


# ---------------------------------------------------------------------------
# AC1/AC2/AC4 — a failing gate changes nothing
# ---------------------------------------------------------------------------


def test_gate_failure_leaves_real_index_worktree_and_head_untouched(
    tmp_path, monkeypatch
):
    """A red gate: no commit, nothing staged, the tree byte-identical.

    The change set (``change-set.md``, untracked) is exactly what a leaky
    implementation would leave staged in the real index — the failure mode a
    stage-then-test-with-unstage-on-abort design would produce.
    """
    repo = _build_fixture_repo(tmp_path, gate_fails=True)
    probe = _probe_path(tmp_path, monkeypatch)
    temp_root = _isolated_temp_dir(tmp_path, monkeypatch)
    before = _checkout_state(repo)
    artefacts_before = _index_artefacts(repo, temp_root)

    report = _drive_gate(repo, tmp_path, monkeypatch)

    assert report is not None
    assert report["success"] is False, (
        f"the gate must fail on a red fixture; got {report}"
    )
    attempt = _gate_test_attempt(report)
    _assert_real_gate_execution(attempt)
    assert attempt["success"] is False, attempt
    failures = [str(failure) for failure in attempt["failures"]]
    assert any(FAILING_TEST_NAME in failure for failure in failures), failures
    # One gate attempt, one suite execution: an abort path must not retry the
    # suite behind the caller's back.
    assert len(report["steps"]["tests"]) == 1, report["steps"]["tests"]

    _assert_abort_left_everything_unchanged(
        repo,
        before,
        _checkout_state(repo),
        artefacts_before,
        _index_artefacts(repo, temp_root),
        _probe_records(probe),
    )


def test_aborted_fix_loop_leaves_real_index_worktree_and_head_untouched(
    tmp_path, monkeypatch
):
    """An exhausted fix loop (``max_retry=0``) aborts with the tree unchanged.

    This is the "aborted fix loop" exit path: the gate is red and finish gives
    up instead of handing the failure back to an agent, resetting the item to
    ``open``. It must be as inert as the plain failure path.
    """
    repo = _build_fixture_repo(tmp_path, gate_fails=True)
    probe = _probe_path(tmp_path, monkeypatch)
    temp_root = _isolated_temp_dir(tmp_path, monkeypatch)
    before = _checkout_state(repo)
    artefacts_before = _index_artefacts(repo, temp_root)

    report = _drive_gate(repo, tmp_path, monkeypatch, exit_path="abort")

    assert report is not None
    assert report["success"] is False, report
    assert "Aborting finish phase" in report["message"], report["message"]
    assert "after 0 retries" in report["message"], report["message"]

    _assert_abort_left_everything_unchanged(
        repo,
        before,
        _checkout_state(repo),
        artefacts_before,
        _index_artefacts(repo, temp_root),
        _probe_records(probe),
    )


def test_crash_after_the_suite_leaves_real_index_worktree_and_head_untouched(
    tmp_path, monkeypatch
):
    """An exception escaping the gate still leaves no commit, staging or index.

    The fixture suite runs for real and the gate then raises, so any temp index
    the gate created certainly exists at the moment of the crash: cleanup must
    be exception-safe (a ``finally``), not success-path-only.
    """
    repo = _build_fixture_repo(tmp_path, gate_fails=False)
    probe = _probe_path(tmp_path, monkeypatch)
    temp_root = _isolated_temp_dir(tmp_path, monkeypatch)
    before = _checkout_state(repo)
    artefacts_before = _index_artefacts(repo, temp_root)

    report = _drive_gate(repo, tmp_path, monkeypatch, exit_path="exception")

    assert report is None, "the exception path produces no report"
    records = _probe_records(probe)
    assert len(records) == 1, f"the suite must have run before the crash: {records}"

    _assert_abort_left_everything_unchanged(
        repo,
        before,
        _checkout_state(repo),
        artefacts_before,
        _index_artefacts(repo, temp_root),
        records,
    )


# ---------------------------------------------------------------------------
# AC3 — every exit path removes the temporary index (after F6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exit_path", ["success", "failure", "abort", "exception"])
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-fix baseline (F6 / SA-0MVQHVGFR006W8LG not landed): the finish "
        "gate runs the suite against the loose working tree and never creates "
        "a temporary index, so there is nothing to remove. Temp-index staging "
        "exports GIT_INDEX_FILE and removes the file on every exit path; "
        "remove this marker then (the F1/F2/F4 tracer-bullet convention)."
    ),
)
def test_every_exit_path_uses_and_removes_the_temporary_index(
    exit_path, tmp_path, monkeypatch
):
    """Each exit path runs the suite against a temp index and removes it.

    The path is taken from what the gate's own suite observed, so it holds for
    any naming or location F6 chooses; the file must be gone (and not be the
    real index) once the gate returns.
    """
    repo = _build_fixture_repo(tmp_path, gate_fails=exit_path in {"failure", "abort"})
    probe = _probe_path(tmp_path, monkeypatch)
    temp_root = _isolated_temp_dir(tmp_path, monkeypatch)
    artefacts_before = _index_artefacts(repo, temp_root)

    _drive_gate(repo, tmp_path, monkeypatch, exit_path=exit_path)

    records = _probe_records(probe)
    assert records, f"the gate suite must have run for {exit_path!r}"
    temp_indexes = _observed_temp_indexes(records)
    assert temp_indexes, (
        "the gate must run the suite against a temporary index "
        f"(GIT_INDEX_FILE); observed none for {exit_path!r}"
    )
    real_index = _real_index_path(repo).resolve()
    for temp_index in temp_indexes:
        assert temp_index.resolve() != real_index, (
            "the suite ran against the real index instead of a temporary one: "
            f"{real_index}"
        )
        assert not temp_index.exists(), (
            f"the temporary index survived the {exit_path!r} exit path: "
            f"{temp_index}"
        )
    assert not sorted(_index_artefacts(repo, temp_root) - artefacts_before)

