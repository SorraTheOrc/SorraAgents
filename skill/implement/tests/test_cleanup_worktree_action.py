"""The ``implement.py cleanup <id>`` action (SA-0MVTHIZE0007EAKZ).

A plain leaf session that defers worktree removal (``finish --keep-worktree``
or ``IMPLEMENT_KEEP_WORKTREE=1``, delivered by SA-0MVTE8XAJ002IUDR) owns its
own cleanup: the worktree **is** the session's cwd, so it must be removed
after the session has finished everything that needs that cwd. Previously the
only way to do that was a raw ``git worktree remove``, which bypasses the
safety gates (dirty trees, process cleanup) and can silently discard
uncommitted work.

The contract pinned here — ``implement.py cleanup <id> [--json]
[--worktree-path <path>]``:

- **AC2 — cleanup still happens deterministically.** For a terminal item with
  a clean worktree the removal goes through the *existing* single removal
  path (``cleanup_worktree_processes`` → ``_remove_worktree`` →
  ``remove_state``), exactly once, and the report records ``removed: true``,
  ``path`` and ``removed_state: true``.
- **AC4 — interruption safety and idempotence.** An already-absent worktree is
  a reported no-op success (``already_absent: true``, ``removed: false``,
  exit 0) that still clears a stale state file, and a repeat invocation
  returns an equivalent report.
- **AC3 — consistency with the driven fix.** No third removal mechanism: the
  action delegates to the shared removal path, so patching
  ``_remove_worktree`` / ``cleanup_worktree_processes`` intercepts every
  deletion (the directory survives the run, proving nothing removed it
  behind the patches) and the report reuses the driver's retention
  vocabulary.
- **Safety gates.** A non-terminal item, an unreadable status, a dirty
  worktree, the main checkout and a plain directory are all **retained**
  (zero ``_remove_worktree`` calls) and reported with an actionable remedy.
- **CLI surface.** ``cleanup`` is accepted by ``parse_args``, dispatched by
  ``main()`` and documented in ``--help``; ``phase_cleanup``'s docstring
  states the ordering contract ("last bash command").

The tests drive ``phase_cleanup``/``main`` with their external boundaries
(the ``wl`` store, worktree resolution, the repo-root lookup, the dirty check,
process cleanup and ``_remove_worktree``) patched — the patched-boundary
style of ``test_finish_no_diff_status.py`` — and assert observable behaviour
only: the recorded calls, the on-disk fixture and the returned report.
"""

from __future__ import annotations

import contextlib
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

ITEM_ID = "SA-CLEANUP"
MAIN_REPO_ROOT = "/tmp/cleanup-main-repo-root"

#: The report keys the action promises (contract, frozen by the work item).
REPORT_KEYS = {
    "removed",
    "path",
    "already_absent",
    "retained_dirty",
    "retained_non_terminal",
    "retained_no_worktree",
    "removed_state",
    "message",
}


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Run every test from an empty directory.

    Resolution prefers a candidate whose ``.implement_state.json`` records the
    item, and the *current directory* is one such candidate — in the real
    suite that would be this repository's own worktree. An empty cwd keeps the
    fixture worktrees authoritative.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.chdir(session_dir)
    return session_dir


@pytest.fixture(autouse=True)
def _clean_deferral_env(monkeypatch):
    """Neutralise ambient deferral switches (a driven session sets them)."""
    monkeypatch.delenv(implement.DRIVE_RECURSION_ENV, raising=False)
    monkeypatch.delenv(implement.KEEP_WORKTREE_ENV, raising=False)


def _worktree_fixture(tmp_path: Path) -> Path:
    """Create a worktree-looking directory (``.git`` is a *file*) + state file."""
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake\n")
    (worktree / implement.STATE_FILE_NAME).write_text("{}")
    return worktree


def _state(worktree: Path, work_item_id: str = ITEM_ID) -> implement.ImplementState:
    return implement.ImplementState(
        work_item_id=work_item_id,
        worktree_path=str(worktree),
        repo_root=MAIN_REPO_ROOT,
        parent_branch="dev",
    )


