"""Finish main-checkout guard regression tests (WL-0MVPADZ1Z000FOSG AC3).

``implement.py finish`` runs from a linked worktree but operates on the main
checkout (fast-forward, push, worktree removal). A foreign actor mutating the
main checkout mid-finish — the 2026-10-26 ``packages/shared`` wipe — must
never be silently swallowed: finish aborts loudly, names every offending path
and the concurrent-actor evidence, leaves the work item ``open``, and records
the guard's work under ``report["steps"]["main_checkout_guard"]``.

Covered here:
- pure ``_main_checkout_guard_violations`` classification (deletions,
  modifications, ``.worklog/`` + expected-dirty exclusions, own fast-forward
  churn allow-list),
- a real-repo snapshot → delete → verify cycle (the defect class reproduced
  against a real git checkout),
- ``phase_finish`` behaviour: foreign deletion aborts with the path named and
  the item reset to ``open``; own fast-forward churn does not abort; a clean
  finish reports the guard step with snapshot/verify times and a pass verdict.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement
from skill.shared import git_sandbox

# ---------------------------------------------------------------------------
# Pure classification tests
# ---------------------------------------------------------------------------


def test_violations_report_foreign_tracked_deletion():
    """A tracked file deleted between snapshot and verify is offending."""
    baseline = {"taken_at": "T0", "status": {}}
    current = {
        "taken_at": "T1",
        "status": {"packages/shared/src/database.ts": " D"},
    }
    violations = implement._main_checkout_guard_violations(baseline, current)
    assert violations == ["packages/shared/src/database.ts [ D]"]


def test_violations_report_foreign_modification():
    """A tracked file modified between snapshot and verify is offending."""
    baseline = {"taken_at": "T0", "status": {"src/a.ts": " M"}}
    current = {"taken_at": "T1", "status": {"src/a.ts": "MM"}}
    violations = implement._main_checkout_guard_violations(baseline, current)
    assert violations == ["src/a.ts [MM]"]


def test_violations_report_new_untracked_path():
    """A non-ignored file appearing mid-run is foreign activity, reported."""
    baseline = {"taken_at": "T0", "status": {}}
    current = {"taken_at": "T1", "status": {"scratch/new-file.txt": "??"}}
    violations = implement._main_checkout_guard_violations(baseline, current)
    assert violations == ["scratch/new-file.txt [??]"]


def test_violations_ignore_worklog_and_expected_dirty():
    """``.worklog/`` bookkeeping and project-declared dirty paths never abort."""
    baseline = {"taken_at": "T0", "status": {}}
    current = {
        "taken_at": "T1",
        "status": {
            ".worklog/scheduled-prompts.json": " M",
            "agent-infra/generated.txt": " M",
        },
    }
    violations = implement._main_checkout_guard_violations(
        baseline, current, expected_dirty=("agent-infra/",)
    )
    assert violations == []


def test_violations_ignore_pre_existing_dirty_unchanged():
    """Dirt present in the baseline snapshot is not foreign churn."""
    baseline = {"taken_at": "T0", "status": {"src/wip.ts": " M"}}
    current = {"taken_at": "T1", "status": {"src/wip.ts": " M"}}
    assert implement._main_checkout_guard_violations(baseline, current) == []


def test_violations_own_churn_allow_list_never_hides_deletion():
    """Finish's own fast-forward churn is expected, a deletion is not."""
    baseline = {"taken_at": "T0", "status": {}}
    current = {
        "taken_at": "T1",
        "status": {
            "docs/dev/worktree-isolation.md": " M",  # fast-forward content change
            "packages/shared/src/database.ts": " D",  # foreign wipe
        },
    }
    violations = implement._main_checkout_guard_violations(
        baseline, current, own_churn={"docs/dev/worktree-isolation.md"}
    )
    assert violations == ["packages/shared/src/database.ts [ D]"]


