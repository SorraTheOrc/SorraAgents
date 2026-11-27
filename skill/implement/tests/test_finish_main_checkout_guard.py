
# <!-- REFACTOR-SA-0MWRDN7TC003VLD0
# smell: unknown
# severity: low
# description: Unparenthesized implicit string concatenation in collection
# -->

# <!-- REFACTOR-SA-0MWI3MT6L004K1L7
# smell: unused_import
# severity: critical
# description: Local variable `mocks` is assigned to but never used
# -->
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
  finish reports the guard step with snapshot/verify times and a pass verdict,
- **failure-path verify points** (SA-0MVXVBYG00075CIQ): every pre-push abort
  (build failure, gate failure in JSON mode, retries exhausted, interactive
  user abort) records a verify entry, so a foreign deletion is reported loudly
  instead of leaving ``verifies: []`` — the 2026-10-31 shape, where 37 foreign
  deletions in the main checkout were never evaluated. This is the **test-first
  slice**: the verify points are wired by the sibling item
  SA-0MVXVBYXI000QMXM, so the five guard assertions below pin that contract.
  The unmarked companion
  ``test_each_pre_push_abort_path_returns_a_failure_report`` proves the harness
  really reaches each abort path, so a failure of the guard tests means
  "verify point absent", never "harness broken".
"""

from __future__ import annotations

import contextlib
import json
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


def _write_sibling_state(repo: Path, work_item_id: str) -> Path:
    """Write a sibling worktree state file naming *work_item_id*."""
    sibling = repo / ".worklog" / "worktrees" / f"wl-{work_item_id}"
    sibling.mkdir(parents=True)
    (sibling / implement.STATE_FILE_NAME).write_text(
        json.dumps(
            {
                "work_item_id": work_item_id,
                "worktree_path": str(sibling),
                "repo_root": str(repo),
                "started_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return sibling


def test_actor_evidence_names_a_sibling_owner_instead_of_the_run(tmp_path):
    """A sibling state file names the likely actor (never the running item)."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    _write_sibling_state(repo, "WL-SIBLING")
    own = tmp_path / "own-worktree"
    own.mkdir()

    evidence = implement._main_checkout_actor_evidence(
        str(repo), str(own), "WL-SELF"
    )

    assert "work item WL-SIBLING" in evidence
    assert "WL-SELF" not in evidence


def test_actor_evidence_never_names_the_own_worktree(tmp_path):
    """The caller's own state file is excluded from the evidence."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    own = _write_sibling_state(repo, "WL-OWN")

    evidence = implement._main_checkout_actor_evidence(
        str(repo), str(own), "WL-OWN"
    )

    assert evidence == "unknown"


def test_verify_records_actor_evidence_on_failure(tmp_path):
    """A guard failure records the discoverable actor beside the paths."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    target = repo / "packages" / "shared.ts"
    target.parent.mkdir()
    target.write_text("export {}\n", encoding="utf-8")
    git_sandbox.commit_all(repo, "seed")
    _write_sibling_state(repo, "WL-SIBLING")
    own = tmp_path / "own-worktree"
    own.mkdir()

    baseline = implement._snapshot_main_checkout(str(repo))
    target.unlink()
    guard: dict = {"verifies": [], "verdict": "pass"}

    violations = implement._verify_main_checkout(
        guard,
        baseline,
        repo_root=str(repo),
        worktree_path=str(own),
        own_churn=[],
        expected_dirty=(),
        stage="pre_push",
        work_item_id="WL-SELF",
    )

    assert violations == ["packages/shared.ts [ D]"]
    assert "work item WL-SIBLING" in guard["verifies"][0]["actor_evidence"]