def _remove_worktree_side_effect(path: str, repo_root: str | None = None) -> bool:
    """Simulate a real removal so the absence assertions are meaningful."""
    shutil.rmtree(path, ignore_errors=True)
    return True


@contextlib.contextmanager
def _patched_cleanup(
    worktree: Path | None,
    *,
    status: str = "completed",
    stage: str = "in_review",
    dirty: bool = False,
    remove_side_effect=_remove_worktree_side_effect,
    repo_root: str = MAIN_REPO_ROOT,
    state: implement.ImplementState | None = None,
):
    """Patch every external boundary of ``phase_cleanup``.

    ``status``/``stage``/``dirty`` model what the store and the tree report;
    ``remove_side_effect`` lets a test decide whether the mocked removal really
    deletes the fixture directory (the default) or leaves it alone so a
    patch-boundary assertion can prove the tree was untouched.
    """
    if state is None and worktree is not None:
        state = _state(worktree)

    with contextlib.ExitStack() as stack:
        discovery = stack.enter_context(
            patch.object(
                implement,
                "_discover_worktree",
                return_value=str(worktree) if worktree is not None else None,
            )
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        wl_show = stack.enter_context(
            patch.object(
                implement,
                "wl_show",
                return_value={"status": status, "stage": stage},
            )
        )
        stack.enter_context(
            patch.object(implement, "_get_repo_root", return_value=repo_root)
        )
        lifecycle_write = stack.enter_context(
            patch.object(implement.StatusLifecycle, "update_status", MagicMock())
        )
        dirty_check = stack.enter_context(
            patch.object(
                implement, "_worktree_has_uncommitted_changes", return_value=dirty
            )
        )
        cleanup = stack.enter_context(
            patch.object(
                implement,
                "cleanup_worktree_processes",
                return_value={"method": "wl_cleanup_worktree", "terminated": 0},
            )
        )
        removal = stack.enter_context(
            patch.object(implement, "_remove_worktree", side_effect=remove_side_effect)
        )
        yield {
            "discovery": discovery,
            "wl_show": wl_show,
            "lifecycle_write": lifecycle_write,
            "dirty_check": dirty_check,
            "cleanup": cleanup,
            "removal": removal,
        }


# ---------------------------------------------------------------------------
# AC2 — the removal runs deterministically through the existing gates
# ---------------------------------------------------------------------------


def test_cleanup_removes_a_terminal_items_clean_worktree(tmp_path):
    """A terminal item's clean worktree is removed exactly once, gates intact."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree) as mocks:
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["success"] is True, report["message"]
    assert report["removed"] is True
    assert report["path"] == str(worktree.resolve())
    assert report["removed_state"] is True

    # The existing removal path ran exactly once, for the resolved path, with
    # the main checkout root (the authoritative value for _remove_worktree's
    # repo-root refusal).
    assert mocks["cleanup"].call_count == 1
    assert mocks["cleanup"].call_args.args[0] == str(worktree.resolve())
    assert mocks["removal"].call_count == 1
    assert mocks["removal"].call_args.args[0] == str(worktree.resolve())
    assert mocks["removal"].call_args.kwargs["repo_root"] == MAIN_REPO_ROOT

    # The tree and its state file are gone.
    assert not worktree.exists()
    assert not (worktree / implement.STATE_FILE_NAME).exists()


def test_cleanup_report_carries_every_contracted_key(tmp_path):
    """The JSON report exposes the contracted key set (agents parse it)."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree):
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert REPORT_KEYS <= set(report), REPORT_KEYS - set(report)
    assert report["phase"] == "cleanup"
    assert report["work_item_id"] == ITEM_ID


@pytest.mark.parametrize(
    ("status", "stage"),
    [("completed", "in_review"), ("completed", ""), ("in_review", "")],
)
def test_cleanup_accepts_the_finished_markers(tmp_path, status, stage):
    """``completed`` and ``in_review`` are the removable (finished) states."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree, status=status, stage=stage):
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["removed"] is True, (status, stage, report["message"])
    assert report["retained_non_terminal"] is False


def test_cleanup_never_mutates_the_lifecycle(tmp_path):
    """Cleanup is a read-only gate + removal: it never claims or transitions."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree) as mocks:
        implement.phase_cleanup(ITEM_ID, json_output=True)

    assert mocks["lifecycle_write"].call_count == 0


# ---------------------------------------------------------------------------
# AC4 — interruption safety: already absent, and idempotent repeats
# ---------------------------------------------------------------------------


def test_cleanup_of_an_absent_worktree_is_a_reported_noop(tmp_path, capsys):
    """An absent tree (resolved via the override) is a success, not an error.

    The stale-state clearing is part of the no-op: ``remove_state`` is still
    called for the resolved path, so an interrupted workflow can simply be
    re-run and never resumes a dead tree.
    """
    absent = tmp_path / "wt"

    with _patched_cleanup(absent) as mocks:
        with patch.object(implement, "remove_state") as remove_state:
            report = implement.phase_cleanup(
                ITEM_ID, json_output=True, worktree_path=str(absent)
            )

    assert report["success"] is True
    assert report["already_absent"] is True
    assert report["removed"] is False
    assert report["path"] == str(absent.resolve())
    assert report["removed_state"] is True, "nothing (no state file) is left behind"
    assert mocks["removal"].call_count == 0, "nothing to remove — no removal call"
    assert remove_state.call_count == 1, "a stale state file is still cleared"
    assert remove_state.call_args.args[0] == str(absent.resolve())

    # JSON mode printed the report, and the CLI exits 0 for a retained no-op.
    assert '"already_absent": true' in capsys.readouterr().out


def test_cleanup_is_idempotent_across_runs(tmp_path):
    """A second invocation (with the path remembered) is an equivalent no-op."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree):
        first = implement.phase_cleanup(
            ITEM_ID, json_output=True, worktree_path=str(worktree)
        )
    # Discovery can no longer find a removed tree, so the repeat run passes the
    # path explicitly — the shape an interrupted workflow resumes with.
    with _patched_cleanup(None, state=None):
        second = implement.phase_cleanup(
            ITEM_ID, json_output=True, worktree_path=str(worktree)
        )

    assert first["removed"] is True
    assert first["already_absent"] is False
    assert first["removed_state"] is True

    assert second["success"] is True
    assert second["removed"] is False
    assert second["already_absent"] is True
    assert second["path"] == first["path"]
    assert second["removed_state"] is True
    for key in ("retained_dirty", "retained_non_terminal", "retained_no_worktree"):
        assert second[key] is False, key


# ---------------------------------------------------------------------------
# Safety gates — retained, reported, never forced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "stage"),
    [("open", "intake_complete"), ("in-progress", "plan_complete"), ("blocked", "")],
)
def test_non_terminal_item_is_retained(tmp_path, status, stage):
    """A non-terminal item's worktree is never removed."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree, status=status, stage=stage) as mocks:
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["success"] is True
    assert report["retained_non_terminal"] is True
    assert report["removed"] is False
    assert mocks["removal"].call_count == 0
    assert mocks["cleanup"].call_count == 0, "no process cleanup for a retained tree"
    assert str(worktree) in report["message"]
    assert "implement.py cleanup" in report["message"], "the remedy is actionable"
    assert worktree.is_dir(), "the tree survives a non-terminal cleanup"


def test_unreadable_status_retains_the_worktree(tmp_path):
    """An unreadable store fails closed: retain rather than guess terminality."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree) as mocks:
        mocks["wl_show"].return_value = {}
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["success"] is True
    assert report["retained_non_terminal"] is True
    assert report["removed"] is False
    assert mocks["removal"].call_count == 0
    assert "unknown" in report["reason"]


def test_dirty_worktree_is_retained(tmp_path):
    """Uncommitted work is never discarded — the tree is retained and reported."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree, dirty=True) as mocks:
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["success"] is True
    assert report["retained_dirty"] is True
    assert report["removed"] is False
    assert mocks["removal"].call_count == 0
    assert worktree.is_dir()
    assert "commit or discard" in report["message"].lower()
    assert f"git -C {worktree}" in report["message"]
    assert f"git worktree remove {worktree} && git worktree prune" in report["message"]


def test_main_checkout_path_is_never_removed(tmp_path):
    """A path that resolves to the main checkout is refused outright."""
    main_checkout = tmp_path / "main"
    (main_checkout / ".git").mkdir(parents=True)
    # A state file whose ``repo_root`` is the path itself (the stale-artifact
    # shape ``_resolve_main_repo_root`` re-derives) makes the path *the* main
    # checkout for this run.
    state = implement.ImplementState(
        work_item_id=ITEM_ID,
        worktree_path=str(main_checkout),
        repo_root=str(main_checkout),
        parent_branch="dev",
    )

    with _patched_cleanup(
        main_checkout, state=state, repo_root=str(main_checkout)
    ) as mocks:
        report = implement.phase_cleanup(
            ITEM_ID, json_output=True, worktree_path=str(main_checkout)
        )

    assert report["success"] is True
    assert report["retained_main_checkout"] is True
    assert report["removed"] is False
    assert mocks["removal"].call_count == 0
    assert mocks["cleanup"].call_count == 0
    assert main_checkout.is_dir()


def test_plain_directory_is_never_removed(tmp_path):
    """A directory that is not a linked worktree is refused, never deleted.

    ``_remove_worktree`` falls back to a manual ``shutil.rmtree`` when
    ``git worktree remove`` fails, so a plain directory must never reach it.
    """
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    (plain / implement.STATE_FILE_NAME).write_text("{}")
    (plain / "precious.txt").write_text("user data")

    with _patched_cleanup(plain) as mocks:
        report = implement.phase_cleanup(
            ITEM_ID, json_output=True, worktree_path=str(plain)
        )

    assert report["success"] is True
    assert report["retained_no_worktree"] is True
    assert report["removed"] is False
    assert mocks["removal"].call_count == 0
    assert mocks["cleanup"].call_count == 0
    assert (plain / "precious.txt").exists()


def test_worktree_of_another_item_is_never_removed(tmp_path):
    """A state file recording a different item never authorises a removal."""
    worktree = _worktree_fixture(tmp_path)
    other_state = _state(worktree, work_item_id="SA-SOMEONEELSE")

    with _patched_cleanup(worktree, state=other_state) as mocks:
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["success"] is True
    assert report["retained_other_item"] is True
    assert report["removed"] is False
    assert mocks["removal"].call_count == 0
    assert "SA-SOMEONEELSE" in report["message"]


def test_no_worktree_found_is_reported(tmp_path):
    """Nothing to resolve → ``retained_no_worktree`` success, no removal."""
    with _patched_cleanup(None) as mocks:
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["success"] is True
    assert report["retained_no_worktree"] is True
    assert report["removed"] is False
    assert report["path"] == ""
    assert mocks["removal"].call_count == 0
    assert "no worktree" in report["message"].lower()


# ---------------------------------------------------------------------------
# AC3 — consistency with the driven fix: one removal mechanism, same vocabulary
# ---------------------------------------------------------------------------


def test_removal_only_happens_through_the_patched_boundaries(tmp_path):
    """The action adds no deletion mechanism of its own.

    Both existing boundaries are mocked **without** deleting anything: if the
    action removed the tree behind their backs (``rm -rf``, its own
    ``shutil.rmtree``, a second ``git worktree remove``), the fixture would
    disappear. It survives, and the recorded calls show the removal was routed
    through them (AC3).
    """
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(
        worktree, remove_side_effect=lambda path, repo_root=None: True
    ) as mocks:
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["success"] is True
    assert report["removed"] is True
    assert mocks["cleanup"].call_count == 1
    assert mocks["removal"].call_count == 1
    assert worktree.is_dir(), (
        "a deletion outside cleanup_worktree_processes/_remove_worktree would "
        "have removed the tree"
    )


def test_retention_vocabulary_matches_the_driver(tmp_path):
    """The report reuses the driver's retention keys (one shared vocabulary)."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree):
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    driver_entry = implement._remove_finished_worktree(
        ITEM_ID, str(tmp_path / "missing"), repo_root=MAIN_REPO_ROOT
    )
    assert {"removed", "already_absent", "retained_dirty"} <= set(driver_entry)
    assert {"removed", "already_absent", "retained_dirty"} <= set(report)


def test_refused_removal_is_reported_not_raised(tmp_path):
    """A safety-gate refusal from ``_remove_worktree`` retains and reports."""
    worktree = _worktree_fixture(tmp_path)

    def _refuse(path: str, repo_root: str | None = None) -> bool:
        raise RuntimeError("REFUSE to manually remove: main working tree")

    with _patched_cleanup(worktree, remove_side_effect=_refuse):
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["success"] is True, "a refusal is a reported outcome"
    assert report["removed"] is False
    assert "refused" in report["reason"]
    assert report["remedy"]
    assert worktree.is_dir()


def test_cleanup_removes_under_the_deferral_switches(tmp_path, monkeypatch):
    """``cleanup`` is the *deferred* owner, so it removes under either switch.

    The deferral predicate governs ``finish`` only; the plain leaf session that
    opted in with ``IMPLEMENT_KEEP_WORKTREE=1`` must still be able to run the
    explicit cleanup (no third removal path, no conflicting predicate).
    """
    monkeypatch.setenv(implement.KEEP_WORKTREE_ENV, "1")
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree) as mocks:
        report = implement.phase_cleanup(ITEM_ID, json_output=True)

    assert report["removed"] is True
    assert mocks["removal"].call_count == 1


# ---------------------------------------------------------------------------
# CLI surface and in-code contract
# ---------------------------------------------------------------------------


def test_parse_args_accepts_cleanup():
    """``cleanup`` is a valid action and takes the worktree-path override."""
    args = implement.parse_args(["cleanup", ITEM_ID])
    assert args.action == "cleanup"
    assert args.work_item_id == ITEM_ID
    assert args.worktree_path is None
    assert args.json is False

    args = implement.parse_args(
        ["cleanup", ITEM_ID, "--json", "--worktree-path", "/tmp/wt"]
    )
    assert args.worktree_path == "/tmp/wt"
    assert args.json is True


def test_main_dispatches_cleanup():
    """``main`` runs the cleanup phase and exits 0 on a reported success."""
    report = {"phase": "cleanup", "success": True, "message": "ok", "removed": True}
    with patch.object(implement, "phase_cleanup", return_value=report) as phase:
        code = implement.main(["cleanup", ITEM_ID, "--json"])

    assert code == 0
    assert phase.call_count == 1
    assert phase.call_args.kwargs["work_item_id"] == ITEM_ID
    assert phase.call_args.kwargs["json_output"] is True


def test_main_cleanup_exits_non_zero_on_an_unexpected_failure(tmp_path):
    """A genuine failure exits non-zero (retained outcomes do not)."""
    report = {"phase": "cleanup", "success": False, "message": "boom"}
    with patch.object(implement, "phase_cleanup", return_value=report):
        assert implement.main(["cleanup", ITEM_ID, "--json"]) == 1


def test_help_documents_cleanup(capsys):
    """``--help`` lists the action so an operator can discover it."""
    with pytest.raises(SystemExit) as excinfo:
        implement.parse_args(["--help"])
    assert excinfo.value.code == 0
    # argparse wraps the help text, so compare on collapsed whitespace.
    out = " ".join(capsys.readouterr().out.split())
    assert "cleanup" in out
    assert "Workflow phase to execute" in out
    assert "last bash command" in out


def test_phase_cleanup_docstring_states_the_ordering_contract():
    """The phase docstring tells the caller it must be the *last* command."""
    doc = implement.phase_cleanup.__doc__ or ""
    lowered = doc.lower()
    assert "last" in lowered
    assert "bash" in lowered
    assert "cwd" in lowered


def test_human_output_names_the_path_and_the_remedy(tmp_path, capsys):
    """Non-JSON output is actionable: path plus the exact remedy command."""
    worktree = _worktree_fixture(tmp_path)

    with _patched_cleanup(worktree, dirty=True):
        implement.phase_cleanup(ITEM_ID, json_output=False)

    out = capsys.readouterr().out
    assert str(worktree) in out
    assert f"git worktree remove {worktree} && git worktree prune" in out