def test_violations_own_churn_list_only_matches_listed_paths():
    """Only paths actually changed by the fast-forward are allow-listed."""
    baseline = {"taken_at": "T0", "status": {}}
    current = {"taken_at": "T1", "status": {"docs/dev/other.md": " M"}}
    violations = implement._main_checkout_guard_violations(
        baseline, current, own_churn={"docs/dev/worktree-isolation.md"}
    )
    assert violations == ["docs/dev/other.md [ M]"]


# ---------------------------------------------------------------------------
# Real-repo snapshot → delete → verify cycle (the defect class)
# ---------------------------------------------------------------------------


def test_snapshot_verify_detects_real_deletion(tmp_path):
    """A real tracked-file deletion between two snapshots is detected."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    (repo / "packages").mkdir()
    (repo / "packages" / "shared").mkdir()
    target = repo / "packages" / "shared" / "src.ts"
    target.write_text("export const x = 1;\n", encoding="utf-8")
    git_sandbox.commit_all(repo, "seed")

    baseline = implement._snapshot_main_checkout(str(repo))
    assert baseline["status"] == {}

    target.unlink()
    current = implement._snapshot_main_checkout(str(repo))
    violations = implement._main_checkout_guard_violations(baseline, current)
    assert violations == ["packages/shared/src.ts [ D]"]


def test_snapshot_filters_worklog_dirt(tmp_path):
    """``.worklog/`` churn is invisible to the guard's status map."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    (repo / "packages" / "shared").mkdir(parents=True)
    (repo / "packages" / "shared" / "src.ts").write_text("x\n", encoding="utf-8")
    git_sandbox.commit_all(repo, "seed")

    (repo / ".worklog").mkdir()
    (repo / ".worklog" / "scheduled-prompts.json").write_text("{}", encoding="utf-8")

    snapshot = implement._snapshot_main_checkout(str(repo))
    assert snapshot["status"] == {}


def test_snapshot_reports_worktree_foreign_delete_across_checkouts(tmp_path):
    """A worktree-scoped deletion of a main-checkout file is caught.

    This is the exact "worktree runs, main checkout mutates" configuration of
    the incident: the snapshot is taken from the linked worktree's cwd but
    targets the main checkout.
    """
    main = git_sandbox.init_repo(tmp_path / "main")
    (main / "packages" / "shared").mkdir(parents=True)
    target = main / "packages" / "shared" / "src.ts"
    target.write_text("x\n", encoding="utf-8")
    git_sandbox.commit_all(main, "seed")

    wt = git_sandbox.add_worktree(main, tmp_path / "wt", "wl-TEST-child")
    baseline = implement._snapshot_main_checkout(str(main))

    target.unlink()
    current = implement._snapshot_main_checkout(str(main))
    violations = implement._main_checkout_guard_violations(baseline, current)
    assert violations == ["packages/shared/src.ts [ D]"]

    # Clean up the worktree registration so tmp_path can be removed.
    git_sandbox.run_git(main, ["worktree", "remove", "--force", str(wt)], check=True)


# ---------------------------------------------------------------------------
# Actor evidence
# ---------------------------------------------------------------------------


