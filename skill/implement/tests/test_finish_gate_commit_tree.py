"""F1 reproduction: the finish gate validates the loose tree, not the commit's.

Cross-tree false-green reproduction at the finish-gate entry point (F1,
SA-0MVQHV72U003UJUB; parent SA-0MVL2NH1G0094FA2).

What is reproduced
------------------
``phase_finish`` runs its test step **before** ``git_commit`` stages the change
set (``git add -A``). A suite test that keys off the *tracked* file set
(``git ls-files`` semantics) therefore cannot see files the commit is about to
add. This module builds a hermetic fixture repository whose suite contains
exactly such a guard, adds a content file the guard must reject (untracked in
the loose working tree), and drives the **real** ``phase_finish`` test step —
only the external boundaries (build, git commit/push, worktree removal, the
``wl``/``StatusLifecycle`` store) are stubbed, in the style of
``test_finish_no_diff_status.py``.

Captured baseline (pre-fix — F6 / SA-0MVQHVGFR006W8LG not landed)
-----------------------------------------------------------------
The gate reports::

    {"scope": "full", "success": true}

while the tree the commit produces is red: staging the change set exactly as
``git_commit`` does (``git add -A``) makes the guard test fail by name. That
green verdict on a red commit tree is the bug evidence
(``test_baseline_capture_false_green_at_finish_gate_entry_point``).

After F6 (temp-index staging)
-----------------------------
The same fixture must fail the gate red with the guard test named in the gate
``failures``. ``test_gate_fails_red_naming_the_guard_test_after_fix`` pins that
expectation as a strict ``xfail`` — the repository's tracer-bullet convention
(see ``skill/shared/tests/test_git_sandbox.py`` and
``tests/test_repository_override_isolation.py``). F6 removes the marker when
the temp-index gate lands, at which point the baseline capture above must be
inverted to assert the red verdict instead.

Hermetic by construction: a temp git repository (``git_sandbox`` factories, no
environment leakage) plus stubbed boundaries — no real worklog, no real main
checkout, no network.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

import test_cache
from shared import git_sandbox as gs

from skill.implement.scripts import implement

#: The guard test the fixture suite carries. Named in the gate ``failures``
#: once the temp-index gate (F6) tests the would-be-commit tree.
GUARD_TEST_NAME = "test_no_content_tracked_outside_documented_allowlist"
GUARD_TEST_REL = f"tests/{GUARD_TEST_NAME}.py"

#: Documented allowlist of ``content/`` paths the fixture may track. The
#: change set below deliberately adds a path that is **not** in it.
CONTENT_ALLOWLIST: tuple[str, ...] = (
    ".gitignore",
    "README.md",
    GUARD_TEST_REL,
)

_GUARD_TEST_SOURCE = f'''"""Tracked-file guard: ``content/`` must be documented before it is tracked."""

import subprocess

ALLOWLIST = {set(CONTENT_ALLOWLIST)!r}


def {GUARD_TEST_NAME}():
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    offenders = sorted(
        path
        for path in tracked
        if path.startswith("content/") and path not in ALLOWLIST
    )
    assert offenders == [], f"content tracked outside the allowlist: {{offenders}}"
'''


# ---------------------------------------------------------------------------
# Fixture repository
# ---------------------------------------------------------------------------


def _build_fixture_repo(tmp_path: Path) -> Path:
    """A hermetic repo whose suite carries a ``git ls-files`` tracked-file guard.

    One seed commit contains the suite (and the documented allowlist); the
    *change set* then adds ``content/episode-1.md`` — untracked in the loose
    working tree, exactly the shape that made the gate green while the commit
    went red (OSL-0MVK2F1Z300996SA).
    """
    repo = gs.init_repo(tmp_path / "fixture-repo", default_branch="dev")
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# Fixture suite\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / GUARD_TEST_REL).write_text(_GUARD_TEST_SOURCE, encoding="utf-8")
    gs.commit_all(repo, "seed fixture suite")

    (repo / "content").mkdir()
    (repo / "content" / "episode-1.md").write_text(
        "# Episode 1\n", encoding="utf-8"
    )
    return repo


def _run_fixture_suite(repo: Path) -> subprocess.CompletedProcess:
    """Run the fixture repository's own suite (its single test) in *repo*."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-r", "a", "--disable-warnings", "tests"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# ---------------------------------------------------------------------------
# phase_finish harness — real test step, stubbed external boundaries
# ---------------------------------------------------------------------------


def _build_result() -> dict:
    return {"success": True, "stdout": "", "stderr": "", "exit_code": 0, "skipped": True}


