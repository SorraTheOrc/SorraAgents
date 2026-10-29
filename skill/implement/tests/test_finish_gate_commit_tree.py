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


def _run_guard_suite(repo: Path) -> subprocess.CompletedProcess:
    """Run the fixture's own suite (one guard test) in *repo*."""
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
    committed_tree = _run_guard_suite(repo)
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