def test_actor_evidence_unknown_when_no_other_worktrees(tmp_path):
    """With no other worktree state files, evidence is ``unknown``."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    evidence = implement._main_checkout_actor_evidence(str(repo), "/tmp/none")
    assert evidence == "unknown"


# ---------------------------------------------------------------------------
# phase_finish behaviour
# ---------------------------------------------------------------------------


#: Stable full commit hash the fake git resolves every revision to. The
#: verified local-dev fast-forward and the post-push ancestry assertion
#: (SA-0MVPZG7A0000N66U) resolve refs with ``git rev-parse --verify``; a
#: resolvable published commit keeps both paths real instead of tripping the
#: fail-closed assertion (covered separately by
#: ``tests/test_implement_finish_local_dev_verified_sync.py``).
_FAKE_COMMIT = "1" * 40


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    stdout = f"{_FAKE_COMMIT}\n" if len(cmd) > 1 and cmd[1] == "rev-parse" else ""
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")


def _build_result() -> dict:
    return {"success": True, "stdout": "no build", "stderr": "", "exit_code": 0,
            "skipped": True}


def _tests_result() -> dict:
    return {"success": True, "failures": [], "skipped": False, "tooling": "suite",
            "scope": "changed", "notices": []}


def _cleanup_result() -> dict:
    return {"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""}


@contextlib.contextmanager
def _finish_harness(tmp_path):
    """Patch every external boundary of ``phase_finish`` (no real git).

    Mirrors ``test_finish_no_diff_status._mocked_phase_finish`` so the two
    files stay consistent; additionally lets callers patch
    ``_snapshot_main_checkout`` by accepting ``extra_patches``.
    """
    worktree = str(tmp_path / "wt")
    Path(worktree).mkdir(parents=True, exist_ok=True)
    state = implement.ImplementState(
        work_item_id="WL-TEST",
        worktree_path=worktree,
        repo_root=str(tmp_path / "main"),
        parent_branch="dev",
    )
    update_status = MagicMock()
    store_show = {"workItem": {"status": "completed", "stage": "in_review"}}
    show = MagicMock(return_value=store_show)
    extra: dict = {}

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=worktree)
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(patch.object(implement, "run_build", return_value=_build_result()))
        # ``phase_finish`` runs its gate against the would-be-commit tree
        # (``run_gate_suite``, SA-0MVQHVGFR006W8LG) and stages the change set
        # in the real index through ``stage_tested_tree`` once green. Both act
        # on a real git repository; this harness drives ``phase_finish`` from a
        # bare temp directory, so both boundaries are stubbed green (the gate's
        # own behaviour is covered by ``test_finish_gate_*.py``).
        stack.enter_context(
            patch.object(
                implement,
                "run_gate_suite",
                return_value=(
                    _tests_result(),
                    {
                        "tree": "abc1234",
                        "scope": "changed",
                        "cached": False,
                        "index_unchanged": True,
                    },
                ),
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "stage_tested_tree",
                return_value={
                    "success": True,
                    "tree": "abc1234",
                    "expected": "abc1234",
                },
            )
        )
        stack.enter_context(patch.object(implement, "run_tests", return_value=_tests_result()))
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(implement, "git_get_commit_hash", return_value="abc1234")
        )
        stack.enter_context(
            patch.object(implement, "git_push_to_dev", return_value=True)
        )
        stack.enter_context(patch.object(implement, "run_cmd", side_effect=_fake_run_cmd))
        stack.enter_context(patch.object(implement, "_record_finish_own_churn", return_value=[]))
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
            patch.object(implement.StatusLifecycle, "update_status", update_status)
        )
        stack.enter_context(patch.object(implement.StatusLifecycle, "show", show))
        yield {"update_status": update_status, "extra": extra, "stack": stack}


def _snapshots(statuses: list[dict[str, str]]):
    """A ``_snapshot_main_checkout`` fake returning scripted status maps."""
    calls = {"n": 0}

    def fake(repo_root: str) -> dict:
        status = statuses[min(calls["n"], len(statuses) - 1)]
        calls["n"] += 1
        return {"taken_at": f"T{calls['n']}", "status": status}

    return fake


def test_finish_aborts_on_foreign_deletion_naming_path(tmp_path):
    """A foreign main-checkout deletion aborts finish with the path named.

    The work item is left ``open`` (never ``completed``/``in_review``), the
    abort message carries the offending path and a recovery command, and the
    guard step reports the failure verdict.
    """
    with _finish_harness(tmp_path) as mocks:
        fake = _snapshots([
            {},  # baseline (run start)
            {"packages/shared/src/database.ts": " D"},  # pre-push verify
            {"packages/shared/src/database.ts": " D"},  # pre-removal verify
        ])
        with patch.object(implement, "_snapshot_main_checkout", side_effect=fake):
            report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False
    assert "packages/shared/src/database.ts" in report["message"]
    assert "git checkout --" in report["message"]
    assert "unknown" in report["message"] or "actor" in report["message"]

    # The item is released to open — the LAST status write is ``open``; a
    # guard abort never leaves the item completed/in_review. (An earlier
    # ``completed`` write here is the harness's fake ``__exit__`` restore of
    # the terminal store_show fixture, not a terminal transition: in a real
    # run finish claims the item to in_progress first.)
    last = mocks["update_status"].call_args_list[-1]
    assert last.args[1] == "open"

    # Observability: the guard step records verify times and the verdict.
    guard = report["steps"]["main_checkout_guard"]
    assert guard["verdict"] == "fail"
    stages = [v["stage"] for v in guard["verifies"]]
    assert "pre_push" in stages
    assert guard["snapshot_at"]  # snapshot time recorded
    failing = [v for v in guard["verifies"] if v["verdict"] == "fail"]
    assert failing and failing[0]["offending"] == ["packages/shared/src/database.ts [ D]"]


def test_finish_own_fast_forward_churn_does_not_abort(tmp_path):
    """Finish's own Step-5 fast-forward churn is classified as expected."""
    with _finish_harness(tmp_path) as mocks:
        fake = _snapshots([
            {},  # baseline
            {"docs/dev/implement-skill-reference.md": " M"},  # pre-push (ff churn)
            {"docs/dev/implement-skill-reference.md": " M"},  # pre-removal
        ])
        with (
            patch.object(implement, "_snapshot_main_checkout", side_effect=fake),
            patch.object(
                implement,
                "_record_finish_own_churn",
                return_value=["docs/dev/implement-skill-reference.md"],
            ),
        ):
            report = implement.phase_finish(
                "WL-TEST", json_output=True, no_refactor=True
            )

    assert report["success"] is True, report["message"]
    guard = report["steps"]["main_checkout_guard"]
    assert guard["verdict"] == "pass"
    assert guard["own_churn"] == ["docs/dev/implement-skill-reference.md"]
    terminal = [
        c for c in mocks["update_status"].call_args_list
        if c.kwargs.get("status") == "completed"
    ]
    assert terminal, "a clean finish must reach the terminal transition"