def _cleanup_result() -> dict:
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    """Stand-in for ``run_cmd``: every git sub-command "succeeds".

    ``phase_finish``'s fast-forward main-checkout block and the main-checkout
    status snapshot run against the stubbed main checkout; none of that must
    touch a real repository here.
    """
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


@contextlib.contextmanager
def _stubbed_finish_boundaries(repo: Path, tmp_path: Path):
    """Patch every ``phase_finish`` boundary *except* the test step.

    The suite run is deliberately **not** patched: the reproduction must
    exercise the real finish-gate test step, the authoritative
    ``guarded_run_all`` delegation and the real cache, against the fixture
    repository.
    """
    main_checkout = tmp_path / "main-checkout"
    (main_checkout / ".worklog").mkdir(parents=True)
    state = implement.ImplementState(
        work_item_id="SA-TEST",
        worktree_path=str(repo),
        repo_root=str(main_checkout),
        parent_branch="dev",
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=str(repo))
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(
            patch.object(implement, "run_build", return_value=_build_result())
        )
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        stack.enter_context(patch.object(implement, "run_cmd", side_effect=_fake_run_cmd))
        stack.enter_context(
            patch.object(
                implement, "cleanup_worktree_processes", return_value=_cleanup_result()
            )
        )
        stack.enter_context(patch.object(implement, "remove_state"))
        stack.enter_context(patch.object(implement, "_remove_worktree", return_value=True))
        stack.enter_context(
            patch.object(
                implement,
                "_sync_parent_branch",
                return_value={"method": "skipped", "success": True},
            )
        )
        stack.enter_context(patch.object(implement, "wl_add_comment", return_value=True))
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", MagicMock())
        )
        stack.enter_context(
            patch.object(
                implement.StatusLifecycle,
                "show",
                MagicMock(
                    return_value={
                        "workItem": {"status": "completed", "stage": "in_review"}
                    }
                ),
            )
        )
        yield


def _finish_gate_report(repo: Path, tmp_path: Path, monkeypatch) -> dict:
    """Drive ``phase_finish``'s test step at the finish-gate entry point.

    An ambient ``IMPLEMENT_TEST_COMMAND`` override would replace the fixture's
    suite with an unrelated command, so it is removed to keep the reproduction
    about the gate's tree choice, not about an environment override.
    """
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)
    with _stubbed_finish_boundaries(repo, tmp_path):
        return implement.phase_finish(
            "SA-TEST", json_output=True, no_refactor=True
        )


def _gate_test_attempt(report: dict) -> dict:
    """The first test-step attempt recorded by ``phase_finish``."""
    attempts = report["steps"]["tests"]
    assert attempts, f"the finish gate recorded no test attempt: {report}"
    return attempts[0]


def _assert_real_gate_execution(attempt: dict) -> None:
    """The gate really ran the fixture suite — not skipped, overridden or cached."""
    assert attempt["skipped"] is False, attempt
    assert attempt["tooling"] == "suite", attempt
    assert attempt["scope"] == "full", attempt


# ---------------------------------------------------------------------------
# AC1 — baseline capture of the pre-fix cross-tree false green
# ---------------------------------------------------------------------------


def test_baseline_capture_false_green_at_finish_gate_entry_point(tmp_path, monkeypatch):
    """Captured bug evidence: the gate reports ``{"scope": "full", "success": true}``
    for a change set whose committed tree is red.

    The fixture change set adds ``content/episode-1.md``, which the suite's
    tracked-file guard rejects — but only once the commit stages it. Because
    ``phase_finish`` tests before it stages, today's gate sees the loose
    working tree (``git ls-files`` does not list the new file) and reports a
    green full-scope run. Staging the change set exactly as ``git_commit``
    does turns the same suite red, proving the verdict was produced against a
    different tree than the commit (parent AC1).

    F6 (SA-0MVQHVGFR006W8LG) inverts this expectation: when the temp-index
    gate lands, replace this assertion with the red-verdict assertion in
    ``test_gate_fails_red_naming_the_guard_test_after_fix`` and drop that
    test's ``xfail`` marker.
    """
    repo = _build_fixture_repo(tmp_path)

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is True, report["message"]
    attempt = _gate_test_attempt(report)
    _assert_real_gate_execution(attempt)
    # The captured pre-fix verdict — the false green, verbatim.
    assert {
        "scope": attempt["scope"],
        "success": attempt["success"],
    } == {"scope": "full", "success": True}

    # The tree the commit produces is red the moment it is staged: this is
    # exactly what `git_commit` runs after the gate.
    gs.run_git(repo, ["add", "-A"], check=True)
    committed_tree = _run_fixture_suite(repo)
    assert committed_tree.returncode == 1, committed_tree.stdout
    assert GUARD_TEST_NAME in committed_tree.stdout


