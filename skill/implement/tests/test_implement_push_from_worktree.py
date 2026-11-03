"""Tracer-bullet regressions: the dev-push gate's subject is the pushed commit.

Parent **SA-0MVTFMB7K002Y1F3** — test-first slice (child **SA-0MVUTXILY003DVTE**).
The change under test is implemented by the two sibling slices
**SA-0MVUTXJ4300301ZN** (push from the worktree + fail-closed subject guard) and
**SA-0MVUTXJM8006PQY4** (gate evidence on success); every expectation below is
marked ``xfail(strict=True)`` until those land, and the fixing change set
removes the markers.

Why this matters
----------------
``.githooks/pre-push`` runs the full suite against the tree of the **directory
the push was launched from** (``git rev-parse --show-toplevel``), not against
the commit being pushed. ``implement.py finish`` used to push from the **main
checkout** while the commit lived on the feature branch in the **worktree**, so
the gate could verify a different tree than the one that landed on ``dev`` —
deterministically, because the cache is keyed by ``HEAD`` sha + dirty-content
hash. The non-fast-forward retry made it worse: the rebase rewrote the commit
*inside the worktree* and then retried the push from the main checkout, which
still held the pre-rebase commit.

Pre-fix baseline and current state
----------------------------------
The two **push-subject** expectations
(``test_first_push_runs_from_the_worktree_holding_the_commit``,
``test_mismatched_push_subject_is_refused_before_pushing``) were written as
``xfail(strict=True)`` baselines and are now **enforced**: the fixing change set
SA-0MVUTXJ4300301ZN pushes from the worktree and added the fail-closed guard, so
their markers are gone.

The two **gate-evidence** expectations
(``test_gate_evidence_reports_cached_full_suite_on_success``,
``test_gate_evidence_reports_executed_full_suite_when_the_gate_runs``) remain
``xfail(strict=True)`` until SA-0MVUTXJM8006PQY4 lands; that change set removes
their markers. Before that fix the success path logs nothing: the hook's stderr
is captured with ``subprocess.PIPE`` and only logged on failure, and no evidence
line names the resolved tree.

- ``test_fake_gate_stages_a_full_suite_entry_for_the_pushed_tree`` and
  ``test_finish_reaches_the_dev_push_with_the_recorded_subject`` are the
  **companions** that keep those baselines unambiguous: they pass before and
  after the fix, so a failing xfail test can only be failing on the behaviour
  under test (the preconditions — a real push, a real gate run, a reachable
  finish push — are proven separately, not smuggled into the baseline).

Hermetic by construction: a scratch bare ``origin`` plus one main checkout and
one linked worktree built with ``skill/shared/git_sandbox.py`` factories; the
``wl``/store, build and test boundaries of ``phase_finish`` are stubbed. No test
command runs, no wall-clock sleeps, and the classifications are driven by cache
entries staged in the fixture, never by timing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

_SKILL_ROOT = REPO_ROOT / "skill"
if str(_SKILL_ROOT) not in sys.path:
    sys.path.append(str(_SKILL_ROOT))

import test_cache
from shared import git_sandbox as gs

from skill.implement.scripts import implement

#: The logger ``implement.py`` writes to (a fixed name, independent of the
#: module import path).
LOG_NAME = "implement.scripts.implement"

#: Canonical branch name of the fixture worktree. ``phase_finish`` derives the
#: branch from the worktree's directory name, so the fixture names the
#: directory after the branch.
BRANCH = "wl-test-branch"

_REASON_EVIDENCE = (
    "pre-fix baseline: git_push_to_dev logs nothing on success and emits no "
    "resolved-tree evidence — fixed by SA-0MVUTXJM8006PQY4; remove this "
    "marker there"
)

gate_evidence_xfail = pytest.mark.xfail(
    strict=True, reason=_REASON_EVIDENCE, raises=AssertionError
)


# ---------------------------------------------------------------------------
# Hermetic fixture: main checkout + linked worktree holding a commit
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """A scratch ``origin``, a ``dev`` main checkout and a feature worktree.

    ``main`` is checked out on ``dev`` at :data:`base`; ``wl-test-branch`` is a
    linked worktree whose ``HEAD`` is :data:`feature` (a child of ``base``) and
    which carries a pytest-style suite so ``full_suite_commands`` resolves a
    real full-suite command for the cache/evidence assertions.
    """
    remote = gs.init_bare_remote(tmp_path / "origin.git")
    main = gs.init_repo(tmp_path / "main", default_branch="dev")
    (main / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (main / "tests").mkdir()
    (main / "tests" / "test_seed.py").write_text(
        "def test_seed():\n    assert True\n", encoding="utf-8"
    )
    base = gs.commit_all(main, "base commit")
    gs.add_remote(main, "origin", str(remote))
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)

    wt = gs.add_worktree(main, tmp_path / BRANCH, BRANCH)
    (wt / "feature.txt").write_text("feature\n", encoding="utf-8")
    feature = gs.commit_all(wt, "optional feature")

    return {
        "tmp": tmp_path,
        "remote": remote,
        "main": main,
        "wt": wt,
        "base": base,
        "feature": feature,
        "feature_short": gs.run_git(
            wt, ["rev-parse", "--short", "HEAD"], check=True
        ).stdout.strip(),
    }


def _rev(cwd: Path, ref: str = "HEAD") -> str:
    """Return the full commit hash for *ref* in *cwd*."""
    return gs.run_git(cwd, ["rev-parse", ref], check=True).stdout.strip()


def _names_commit(candidate: str, commit: str) -> bool:
    """Whether *candidate* and *commit* name the same commit (short or full)."""
    return bool(candidate) and bool(commit) and (
        candidate.startswith(commit) or commit.startswith(candidate)
    )


# ---------------------------------------------------------------------------
# phase_finish harness — external boundaries stubbed, real pick of push cwd
# ---------------------------------------------------------------------------


def _build_result() -> dict:
    return {"success": True, "stdout": "", "stderr": "", "exit_code": 0, "skipped": True}


def _tests_result() -> dict:
    return {
        "success": True,
        "failures": [],
        "skipped": False,
        "tooling": "suite",
        "scope": "changed",
        "notices": [],
    }


def _fake_run_cmd(cmd: list[str], *args, **kwargs) -> subprocess.CompletedProcess:
    """Every stubbed git sub-command "succeeds" without touching a repository,
    except ``rev-parse``.

    The push-subject guard (SA-0MVUTXJ4300301ZN) resolves the push cwd's
    ``HEAD`` and the pushed commit against the sandbox's real commit graph, so
    ``rev-parse`` must reach the real repository. Every command that *moves* a
    ref (commit, fast-forward, push, worktree removal) stays stubbed, so the
    fixture's own git state is untouched: the main checkout's ``HEAD`` stays at
    the pre-feature commit, exactly the shape that made the gate verify the
    wrong tree.
    """
    if (
        isinstance(cmd, (list, tuple))
        and len(cmd) >= 2
        and cmd[0] == "git"
        and cmd[1] == "rev-parse"
    ):
        try:
            return subprocess.run(
                list(cmd),
                cwd=kwargs.get("cwd"),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:  # pragma: no cover - best effort delegation
            pass
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


@contextlib.contextmanager
def _stubbed_finish(sandbox: dict, pushed_commit: str, pushes: list[tuple]):
    """Patch ``phase_finish``'s external boundaries and record its dev push.

    The push boundary records ``(cwd, branch, commit_hash)`` so a test can
    assert *which checkout* the dev-push gate would have measured. Everything
    that would mutate the sandbox (commit, fast-forward, ancestry assertion,
    worktree removal, parent-branch sync) or shell out to ``wl`` is stubbed.
    """
    main = sandbox["main"]
    wt = sandbox["wt"]
    state = implement.ImplementState(
        work_item_id="SA-TEST",
        worktree_path=str(wt),
        repo_root=str(main),
        parent_branch="dev",
    )

    def recording_push(cwd, branch, commit_hash=None, **kwargs):
        pushes.append((cwd, branch, commit_hash))
        return True

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(implement, "_discover_worktree", return_value=str(wt))
        )
        stack.enter_context(patch.object(implement, "read_state", return_value=state))
        stack.enter_context(
            patch.object(implement, "_worktree_placement_violation", return_value=None)
        )
        stack.enter_context(
            patch.object(implement, "run_build", return_value=_build_result())
        )
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
                return_value={"success": True, "tree": "abc1234", "expected": "abc1234"},
            )
        )
        stack.enter_context(
            patch.object(implement, "run_tests", return_value=_tests_result())
        )
        stack.enter_context(patch.object(implement, "git_commit", return_value=True))
        stack.enter_context(
            patch.object(
                implement, "git_get_commit_hash", return_value=pushed_commit
            )
        )
        stack.enter_context(patch.object(implement, "git_push_to_dev", recording_push))
        # Step 5 is main-checkout hygiene only (never the gate subject): model
        # a refused advance so the main checkout keeps its own, different HEAD.
        stack.enter_context(
            patch.object(
                implement,
                "_fast_forward_local_dev_for_push",
                return_value={
                    "method": "refused",
                    "success": True,
                    "advanced": False,
                    "commit": pushed_commit,
                    "reason": "local_dev_diverged",
                    "own_churn": [],
                },
            )
        )
        stack.enter_context(
            patch.object(
                implement,
                "_assert_commit_on_origin_dev",
                return_value={
                    "verified": True,
                    "resolved_commit": pushed_commit,
                    "target": "refs/remotes/origin/dev",
                    "target_tip": pushed_commit,
                },
            )
        )
        stack.enter_context(
            patch.object(implement, "run_cmd", side_effect=_fake_run_cmd)
        )
        stack.enter_context(
            patch.object(
                implement,
                "cleanup_worktree_processes",
                return_value={"method": "wl_cleanup_worktree", "terminated": 0, "warning": ""},
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


def _run_finish(sandbox: dict, pushed_commit: str) -> tuple[dict, list[tuple]]:
    """Drive ``phase_finish`` and return its report plus the recorded pushes."""
    pushes: list[tuple] = []
    with _stubbed_finish(sandbox, pushed_commit, pushes):
        report = implement.phase_finish(
            "SA-TEST", json_output=True, no_refactor=True
        )
    return report, pushes


# ---------------------------------------------------------------------------
# Companions — preconditions proven independently of the behaviour under test
# ---------------------------------------------------------------------------


def test_finish_reaches_the_dev_push_with_the_recorded_subject(sandbox):
    """``phase_finish`` reaches its dev push and reports success.

    Companion to the ``xfail`` baseline above: it proves the harness really
    drives finish through commit → push (status green, branch derived from the
    worktree) so the baseline's failure is the push *cwd*, not a fixture or
    harness problem. Deliberately silent about the cwd itself.
    """
    report, pushes = _run_finish(sandbox, sandbox["feature_short"])

    assert report["success"] is True, report["message"]
    assert pushes, "finish must push to dev"
    assert pushes[0][1] == BRANCH, pushes
    assert report["steps"]["push"]["hash"] == sandbox["feature_short"]


# ---------------------------------------------------------------------------
# AC2 — the FIRST dev push runs from the checkout holding the pushed commit
# ---------------------------------------------------------------------------


def test_first_push_runs_from_the_worktree_holding_the_commit(sandbox):
    """The first dev push's cwd is the worktree, whose HEAD is the pushed sha.

    Pre-fix baseline: ``phase_finish`` pushes from ``repo_root`` (the main
    checkout), whose ``HEAD`` is still the pre-feature commit — so the pre-push
    hook measures a tree the pushed commit does not describe.
    """
    report, pushes = _run_finish(sandbox, sandbox["feature_short"])

    assert report["success"] is True, report["message"]
    assert pushes, "finish must push to dev"
    cwd, branch, pushed = pushes[0]
    assert branch == BRANCH, pushes
    assert cwd == str(sandbox["wt"]), (
        "the dev push must run from the worktree that holds the pushed "
        f"commit; got cwd={cwd!r}"
    )
    assert _names_commit(_rev(Path(cwd)), pushed), (
        "the push cwd's HEAD must be the pushed commit so the pre-push "
        f"hook's project root is the pushed tree; HEAD={_rev(Path(cwd))} "
        f"pushed={pushed}"
    )


# ---------------------------------------------------------------------------
# AC3 — a HEAD != pushed-sha mismatch is refused, never silently gated
# ---------------------------------------------------------------------------


def test_mismatched_push_subject_is_refused_before_pushing(sandbox):
    """A pushed sha the push cwd does not hold is refused before any push.

    The worktree holds ``feature`` but finish is made to push ``base`` (the
    main checkout's commit) — the exact "checkout holding a different commit"
    shape. The subject guard must refuse: no push is attempted, and the
    fail-closed error names the commit, the branch, the worktree and the
    actual ``HEAD``.

    Pre-fix baseline: there is no subject guard, so the mismatched push is
    attempted (the recorder sees one call) and finish reports success.
    """
    wt = sandbox["wt"]
    report, pushes = _run_finish(sandbox, sandbox["base"])

    assert pushes == [], (
        "a push subject mismatch must be refused before the push; "
        f"recorded pushes: {pushes}"
    )
    assert report["success"] is False, report
    message = report["message"]
    assert str(wt) in message, message
    assert BRANCH in message, message
    for label, commit in (("pushed", sandbox["base"]), ("HEAD", sandbox["feature"])):
        assert any(
            candidate in message
            for candidate in (commit, commit[:12], commit[:7])
        ), f"the refusal must name the {label} commit {commit}: {message}"


# ---------------------------------------------------------------------------
# Shared evidence helpers — the dev-push gate's resolved tree
# ---------------------------------------------------------------------------


_EVIDENCE_RE = re.compile(
    r"dev-push gate evidence:.*?"
    r"root=(?P<root>\S+)\s+"
    r"head=(?P<head>[0-9a-f]+)\s+"
    r"pushed=(?P<pushed>[0-9a-f]+)\s+"
    r"full-suite=(?P<classification>[\w-]+)",
    re.DOTALL,
)


def _gate_evidence(log_text: str) -> dict:
    """Parse the single structured dev-push gate evidence line from *log_text*."""
    match = _EVIDENCE_RE.search(log_text)
    assert match is not None, (
        "git_push_to_dev must log one structured evidence line naming the "
        f"gate's resolved tree; log was:\n{log_text}"
    )
    return match.groupdict()


def _fake_runner(command, cwd, timeout) -> subprocess.CompletedProcess:
    """Stand-in suite runner: records a green full-suite entry, runs nothing."""
    return subprocess.CompletedProcess(args=[command], returncode=0, stdout="1 passed\n", stderr="")


def _full_suite_commands(sandbox: dict) -> list[str]:
    """The full-suite commands the production evidence path resolves."""
    commands = implement._resolve_full_suite_commands(str(sandbox["wt"]))
    assert commands, "the fixture worktree must resolve a full-suite command"
    return commands


def _seed_full_suite_cache(sandbox: dict, commands: list[str]) -> None:
    """Stage a green full-suite cache entry for the pushed tree (warm cache)."""
    for command in commands:
        entry = test_cache.run_cached(command, cwd=str(sandbox["wt"]), runner=_fake_runner)
        assert entry["cached"] is False, entry


def _install_fake_gate_hook(sandbox: dict, commands: list[str]) -> Path:
    """Install a ``pre-push`` hook that stages a cache entry, as the gate does.

    The hook is the sandbox's stand-in for ``.githooks/pre-push``: it writes a
    green ``scope: full`` entry for the pushed tree (what a real gate run does)
    and prints a marker to stderr so the pusher-side logging can be asserted.
    """
    git_dir = Path(
        gs.run_git(sandbox["main"], ["rev-parse", "--git-dir"], check=True).stdout.strip()
    )
    if not git_dir.is_absolute():
        git_dir = sandbox["main"] / git_dir
    hook = git_dir / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/usr/bin/env python3\n"
        '"""Fake dev-push gate: stage a full-suite entry and report to stderr."""\n'
        "import json\n"
        "import subprocess\n"
        "import sys\n"
        f"sys.path.insert(0, {str(_SKILL_ROOT)!r})\n"
        "import test_cache\n"
        "root = subprocess.run(\n"
        '    ["git", "rev-parse", "--show-toplevel"],\n'
        "    capture_output=True, text=True, check=True,\n"
        ").stdout.strip()\n"
        f"COMMANDS = json.loads({json.dumps(commands)!r})\n"
        "def _runner(command, cwd, timeout):\n"
        '    return subprocess.CompletedProcess([command], 0, "1 passed\\n", "")\n'
        "for command in COMMANDS:\n"
        "    test_cache.run_cached(command, cwd=root, runner=_runner)\n"
        'print(f"fake gate: full suite executed for {root}", file=sys.stderr)\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


# ---------------------------------------------------------------------------
# AC4 — gate evidence is surfaced on success, including executed-vs-cached
# ---------------------------------------------------------------------------


def test_fake_gate_stages_a_full_suite_entry_for_the_pushed_tree(sandbox):
    """A push from the worktree runs the hook and stages a full-suite entry.

    Companion to the classification tests: it proves the premises they rely
    on — the sandbox ``pre-push`` hook runs for a push from the linked
    worktree, and the entry it stages is keyed to the pushed tree so a
    read-only ``query_cached`` sees it afterwards (cold before, warm after).
    No timing is asserted, only cache state.
    """
    commands = _full_suite_commands(sandbox)
    _install_fake_gate_hook(sandbox, commands)
    assert test_cache.query_cached(commands[0], cwd=str(sandbox["wt"])) is None

    assert implement.git_push_to_dev(
        str(sandbox["wt"]), BRANCH, sandbox["feature_short"]
    ) is True

    assert test_cache.query_cached(commands[0], cwd=str(sandbox["wt"])) is not None, (
        "the hook's full-suite entry must be visible for the pushed tree"
    )
    assert _rev(sandbox["remote"], "dev") == sandbox["feature"]


@gate_evidence_xfail
def test_gate_evidence_reports_cached_full_suite_on_success(sandbox, caplog):
    """A warm cache yields ``full-suite=cached`` evidence naming the pushed tree.

    The cache entry is staged in the fixture (no wall-clock proxy): a valid
    full-suite entry already exists for the pushed ``HEAD`` before the push, so
    the gate would be served from cache rather than executing the suite.
    """
    commands = _full_suite_commands(sandbox)
    _seed_full_suite_cache(sandbox, commands)

    with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
        assert implement.git_push_to_dev(
            str(sandbox["wt"]), BRANCH, sandbox["feature_short"]
        ) is True

    evidence = _gate_evidence(caplog.text)
    assert evidence["root"] == str(sandbox["wt"]), evidence
    assert _names_commit(evidence["head"], sandbox["feature"]), evidence
    assert _names_commit(evidence["pushed"], sandbox["feature"]), evidence
    assert evidence["classification"] == "cached", evidence


@gate_evidence_xfail
def test_gate_evidence_reports_executed_full_suite_when_the_gate_runs(sandbox, caplog):
    """A cold cache plus a gate run yields ``full-suite=executed``.

    The sandbox ``pre-push`` hook executes the gate (staging the entry the real
    suite run would stage) and reports to stderr; the pusher must log that hook
    output on success — today it is captured and dropped — and classify the
    entry as ``executed`` because no valid entry existed before the push.
    """
    commands = _full_suite_commands(sandbox)
    _install_fake_gate_hook(sandbox, commands)
    assert test_cache.query_cached(commands[0], cwd=str(sandbox["wt"])) is None, (
        "the cache must be cold before the push for this classification"
    )

    with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
        assert implement.git_push_to_dev(
            str(sandbox["wt"]), BRANCH, sandbox["feature_short"]
        ) is True

    assert "fake gate: full suite executed" in caplog.text, (
        "the pre-push hook's stderr must be logged on success too; "
        f"log was:\n{caplog.text}"
    )
    evidence = _gate_evidence(caplog.text)
    assert evidence["root"] == str(sandbox["wt"]), evidence
    assert _names_commit(evidence["head"], sandbox["feature"]), evidence
    assert _names_commit(evidence["pushed"], sandbox["feature"]), evidence
    assert evidence["classification"] == "executed", evidence
    # The gate really staged the full-suite entry the classification reports.
    assert test_cache.query_cached(commands[0], cwd=str(sandbox["wt"])) is not None
