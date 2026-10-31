
# <!-- REFACTOR-SA-0MVWWENFV005VXW3
# smell: ruff_specific
# severity: medium
# description: Unpacked variable `spawn_log` is never used
# -->

# <!-- REFACTOR-SA-0MVWWENFV005VXW3
# smell: simplification
# severity: medium
# description: Use a single `with` statement with multiple contexts instead of nested `with` statements
# -->
"""F4: verdict provenance and cache contract at the finish gate.

Contract tests for the finish gate's *verdict provenance* (F4,
SA-0MVQHVBCC0083VK5; parent SA-0MVL2NH1G0094FA2).

Why this module exists
----------------------
The gate's green verdict used to be unattributable: the report said
``{"scope": "full", "success": true}`` and nothing recorded *which tree* that
verdict came from or *whether it was served from cache*. F1
(``skill/implement/tests/test_finish_gate_commit_tree.py``) reproduced a
cross-tree false green and F2 reproduced a foreign cached verdict; this module
pins the contract that makes such verdicts detectable, so F7/F8
(tree-fingerprint cache binding) and F9 (verifiable verdicts) have executable
acceptance criteria rather than prose.

The contract under test
-----------------------
**Gate report (per test attempt, F9).** Every entry of
``report["steps"]["tests"]`` records:

* ``tree`` — the worktree path the suite ran against (absolute);
* ``git_fingerprint`` — the would-be-commit **tree** SHA (``git write-tree``
  of the staged tree), not the loose working-tree state;
* ``cached`` — ``True`` only when the verdict was served from the cache;
* ``cache_entry`` — when ``cached``, a mapping carrying at least ``command``,
  ``completed_at`` and ``scope`` (the cache entry's metadata).

**A suite execution count.** A clean change set produces exactly **one** suite
execution per gate verdict; an identical tree inside the TTL produces **none**
(the cached verdict is reused). Both are counted from a suite that appends a
line to an out-of-repo log every time its test body executes, so a duplicate
run is impossible to hide.

Tracer-bullet convention
------------------------
The report-shape expectations were ``xfail(strict=True)`` while F4 landed the
contract ahead of the behaviour (the repository's established convention for
test-first work on this epic). F9 (SA-0MVQHVJEX0071R7G) landed the provenance
keys, so the markers are gone and these are ordinary regression tests.

The two execution-count contracts (``..._exactly_once`` and
``..._reuses_the_cached_verdict``) pass **today** and must keep passing: they
are the regression guards against a fix that runs the suite twice (parent
constraint "one suite execution per gate verdict on a clean change set").

Hermetic by construction: a temp git repository plus the stubbed finish
boundaries from the F1 module (build, commit, push, worktree removal, the
``wl``/``StatusLifecycle`` store) — the gate's **test step is real**: the
authoritative ``guarded_run_all`` delegation and the real per-repo cache run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

# The F1 harness: fixture-repo factories plus the stubbed finish boundaries.
# The gate's test step is deliberately NOT stubbed, so every test here drives
# the real suite execution path.
from skill.implement.scripts import implement
from skill.implement.tests.test_finish_gate_commit_tree import (
    _assert_real_gate_execution,
    _finish_gate_report,
    _gate_suite_command,
    _gate_test_attempt,
    _stubbed_finish_boundaries,
    _would_be_commit_tree_sha,
)

#: The single test the clean fixture suite carries. Its *body* appends a line
#: to an out-of-repo spawn log, so each suite execution is countable.
CLEAN_SUITE_TEST_NAME = "test_clean_suite_is_green"
CLEAN_SUITE_TEST_REL = f"tests/{CLEAN_SUITE_TEST_NAME}.py"

_CLEAN_SUITE_SOURCE = '''"""Clean fixture suite: records every execution of its test body."""

from pathlib import Path

SPAWN_LOG = Path({spawn_log!r})


def {test_name}():
    with SPAWN_LOG.open("a", encoding="utf-8") as handle:
        handle.write("spawn\\n")
    assert True
'''


# ---------------------------------------------------------------------------
# Fixture repository — a clean, green change set
# ---------------------------------------------------------------------------


def _build_clean_fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A hermetic repo whose suite is green, plus its out-of-repo spawn log.

    The seed commit carries the suite and a README; the *change set* then
    edits the README — a clean change set that the gate must pass with a
    single suite execution (parent constraint "one suite execution per gate
    verdict on a clean change set").

    The spawn log lives **outside** the repository so counting executions can
    never perturb the git state the cache fingerprints (an in-repo counter
    file would change the state between runs and turn a legitimate cache hit
    into a miss).
    """
    repo = gs.init_repo(tmp_path / "clean-repo", default_branch="dev")
    spawn_log = tmp_path / "suite-spawns.log"

    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Fixture suite\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / CLEAN_SUITE_TEST_REL).write_text(
        _CLEAN_SUITE_SOURCE.format(
            spawn_log=str(spawn_log), test_name=CLEAN_SUITE_TEST_NAME
        ),
        encoding="utf-8",
    )
    gs.commit_all(repo, "seed clean fixture suite")

    (repo / "README.md").write_text("# Fixture suite\n\nClean change set.\n",
                                    encoding="utf-8")
    return repo, spawn_log