def test_annotate_excluded_churn_notes_appends_unknown_explicitly(tmp_path):
    """The actor second signal is always labelled, ``unknown`` included."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    own = tmp_path / "own-worktree"
    own.mkdir()
    notes = [
        "excluded: concurrently resolved working-tree change in "
        f"{repo}: src/app.ts [ M]"
    ]

    annotated, evidence = implement._annotate_excluded_churn_notes(
        notes, str(repo), str(own), "WL-SELF"
    )

    assert evidence == "unknown"
    assert annotated == [f"{notes[0]} [likely actor: unknown]"]


def test_annotate_excluded_churn_notes_names_the_actor(tmp_path):
    """The second signal names the owner when one is discoverable."""
    repo = git_sandbox.init_repo(tmp_path / "main")
    _write_sibling_state(repo, "WL-SIBLING")
    own = tmp_path / "own-worktree"
    own.mkdir()

    annotated, evidence = implement._annotate_excluded_churn_notes(
        ["excluded: path [ M]"], str(repo), str(own), "WL-SELF"
    )

    assert "work item WL-SIBLING" in evidence
    assert annotated == [f"excluded: path [ M] [likely actor: {evidence}]"]


def test_annotate_excluded_churn_notes_does_not_annotate_twice(tmp_path):
    """A note the outer guard already annotated is passed through unchanged.

    ``run_tests`` annotates its own output (WL-0MWR1P1F3008AH3V), so the finish
    report must adopt that signal instead of appending a second lookup and a
    duplicate label. The bounded actor lookup is therefore not called.
    """
    note = (
        "excluded: concurrently resolved working-tree change in /repo: "
        "docs/dev/x.md [ M] [likely actor: WL-SIBLING]"
    )

    with patch.object(implement, "_main_checkout_actor_evidence") as lookup:
        annotated, evidence = implement._annotate_excluded_churn_notes(
            [note], str(tmp_path), "/tmp/none", ""
        )

    lookup.assert_not_called()
    assert annotated == [note]
    assert evidence == "WL-SIBLING"


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
    if len(cmd) > 1 and cmd[1] == "merge-base":
        # A freshly-committed feature branch is not yet on origin/dev:
        # the pre-push already-landed check (SA-0MW22SEMV00230O5) must see
        # an unpushed commit and proceed to the push.
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
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
        # The post-push ancestry assertion (Step 6a) resolves against the
        # stubbed main checkout; model a published commit so the terminal
        # path is exercised (the assertion's own fail-closed behaviour has
        # dedicated coverage in test_implement_finish_local_dev_verified_sync.py,
        # SA-0MVPZG7A0000N66U).
        stack.enter_context(
            patch.object(
                implement,
                "_assert_commit_on_origin_dev",
                return_value={
                    "verified": True,
                    "resolved_commit": _FAKE_COMMIT,
                    "target": "refs/remotes/origin/dev",
                    "target_tip": _FAKE_COMMIT,
                },
            )
        )
        # The push subject is the synthetic hash ``abc1234`` in this harness, so
        # the worktree-holding guard (SA-0MVUTXJ4300301ZN) is stubbed like the
        # push itself; it has dedicated coverage in
        # ``skill/implement/tests/test_implement_push_from_worktree.py``.
        stack.enter_context(patch.object(implement, "_assert_push_subject"))
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
        wl_add_comment = MagicMock(return_value=True)
        stack.enter_context(patch.object(implement, "wl_add_comment", wl_add_comment))
        stack.enter_context(patch.object(implement, "_surface_postfix_prose"))
        stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", update_status)
        )
        stack.enter_context(patch.object(implement.StatusLifecycle, "show", show))
        yield {
            "update_status": update_status,
            "wl_add_comment": wl_add_comment,
            "extra": extra,
            "stack": stack,
        }


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
    """Finish's post-push fast-forward churn is classified as expected.

    The local-``dev`` advance now runs only **after** the push reports
    success (WL-0MWFKDH78002T2CR AC1), so the pre-push guard verify sees a
    clean checkout; the pre-worktree-removal verify sees the post-push
    advance's churn, attributed to finish via ``own_churn`` so it is never
    misreported as a foreign mutation.
    """
    with _finish_harness(tmp_path) as mocks:
        fake = _snapshots([
            {},  # baseline
            {},  # pre-push (no advance yet — the fix)
            {"docs/dev/implement-skill-reference.md": " M"},  # pre-removal (post-push ff churn)
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


def test_finish_never_advances_local_dev_pre_push(tmp_path):
    """AC1: the pre-push guard verify never sees finish's own churn.

    A pre-existing foreign dirty path still aborts pre-push: with no advance
    to attribute, nothing can excuse a main-checkout mutation, and the item
    is released to open instead of silently finishing.
    """
    with _finish_harness(tmp_path):
        fake = _snapshots([
            {},  # baseline
            {"docs/dev/implement-skill-reference.md": " M"},  # pre-push foreign dirt
            {"docs/dev/implement-skill-reference.md": " M"},
        ])
        with patch.object(implement, "_snapshot_main_checkout", side_effect=fake):
            report = implement.phase_finish(
                "WL-TEST", json_output=True, no_refactor=True
            )

    assert report["success"] is False, report["message"]
    guard = report["steps"]["main_checkout_guard"]
    assert guard["verdict"] == "fail"
    failing = [v for v in guard["verifies"] if v["verdict"] == "fail"]
    assert failing and failing[0]["stage"] == "pre_push", guard


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


def test_finish_surfaces_excluded_foreign_churn(tmp_path, caplog):
    """AC1: a green gate still reports the excluded foreign churn and its owner.

    The excluded paths never fail the finish, but they must reach the JSON
    report and the log, labelled with the discoverable actor rather than
    implied as damage caused by this run (WL-0MWR1P1F3008AH3V).
    """
    note = (
        "excluded: concurrently resolved working-tree change in /main: "
        "docs/dev/x.md [ M]"
    )
    tests = {**_tests_result(), "live_repo_excluded": [note]}
    actor = "work item WL-SIBLING (claimed by agent)"

    with _finish_harness(tmp_path), patch.object(
        implement,
        "run_gate_suite",
        return_value=(
            tests,
            {
                "tree": "abc1234",
                "scope": "changed",
                "cached": False,
                "index_unchanged": True,
            },
        ),
    ), patch.object(
        implement, "_main_checkout_actor_evidence", return_value=actor
    ), patch.object(
        implement, "_snapshot_main_checkout", side_effect=_snapshots([{}, {}, {}])
    ):
        caplog.set_level("INFO", logger="implement.scripts.implement")
        report = implement.phase_finish(
            "WL-TEST", json_output=True, no_refactor=True
        )

    assert report["success"] is True, report["message"]
    assert report["live_repo_excluded"] == [f"{note} [likely actor: {actor}]"]
    assert report["live_repo_actor_evidence"] == actor
    assert "Live-repo guard note" in caplog.text
    assert actor in caplog.text


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


# ---------------------------------------------------------------------------
# Failure-path verify points (SA-0MVXVBYG00075CIQ — test-first slice)
# ---------------------------------------------------------------------------
#
# The guard snapshots the main checkout at Step 0.75 but originally verified
# only at ``pre_push`` and ``pre_worktree_removal``. Every pre-push abort
# (build failure, gate failure in JSON mode, retries exhausted, interactive
# user abort) returned before either point, so ``verifies`` stayed ``[]`` and
# foreign deletions in the main checkout went unreported (2026-10-31: 37
# deletions, ``verifies: []``). The sibling item SA-0MVXVBYXI000QMXM wires one
# verify into each of those paths; the tests below pin that contract.

#: The failing-stage label each abort path records in ``guard["verifies"]``
#: (the wiring sibling's contract). Pinned so a report consumer can tell a
#: failure-path verify from the two push-path stages.
_FAILURE_VERIFY_STAGE = {
    "build_failure": "build_failure",
    "gate_json": "gate_failure",
    "gate_retries": "gate_failure",
    "user_abort": "user_abort",
}

#: The two verify stages that already existed before the failure-path wiring.
_PUSH_PATH_STAGES = ("pre_push", "pre_worktree_removal")

#: Statuses that mean the item was advanced instead of released.
_TERMINAL_STATUSES = {"completed", "in_review", "done"}

#: A foreign deletion in the main checkout — the 2026-10-31 defect class.
_FOREIGN_DELETION = "packages/shared/src/database.ts"


def _failing_gate_result() -> dict:
    """A parsed gate failure (a genuine test failure, not infrastructure)."""
    return {
        "success": False,
        "failures": ["tests/test_thing.py::test_thing - AssertionError: boom"],
        "skipped": False,
        "tooling": "pytest",
        "scope": "changed",
        "notices": [],
        "notice_codes": [],
    }


def _failed_build_result() -> dict:
    return {
        "success": False,
        "stdout": "",
        "stderr": "src/index.ts(3,1): error TS2304: Cannot find name 'x'.",
        "exit_code": 2,
        "skipped": False,
    }


def _comment_bodies(mocks) -> list[str]:
    """Bodies of every ``wl_add_comment`` call, in call order."""
    return [
        call.args[1]
        for call in mocks["wl_add_comment"].call_args_list
        if len(call.args) >= 2
    ]


def _guard_abort_comments(mocks) -> list[str]:
    """The main-checkout guard abort comments posted so far."""
    return [b for b in _comment_bodies(mocks) if "Main-checkout mutation detected" in b]


def _assert_released_to_open(mocks, *, expect_status_write: bool = True) -> None:
    """The abort path never advances the item and releases it to ``open``.

    No abort path may leave the item ``completed``/``in_review``; the last
    status write must be ``open``. The JSON-mode gate early return is the one
    nuance about *who* writes it: it hands the reset to the orchestrating
    agent loop (it writes no status itself), so that path asserts the weaker —
    still fail-honest — "never advanced to a terminal status".
    """
    writes = [
        call.args[1]
        for call in mocks["update_status"].call_args_list
        if len(call.args) >= 2
    ]
    assert not [w for w in writes if w in _TERMINAL_STATUSES], writes
    if expect_status_write:
        assert writes, "the abort path must release the work item"
    if writes:
        assert writes[-1] == "open", writes


def _failure_verify(report) -> dict:
    """The failure-path verify entry (the last one, excluding push-path stages)."""
    verifies = report["steps"]["main_checkout_guard"]["verifies"]
    assert verifies, "the failure path recorded no main-checkout verify at all"
    failure_entries = [v for v in verifies if v["stage"] not in _PUSH_PATH_STAGES]
    assert failure_entries, (
        "no failure-path verify entry; recorded stages: "
        f"{[v['stage'] for v in verifies]}"
    )
    return failure_entries[-1]


def _run_failure_path(tmp_path, *, kind: str, snapshots: list[dict[str, str]]):
    """Drive one pre-push abort path through the real ``phase_finish``.

    ``kind`` selects the abort: ``build_failure`` (Step 2), ``gate_json``
    (Step 3 JSON-mode early return), ``gate_retries`` ("Tests failed after N
    retries") or ``user_abort`` (the interactive ``abort`` choice).
    ``snapshots`` scripts ``_snapshot_main_checkout``: the baseline snapshot
    first, then the failure-path verify (the fake repeats the last map).

    Only the abort input is scripted — the guard, the report shape, the status
    transition and the comments are production code.
    """
    failing_gate = (_failing_gate_result(), {"available": True, "tree": "failed"})
    with _finish_harness(tmp_path) as mocks, contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                implement,
                "_snapshot_main_checkout",
                side_effect=_snapshots(snapshots),
            )
        )
        if kind == "build_failure":
            stack.enter_context(
                patch.object(
                    implement, "run_build", return_value=_failed_build_result()
                )
            )
        elif kind in {"gate_json", "gate_retries", "user_abort"}:
            stack.enter_context(
                patch.object(implement, "run_gate_suite", return_value=failing_gate)
            )
        else:  # pragma: no cover - guard against a typo'd test
            raise AssertionError(f"unknown failure path: {kind}")
        if kind == "gate_retries":
            stack.enter_context(patch("builtins.input", return_value=""))
        elif kind == "user_abort":
            stack.enter_context(patch("builtins.input", return_value="abort"))
        report = implement.phase_finish(
            "WL-TEST",
            json_output=(kind == "gate_json"),
            no_refactor=True,
            max_retry=1 if kind == "gate_retries" else implement.DEFAULT_MAX_RETRY,
        )
    return report, mocks


def test_each_pre_push_abort_path_returns_a_failure_report(tmp_path):
    """Harness canary: every abort path really is reached (unmarked companion).

    Green before and after the failure-path wiring. If it ever fails, a
    marked test below is failing for the wrong reason (the harness never
    reached the abort), not because the guard verify is absent.
    """
    # Every abort except the JSON-mode gate posts its own failure comment; the
    # JSON mode hands the failure back in the report instead (the agent loop
    # drives the fix-and-re-run externally), so it is asserted on
    # ``report["message"]``/``report["test_phase"]``.
    expected_failure_comment = {
        "build_failure": "Build failed during finish phase",
        "gate_retries": "Tests failed after 1 retries",
        "user_abort": "Aborted by user during test fix loop",
    }
    for kind, fragment in expected_failure_comment.items():
        report, mocks = _run_failure_path(
            tmp_path / kind, kind=kind, snapshots=[{}, {}]
        )
        assert report["success"] is False, (kind, report.get("message"))
        bodies = _comment_bodies(mocks)
        assert any(fragment in body for body in bodies), (kind, bodies)
        _assert_released_to_open(mocks)

    report, mocks = _run_failure_path(
        tmp_path / "gate_json", kind="gate_json", snapshots=[{}, {}]
    )
    assert report["success"] is False
    assert report["message"].startswith("Test run 1/")
    assert report["test_phase"]["reason"] == "test_failure"
    assert _comment_bodies(mocks) == []
    _assert_released_to_open(mocks, expect_status_write=False)


def test_gate_json_abort_verifies_main_checkout(tmp_path):
    """Gate failure in JSON mode still verifies and reports a foreign deletion."""
    report, mocks = _run_failure_path(
        tmp_path, kind="gate_json", snapshots=[{}, {_FOREIGN_DELETION: " D"}]
    )

    assert report["success"] is False
    entry = _failure_verify(report)
    assert entry["stage"] == _FAILURE_VERIFY_STAGE["gate_json"]
    assert entry["verdict"] == "fail"
    assert entry["offending"] == [f"{_FOREIGN_DELETION} [ D]"]
    assert report["steps"]["main_checkout_guard"]["verdict"] == "fail"

    guard_comments = _guard_abort_comments(mocks)
    assert guard_comments, _comment_bodies(mocks)
    assert any(
        _FOREIGN_DELETION in body
        and "git checkout --" in body
        and "Concurrent actor evidence:" in body
        for body in guard_comments
    ), guard_comments
    # The JSON mode reports the gate failure in the report rather than a
    # comment; adding the guard comment must not swallow that report.
    assert report["message"].startswith("Test run 1/")
    _assert_released_to_open(mocks, expect_status_write=False)


def test_gate_retries_exhausted_verifies_main_checkout(tmp_path):
    """The "Tests failed after N retries" exit still verifies the main checkout."""
    report, mocks = _run_failure_path(
        tmp_path, kind="gate_retries", snapshots=[{}, {_FOREIGN_DELETION: " D"}]
    )

    assert report["success"] is False
    entry = _failure_verify(report)
    assert entry["stage"] == _FAILURE_VERIFY_STAGE["gate_retries"]
    assert entry["verdict"] == "fail"
    assert entry["offending"] == [f"{_FOREIGN_DELETION} [ D]"]
    assert report["steps"]["main_checkout_guard"]["verdict"] == "fail"

    guard_comments = _guard_abort_comments(mocks)
    assert guard_comments, _comment_bodies(mocks)
    assert any(
        _FOREIGN_DELETION in body
        and "git checkout --" in body
        and "Concurrent actor evidence:" in body
        for body in guard_comments
    ), guard_comments
    assert any(
        "Tests failed after 1 retries" in body for body in _comment_bodies(mocks)
    ), "the pre-existing retries-exhausted comment must still be posted"
    _assert_released_to_open(mocks)


def test_build_failure_verifies_main_checkout(tmp_path):
    """A Step-2 build failure still verifies the main checkout."""
    report, mocks = _run_failure_path(
        tmp_path, kind="build_failure", snapshots=[{}, {_FOREIGN_DELETION: " D"}]
    )

    assert report["success"] is False
    entry = _failure_verify(report)
    assert entry["stage"] == _FAILURE_VERIFY_STAGE["build_failure"]
    assert entry["verdict"] == "fail"
    assert entry["offending"] == [f"{_FOREIGN_DELETION} [ D]"]
    assert report["steps"]["main_checkout_guard"]["verdict"] == "fail"

    guard_comments = _guard_abort_comments(mocks)
    assert guard_comments, _comment_bodies(mocks)
    assert any(
        _FOREIGN_DELETION in body
        and "git checkout --" in body
        and "Concurrent actor evidence:" in body
        for body in guard_comments
    ), guard_comments
    assert any(
        "Build failed during finish phase" in body for body in _comment_bodies(mocks)
    ), "the pre-existing build failure comment must still be posted"
    _assert_released_to_open(mocks)


def test_user_abort_verifies_main_checkout(tmp_path):
    """The interactive fix-loop ``abort`` still verifies the main checkout."""
    report, mocks = _run_failure_path(
        tmp_path, kind="user_abort", snapshots=[{}, {_FOREIGN_DELETION: " D"}]
    )

    assert report["success"] is False
    entry = _failure_verify(report)
    assert entry["stage"] == _FAILURE_VERIFY_STAGE["user_abort"]
    assert entry["verdict"] == "fail"
    assert entry["offending"] == [f"{_FOREIGN_DELETION} [ D]"]
    assert report["steps"]["main_checkout_guard"]["verdict"] == "fail"

    guard_comments = _guard_abort_comments(mocks)
    assert guard_comments, _comment_bodies(mocks)
    assert any(
        _FOREIGN_DELETION in body
        and "git checkout --" in body
        and "Concurrent actor evidence:" in body
        for body in guard_comments
    ), guard_comments
    assert any(
        "Aborted by user during test fix loop" in body
        for body in _comment_bodies(mocks)
    ), "the pre-existing user-abort comment must still be posted"
    _assert_released_to_open(mocks)


def test_clean_gate_abort_records_passing_failure_verify(tmp_path):
    """A clean abort records a *passing* failure-path verify and no abort.

    The SA-0MUIJAHXS003M7BG false-positive class must not regress: adding
    verify points to failure paths may not turn benign churn (or no churn at
    all) into a main-checkout abort. The item is still released to ``open``
    by the pre-existing failure handling.
    """
    report, mocks = _run_failure_path(tmp_path, kind="gate_json", snapshots=[{}, {}])

    assert report["success"] is False
    entry = _failure_verify(report)
    assert entry["stage"] == _FAILURE_VERIFY_STAGE["gate_json"]
    assert entry["verdict"] == "pass"
    assert "offending" not in entry
    assert report["steps"]["main_checkout_guard"]["verdict"] == "pass"
    assert _guard_abort_comments(mocks) == []
    assert report["message"].startswith("Test run 1/")
    _assert_released_to_open(mocks, expect_status_write=False)