# ---------------------------------------------------------------------------
# AC2 — after F6 the identical fixture fails the gate red, naming the test
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-fix baseline (F6 / SA-0MVQHVGFR006W8LG not landed): the finish "
        "gate tests the loose working tree, so the added content file is "
        "invisible to the git ls-files guard. Temp-index staging makes the "
        "gate fail red and names the guard test; remove this marker then."
    ),
)
def test_gate_fails_red_naming_the_guard_test_after_fix(tmp_path, monkeypatch):
    """With temp-index staging the identical fixture fails the finish gate red.

    The failing test is named in the gate ``failures`` (not merely a non-zero
    exit), so an agent reading the report knows which guard rejected the
    would-be-commit tree (parent AC1).
    """
    repo = _build_fixture_repo(tmp_path)

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is False, (
        "the gate must reject a change set its own suite rejects once "
        f"committed; got {report}"
    )
    attempt = _gate_test_attempt(report)
    assert attempt["success"] is False, attempt
    assert attempt["scope"] == "full", attempt
    failures = [str(failure) for failure in attempt["failures"]]
    assert any(GUARD_TEST_NAME in failure for failure in failures), failures


# ---------------------------------------------------------------------------
# F2 — the disk-reading parity false pass (SA-0MVQHV8JE007MAK3)
#
# The tracked-file guard above is loose-tree-blind by construction: the suite
# asks git what is tracked, so a not-yet-staged file is invisible to it. The
# parity case from the same incident (OSL-0MVK2F1Z300996SA,
# ``test_registry_documents_match_on_disk_documents``) is different — it reads
# **on-disk** files only, comparing a generated registry against the on-disk
# document set. Staging cannot hide a file from it, so a green verdict for a
# red parity tree can only mean the verdict came from a suite run that visited
# a *different tree*.
#
# What this fixture pins
# ---------------------
# The registry is generated metadata and — as generated artefacts usually are
# — gitignored. ``test_cache.compute_git_state`` fingerprints HEAD plus the
# content of every path ``git status`` reports, so ignored metadata is
# invisible to the cache key: a green run recorded against a tree whose
# registry was rebuilt is served for a tree whose registry lags, even though
# the two on-disk trees produce opposite parity verdicts. The fixture therefore
# records a *real* green run for the rebuilt-metadata tree, restores the
# lagging registry, and drives the real finish-gate test step:
#
# * today the gate serves that foreign green verdict — the false pass, captured
#   as the reproduction;
# * the very same tree is red the moment its suite genuinely runs;
# * after F7 (SA-0MVQHVHXI007PQAB) binds verdict acceptance to the
#   would-be-commit tree fingerprint, the entry misses and the suite re-runs
#   red, naming the parity test (strict ``xfail`` until then, the same
#   tracer-bullet convention as the F1 tests above).
# ---------------------------------------------------------------------------

PARITY_TEST_NAME = "test_registry_documents_match_on_disk_documents"
PARITY_TEST_REL = f"tests/{PARITY_TEST_NAME}.py"
#: Generated metadata, gitignored — the register side of the parity check.
REGISTRY_REL = "_build/registry.json"
REBUILD_SCRIPT_REL = "scripts/rebuild_registry.py"

_PARITY_TEST_SOURCE = f'''"""Parity: the registry must list every on-disk wiki document.

Reads **on-disk** files only (the generated registry and the document set) —
never ``git ls-files`` — so staging the change set cannot hide anything from
it.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / {REGISTRY_REL!r}
WIKI = ROOT / "wiki"


def {PARITY_TEST_NAME}():
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    documents = sorted(registry["documents"])
    on_disk = sorted(path.name for path in WIKI.glob("*.md"))
    assert documents == on_disk, (
        f"registry documents {{documents}} do not match on-disk documents "
        f"{{on_disk}}"
    )
'''

_REBUILD_SCRIPT_SOURCE = '''"""Regenerate the gitignored registry from the on-disk document set."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIKI = ROOT / "wiki"
REGISTRY = ROOT / "_build" / "registry.json"


def rebuild() -> list[str]:
    documents = sorted(path.name for path in WIKI.glob("*.md"))
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(
        json.dumps({"documents": documents}, indent=2), encoding="utf-8"
    )
    return documents


if __name__ == "__main__":
    rebuild()
'''