def _suite_spawns(spawn_log: Path) -> int:
    """How many times the fixture suite's test body has executed."""
    if not spawn_log.exists():
        return 0
    return len(spawn_log.read_text(encoding="utf-8").splitlines())


# ---------------------------------------------------------------------------
# AC1 — the report records the tree, the fingerprint and the cached flag
# ---------------------------------------------------------------------------


def test_fresh_gate_attempt_records_tree_fingerprint_and_cached_flag(
    tmp_path, monkeypatch
):
    """A freshly executed verdict names its tree, its fingerprint, and says
    it was not cached.

    The fingerprint must be the **tree the commit produces**
    (``git write-tree`` of the staged tree), not the loose working-tree state:
    a verdict attributed to a loose-state fingerprint is exactly what made a
    foreign cached green acceptable in the first place (parent AC2).
    """
    repo, spawn_log = _build_clean_fixture_repo(tmp_path)

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is True, report.get("message")
    attempt = _gate_test_attempt(report)
    _assert_real_gate_execution(attempt)
    assert _suite_spawns(spawn_log) == 1, "the gate must really have executed"

    # (a) the tree the suite ran against
    assert Path(attempt["tree"]).resolve() == repo.resolve(), attempt
    # (b) its fingerprint: the would-be-commit tree, not the loose state
    assert attempt["git_fingerprint"] == _would_be_commit_tree_sha(repo), attempt
    # (c) provenance of the verdict itself
    assert attempt["cached"] is False, attempt
    assert not attempt.get("cache_entry"), attempt


# ---------------------------------------------------------------------------
# AC4 — a cache-served verdict carries the entry metadata that identifies it
# ---------------------------------------------------------------------------


def test_cache_hit_attempt_records_the_entry_metadata(tmp_path, monkeypatch):
    """A cached verdict states which entry was served, not just that it passed.

    An operator reading ``cached: true`` must be able to see *what* was
    reused — the command, when it completed, and the scope it represented — so
    a partially-scoped run can never masquerade as full-suite evidence.
    """
    repo, spawn_log = _build_clean_fixture_repo(tmp_path)

    first = _finish_gate_report(repo, tmp_path, monkeypatch)
    assert first["success"] is True, first.get("message")
    assert _suite_spawns(spawn_log) == 1

    second = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert second["success"] is True, second.get("message")
    attempt = _gate_test_attempt(second)
    assert attempt["cached"] is True, attempt
    # Still a single execution: the second verdict was served, not produced.
    assert _suite_spawns(spawn_log) == 1

    entry = attempt["cache_entry"]
    assert entry, attempt
    assert entry["command"] == _gate_suite_command(repo), entry
    assert entry["completed_at"] > 0, entry
    assert entry["scope"] in {"full", "changed"}, entry


# ---------------------------------------------------------------------------
# AC3 — a clean change set passes with a single suite execution
# ---------------------------------------------------------------------------


def test_clean_change_set_runs_the_suite_exactly_once(tmp_path, monkeypatch):
    """No duplicate execution: the gate's green verdict costs one suite run.

    Regression guard for the whole epic: whatever provenance/verdict plumbing
    lands must not re-execute the suite (a second run for a fingerprint, a
    retry after a provenance mismatch, or a lookup followed by a forced
    refresh would all show up here as two spawns).
    """
    repo, spawn_log = _build_clean_fixture_repo(tmp_path)
    # No cached entry exists before the gate runs.
    assert _suite_spawns(spawn_log) == 0

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is True, report.get("message")
    assert len(report["steps"]["tests"]) == 1, report["steps"]["tests"]
    attempt = _gate_test_attempt(report)
    _assert_real_gate_execution(attempt)
    assert _suite_spawns(spawn_log) == 1, (
        "a clean change set must produce exactly one suite execution"
    )