def test_finish_clean_run_reports_guard_step(tmp_path):
    """A clean finish records snapshot/verify times and a pass verdict."""
    with _finish_harness(tmp_path):
        fake = _snapshots([{}, {}, {}])
        with patch.object(implement, "_snapshot_main_checkout", side_effect=fake):
            report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    guard = report["steps"]["main_checkout_guard"]
    assert guard["verdict"] == "pass"
    assert guard["snapshot_at"]
    assert [v["stage"] for v in guard["verifies"]] == ["pre_push", "pre_worktree_removal"]
    assert all(v["verdict"] == "pass" for v in guard["verifies"])


def test_finish_abort_before_worktree_removal_leaves_item_open(tmp_path):
    """A deletion appearing only before worktree removal also aborts loudly."""
    with _finish_harness(tmp_path) as mocks:
        fake = _snapshots([
            {},  # baseline
            {},  # pre-push: clean
            {"src/gone.ts": " D"},  # pre-removal: foreign deletion
        ])
        with patch.object(implement, "_snapshot_main_checkout", side_effect=fake):
            report = implement.phase_finish("WL-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False
    assert "src/gone.ts" in report["message"]
    stages = [v["stage"] for v in report["steps"]["main_checkout_guard"]["verifies"]]
    assert stages == ["pre_push", "pre_worktree_removal"]
    guard = report["steps"]["main_checkout_guard"]
    pre_removal = [v for v in guard["verifies"] if v["stage"] == "pre_worktree_removal"]
    assert pre_removal and pre_removal[0]["verdict"] == "fail"
    assert mocks["update_status"].call_args_list[-1].args[1] == "open"