def _rebuild_registry(repo: Path) -> None:
    """Regenerate the gitignored registry from the on-disk document set."""
    completed = subprocess.run(
        [sys.executable, REBUILD_SCRIPT_REL],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _build_parity_fixture_repo(tmp_path: Path) -> Path:
    """A hermetic repo whose suite compares a generated registry to disk files.

    The seed commit carries one document (``wiki/welcome.md``) and a registry
    generated from it — the parity suite is green there. The *change set* then
    adds ``wiki/synthesis.md`` without rebuilding the registry, so a genuine
    run on the gate's tree is red: the lagging-metadata state the historical
    gate reported green (OSL-0MVK2F1Z300996SA).
    """
    repo = gs.init_repo(tmp_path / "parity-repo", default_branch="dev")
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.worklog/\n_build/\n", encoding="utf-8"
    )
    (repo / "wiki").mkdir()
    (repo / "wiki" / "welcome.md").write_text("# Welcome\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / PARITY_TEST_REL).write_text(_PARITY_TEST_SOURCE, encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / REBUILD_SCRIPT_REL).write_text(
        _REBUILD_SCRIPT_SOURCE, encoding="utf-8"
    )
    _rebuild_registry(repo)
    gs.commit_all(repo, "seed wiki documents and generated registry")

    (repo / "wiki" / "synthesis.md").write_text("# Synthesis\n", encoding="utf-8")
    return repo


def _gate_suite_command(repo: Path) -> str:
    """The single full-suite command the gate resolves for the fixture repo."""
    commands = implement._resolve_full_suite_commands(str(repo))
    assert commands, "the fixture repo must resolve a full-suite command"
    assert len(commands) == 1, commands
    return commands[0]


def _would_be_commit_tree_sha(repo: Path) -> str:
    """The tree SHA the commit will produce (temp-index staging, F6 semantics).

    F7 (SA-0MVQHVHXI007PQAB) binds verdict acceptance to this fingerprint, so
    an entry recorded under a loose working-tree state can never satisfy the
    gate.
    """
    env = {"GIT_INDEX_FILE": str(repo.parent / "would-be-commit.index")}
    gs.run_git(repo, ["read-tree", "HEAD"], check=True, env=env)
    gs.run_git(repo, ["add", "-A"], check=True, env=env)
    return gs.run_git(repo, ["write-tree"], check=True, env=env).stdout.strip()


def _record_foreign_green_verdict(repo: Path) -> tuple[dict, str]:
    """Record a **real** green parity run against a different on-disk tree.

    The registry is generated metadata at a gitignored path, so rebuilding it
    leaves the loose git-state fingerprint unchanged: the run below is stored
    under the same key the gate computes for the lagging-metadata tree. That
    collision — a fingerprint that identifies a tree *state* but neither a
    checkout nor its generated metadata — is the mechanism behind the parity
    false pass: the cache entry is a genuine green verdict, just not for the
    tree the gate is about to commit.

    Returns the stored entry and the suite command it was recorded for.
    """
    command = _gate_suite_command(repo)
    stale_fingerprint = test_cache.compute_git_state(str(repo))
    stale_registry = (repo / REGISTRY_REL).read_text(encoding="utf-8")

    _rebuild_registry(repo)
    assert test_cache.compute_git_state(str(repo)) == stale_fingerprint, (
        "generated (gitignored) metadata must be invisible to the loose "
        "fingerprint — that blindness is what this reproduction pins"
    )
    entry = test_cache.run_cached(command, cwd=str(repo))
    assert entry["cached"] is False, entry
    assert entry["exit_code"] == 0, entry["stdout"]

    # Restore the lagging registry: from here on the gate's own tree is red,
    # while remaining fingerprint-identical to the tree that just ran green.
    (repo / REGISTRY_REL).write_text(stale_registry, encoding="utf-8")
    assert test_cache.compute_git_state(str(repo)) == stale_fingerprint
    return entry, command


# ---------------------------------------------------------------------------
# F2 AC1(a) — the verdict reflects the gate's tree when the suite really runs
# ---------------------------------------------------------------------------