# ---------------------------------------------------------------------------
# AC4 — an identical tree inside the TTL reuses the cache (no execution)
# ---------------------------------------------------------------------------


def test_identical_tree_reuses_the_cached_verdict_without_spawning(
    tmp_path, monkeypatch
):
    """Re-running the gate on the identical tree serves the cached verdict.

    The second gate run must not spawn the suite at all: the entry recorded by
    the first run is a valid verdict for the same tree, so re-running would be
    the duplicate execution AC3 forbids. This is also what makes the
    fingerprint binding (F7/F8) safe to land — a *matching* fingerprint stays
    a hit.
    """
    repo, spawn_log = _build_clean_fixture_repo(tmp_path)

    first = _finish_gate_report(repo, tmp_path, monkeypatch)
    assert first["success"] is True, first.get("message")
    assert _suite_spawns(spawn_log) == 1

    second = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert second["success"] is True, second.get("message")
    assert _suite_spawns(spawn_log) == 1, (
        "an identical tree inside the TTL must be served from the cache, not "
        "re-executed"
    )


# ---------------------------------------------------------------------------
# AC2 — the completion comment carries the verdict provenance
# ---------------------------------------------------------------------------


def _finish_gate_report_capturing_comments(
    repo: Path, tmp_path: Path, monkeypatch
) -> tuple[dict, object]:
    """Drive the finish gate and capture every comment it records.

    The shared harness stubs ``wl_add_comment`` to a no-op, so the recorder is
    applied *inside* it (a later patch wins) — the comment under test is the
    real one ``phase_finish`` writes, not a re-derived string.
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    with _stubbed_finish_boundaries(repo, tmp_path):
        with patch.object(implement, "wl_add_comment") as comment:
            report = implement.phase_finish(
                "SA-TEST", json_output=True, no_refactor=True
            )
    return report, comment


def _completion_comment_text(mock_comment) -> str:
    """The text of every comment ``phase_finish`` recorded on the work item."""
    texts = [
        str(call.args[1])
        for call in mock_comment.call_args_list
        if len(call.args) > 1
    ]
    matching = [text for text in texts if "Implementation complete" in text]
    assert matching, f"no completion comment recorded: {texts}"
    return matching[-1]


def test_completion_comment_records_the_fresh_verdict_provenance(
    tmp_path, monkeypatch
):
    """The completion comment names the tree, its fingerprint and the cache flag.

    An agent (or an auditor) reading only the work item must be able to tell
    *which* tree the green verdict belongs to and whether the suite actually
    ran for it — the provenance is not allowed to live only in the JSON report
    ephemeral output.
    """
    repo, spawn_log = _build_clean_fixture_repo(tmp_path)

    report, comment = _finish_gate_report_capturing_comments(
        repo, tmp_path, monkeypatch
    )

    assert report["success"] is True, report.get("message")
    attempt = _gate_test_attempt(report)
    text = _completion_comment_text(comment)

    assert f"- Tree: {Path(attempt['tree']).resolve()}" in text, text
    assert f"- Fingerprint: {attempt['git_fingerprint']}" in text, text
    assert "- Cached: false" in text, text
    assert "Cache entry" not in text, text


def test_completion_comment_records_the_cached_verdict_provenance(
    tmp_path, monkeypatch
):
    """A cache-served completion states the entry it reused.

    The second gate run on the identical tree is served from the cache; the
    comment must say so *and* identify the entry (command, completion time,
    scope) so a partially-scoped run can never read as full-suite evidence.
    """
    repo, spawn_log = _build_clean_fixture_repo(tmp_path)

    first = _finish_gate_report(repo, tmp_path, monkeypatch)
    assert first["success"] is True, first.get("message")

    second, comment = _finish_gate_report_capturing_comments(
        repo, tmp_path, monkeypatch
    )

    assert second["success"] is True, second.get("message")
    attempt = _gate_test_attempt(second)
    assert attempt["cached"] is True, attempt
    entry = attempt["cache_entry"]
    text = _completion_comment_text(comment)

    assert "- Cached: true" in text, text
    assert f"- Cache entry: command {entry['command']}" in text, text
    assert f"scope {entry['scope']}" in text, text
    assert str(entry["completed_at"]) in text, text