def test_parity_verdict_reflects_the_gate_tree_when_the_suite_genuinely_runs(
    tmp_path, monkeypatch
):
    """No cache entry: the gate runs the parity suite against its own tree.

    The change set's new document is not in the generated registry, so the
    gate must report red and name the parity test. This proves the fixture is
    a real parity detector — the reproduction below can therefore attribute a
    green verdict to a suite run that visited a different tree, never to
    untracked-file invisibility (the parity suite never asks git).
    """
    repo = _build_parity_fixture_repo(tmp_path)
    assert test_cache.query_cached(_gate_suite_command(repo), cwd=str(repo)) is None

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is False, (
        "a lagging registry must fail the gate when the suite genuinely runs "
        f"against the gate's tree; got {report}"
    )
    attempt = _gate_test_attempt(report)
    _assert_real_gate_execution(attempt)
    assert attempt["success"] is False, attempt
    failures = [str(failure) for failure in attempt["failures"]]
    assert any(PARITY_TEST_NAME in failure for failure in failures), failures


# ---------------------------------------------------------------------------
# F2 AC1(b) — baseline capture of the parity false pass (foreign cache entry)
# ---------------------------------------------------------------------------


def test_baseline_capture_parity_false_pass_from_a_foreign_cache_entry(
    tmp_path, monkeypatch
):
    """Captured bug evidence: a green verdict served for a red parity tree.

    A green entry recorded by a genuine run against the rebuilt-metadata tree
    is served for the lagging-metadata tree because generated metadata is
    invisible to the loose fingerprint. The gate reports green while the same
    tree's suite is red on disk — the parity false pass from OSL-0MVK2F1Z300996SA
    reproduced at the finish-gate entry point without relying on untracked
    files. F7 (SA-0MVQHVHXI007PQAB) inverts this expectation: see
    ``test_parity_foreign_cache_entry_misses_once_f7_lands``.
    """
    repo = _build_parity_fixture_repo(tmp_path)
    primed, command = _record_foreign_green_verdict(repo)
    served_before = test_cache.query_cached(command, cwd=str(repo))
    assert served_before is not None and served_before["stdout"] == primed["stdout"]

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is True, report.get("message")
    attempt = _gate_test_attempt(report)
    _assert_real_gate_execution(attempt)
    # The captured finding, verbatim: full scope, green — for a red tree.
    assert {
        "scope": attempt["scope"],
        "success": attempt["success"],
    } == {"scope": "full", "success": True}

    # The verdict was SERVED, not produced: no new entry was recorded.
    served_after = test_cache.query_cached(command, cwd=str(repo))
    assert served_after is not None
    assert served_after["completed_at"] == served_before["completed_at"]

    # ... while the gate's own tree is red the moment its suite actually runs.
    genuine = _run_fixture_suite(repo)
    assert genuine.returncode == 1, genuine.stdout
    assert PARITY_TEST_NAME in genuine.stdout


# ---------------------------------------------------------------------------
# F2 AC2 — after F7 a foreign fingerprint is never served as green
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-fix baseline (F7 / SA-0MVQHVHXI007PQAB not landed): the gate "
        "accepts a cache entry keyed by the loose working-tree fingerprint, so "
        "a green verdict recorded against a different on-disk tree is served "
        "for the gate's tree. Tree-fingerprint binding makes the entry miss "
        "and the suite re-run; remove this marker then and invert the baseline "
        "capture above."
    ),
)
def test_parity_foreign_cache_entry_misses_once_f7_lands(tmp_path, monkeypatch):
    """The foreign green entry must miss; the suite re-runs and reports red.

    Once F7 lands the gate accepts a cached verdict only for the
    would-be-commit tree's fingerprint. The entry below was recorded under a
    loose working-tree state — a different fingerprint entirely — so it must
    be rejected and the parity suite must run against the gate's own tree and
    fail, naming the parity test.
    """
    repo = _build_parity_fixture_repo(tmp_path)
    primed, command = _record_foreign_green_verdict(repo)
    served_before = test_cache.query_cached(command, cwd=str(repo))
    assert served_before is not None

    # Evidence: the entry's fingerprint is not the tree the commit produces.
    assert primed["git_state"] != _would_be_commit_tree_sha(repo)

    report = _finish_gate_report(repo, tmp_path, monkeypatch)

    assert report["success"] is False, (
        "a cached verdict whose fingerprint is not the would-be-commit tree "
        f"must never be served as green; got {report}"
    )
    attempt = _gate_test_attempt(report)
    assert attempt["success"] is False, attempt
    failures = [str(failure) for failure in attempt["failures"]]
    assert any(PARITY_TEST_NAME in failure for failure in failures), failures

    # The suite really re-ran: the stale entry was replaced by the red result.
    served_after = test_cache.query_cached(command, cwd=str(repo))
    assert served_after is not None
    assert served_after["completed_at"] != served_before["completed_at"]
