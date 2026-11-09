
# <!-- REFACTOR-SA-0MW58A4S8003W36C
# smell: simplification
# severity: medium
# description: Use a single `with` statement with multiple contexts instead of nested `with` statements
# -->
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
All four ``xfail(strict=True)`` baselines are now **enforced** and their
markers are gone:

- the two **push-subject** expectations (first-push cwd, subject-mismatch
  refusal) — enforced by SA-0MVUTXJ4300301ZN (worktree push + fail-closed
  guard);
- the two **gate-evidence** expectations (``cached`` / ``executed``) — enforced
  by SA-0MVUTXJM8006PQY4 (success-path hook logging + a structured evidence
  line carrying the executed-vs-cached classification), plus
  ``test_gate_evidence_reports_no_evidence_when_the_gate_produced_none`` for
  the third classification.

- ``test_fake_gate_stages_a_full_suite_entry_for_the_pushed_tree`` and
  ``test_finish_reaches_the_dev_push_with_the_recorded_subject`` are the
  **companions** that keep those baselines unambiguous: they pass before and
  after the fix, so a failing test can only be failing on the behaviour
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
        # Mirror the real helper's ``evidence_out`` contract so the machine-
        # readable wiring (AC5) can be asserted through ``phase_finish``.
        evidence_out = kwargs.get("evidence_out")
        if evidence_out is not None:
            evidence_out.update(
                {
                    "root": str(cwd),
                    "head": sandbox["feature"],
                    "pushed": sandbox["feature"],
                    "full_suite": "cached",
                }
            )
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
        # The read-only post-push verification (SA-0MVXUSJ2P009T97H) resolves
        # the pushed tree's full-suite verdict from the test cache; this
        # harness stubs the pre-push gate's cached green entry. Its own
        # verdict/refusal semantics live in
        # ``test_finish_finalisation_ordering.py``.
        stack.enter_context(
            patch.object(
                implement,
                "query_cached",
                return_value={
                    "stdout": "1 passed",
                    "stderr": "",
                    "exit_code": 0,
                    "completed_at": 1.0,
                    "command": implement.PYTEST_CMD,
                    "git_state": "stub-git-state",
                    "fingerprint_kind": "worktree",
                    "scope": "full",
                    "test_type": "full",
                    "cached": True,
                },
            )
        )
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

    It also pins AC5: the gate evidence the push helper reports is exposed in
    ``report["steps"]["push"]["gate_evidence"]`` for ``--json`` consumers.
    """
    report, pushes = _run_finish(sandbox, sandbox["feature_short"])

    assert report["success"] is True, report["message"]
    assert pushes, "finish must push to dev"
    assert pushes[0][1] == BRANCH, pushes
    assert report["steps"]["push"]["hash"] == sandbox["feature_short"]
    evidence = report["steps"]["push"]["gate_evidence"]
    assert evidence["full_suite"] == "cached", evidence
    assert evidence["root"] == str(sandbox["wt"]), evidence


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
# AC2 — a dirty push subject (tracked changes) is refused, never gated
# ---------------------------------------------------------------------------


def test_dirty_push_subject_is_refused_before_pushing(sandbox):
    """A worktree with tracked uncommitted changes is refused before any push.

    A recovery rebase cannot safely carry tracked changes, so the subject guard
    refuses rather than let the gate measure (and cache) a tree the pushed
    commit does not describe. Here ``HEAD`` **is** the pushed commit, so the
    dirty state is the deciding fact, and the refusal still names the commit,
    branch, worktree and the actual ``HEAD``.
    """
    wt = sandbox["wt"]
    # A tracked modification: the worktree no longer describes the pushed
    # commit's tree, and a rebase cannot proceed with it in place.
    (wt / "feature.txt").write_text("dirty edit\n", encoding="utf-8")

    with pytest.raises(implement.PushSubjectMismatchError) as excinfo:
        implement._assert_push_subject(
            str(wt), sandbox["feature"], BRANCH, str(wt)
        )

    err = excinfo.value
    assert err.actual_head == sandbox["feature"], err.actual_head
    message = str(err)
    assert str(wt) in message, message
    assert BRANCH in message, message
    assert "uncommitted" in message, message


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


def test_gate_evidence_reports_no_evidence_when_the_gate_produced_none(sandbox, caplog):
    """A push with no gate run and no cache entry yields ``no-evidence`` (warned).

    The sandbox installs no ``pre-push`` hook, so nothing stages a full-suite
    entry: the push still succeeds (failing the push is not this helper's job),
    but the evidence line must say so and warn — never report a silent green.
    """
    commands = _full_suite_commands(sandbox)
    assert test_cache.query_cached(commands[0], cwd=str(sandbox["wt"])) is None, (
        "the cache must be cold for the no-evidence classification"
    )

    with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
        assert implement.git_push_to_dev(
            str(sandbox["wt"]), BRANCH, sandbox["feature_short"]
        ) is True

    evidence = _gate_evidence(caplog.text)
    assert evidence["root"] == str(sandbox["wt"]), evidence
    assert _names_commit(evidence["head"], sandbox["feature"]), evidence
    assert _names_commit(evidence["pushed"], sandbox["feature"]), evidence
    assert evidence["classification"] == "no-evidence", evidence
    assert "no full-suite cache entry" in caplog.text, caplog.text
    # Nothing was fabricated into the cache by the evidence path either.
    assert test_cache.query_cached(commands[0], cwd=str(sandbox["wt"])) is None


# ---------------------------------------------------------------------------
# AC2 (SA-0MVSJX5HS002DJHI) — a gate infrastructure refusal is reported with
# its reason and a remedy, not as a generic "push failed"
# ---------------------------------------------------------------------------


def test_push_gate_infrastructure_reason_is_reported_and_commented(sandbox):
    """``phase_finish`` reports the hook's ``gate_reason`` and posts a remedy.

    A transient infrastructure refusal (no free test slot) is not a code
    failure: the finish report's ``steps.push.reason`` must name it and the
    work-item comment must carry the matching remediation. Pre-fix: the push
    path reported ``reason: push_failed`` with no cause.
    """
    comments: list[str] = []

    def failing_push(cwd, branch, commit_hash=None, **kwargs):
        evidence = kwargs.get("evidence_out")
        if evidence is not None:
            evidence.update(
                {
                    "success": False,
                    "gate_reason": "slot_contention",
                    "stderr_tail": (
                        "gate_reason=slot_contention\n"
                        "ERROR: No free test slot — the full-suite gate never "
                        "started"
                    ),
                }
            )
        return False

    pushes: list[tuple] = []
    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=failing_push),
        patch.object(
            implement,
            "wl_add_comment",
            side_effect=lambda *args, **kwargs: comments.append(args[1]) or True,
        ),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is False, report
    assert report["steps"]["push"]["reason"] == "slot_contention", report["steps"]["push"]
    assert any(
        "slot_contention" in comment and "TEST_LOCK_TIMEOUT" in comment
        for comment in comments
    ), f"the work-item comment must name the reason and remedy; got {comments}"


def test_phase_finish_exposes_the_retry_trail_on_a_gate_refusal(sandbox):
    """AC3: the transport retry trail reaches ``report["steps"]["push"]``.

    A gate refusal after a transient transport retry must expose the attempt
    count and the trail (attempt, backoff, reason) to a ``--json`` consumer —
    not only to the log (SA-0MVXEQEI500641SJ AC3).
    """
    trail = [
        {"attempt": 1, "delay_seconds": 2.1, "reason": "transport_close"},
    ]

    def failing_push(cwd, branch, commit_hash=None, **kwargs):
        evidence = kwargs.get("evidence_out")
        if evidence is not None:
            evidence.update(
                {
                    "success": False,
                    "gate_reason": "slot_contention",
                    "stderr_tail": "gate_reason=slot_contention",
                    "attempts": 2,
                    "retry_trail": trail,
                }
            )
        return False

    pushes: list[tuple] = []
    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=failing_push),
        patch.object(implement, "wl_add_comment", return_value=True),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    push_step = report["steps"]["push"]
    assert push_step["reason"] == "slot_contention", push_step
    assert push_step["attempts"] == 2, push_step
    assert push_step["retry_trail"] == trail, push_step


def test_phase_finish_exposes_the_retry_trail_on_a_successful_push(sandbox):
    """AC3: a push that succeeded after a retry also reports its trail."""
    trail = [
        {"attempt": 1, "delay_seconds": 2.0, "reason": "transport_close"},
        {"attempt": 2, "delay_seconds": 4.0, "reason": "transport_close"},
    ]

    def recovering_push(cwd, branch, commit_hash=None, **kwargs):
        evidence = kwargs.get("evidence_out")
        if evidence is not None:
            evidence.update(
                {
                    "root": str(cwd),
                    "head": sandbox["feature"],
                    "pushed": sandbox["feature"],
                    "full_suite": "cached",
                    "attempts": 3,
                    "retry_trail": trail,
                }
            )
        return True

    pushes: list[tuple] = []
    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(implement, "git_push_to_dev", side_effect=recovering_push),
    ):
        report = implement.phase_finish("SA-TEST", json_output=True, no_refactor=True)

    assert report["success"] is True, report["message"]
    push_step = report["steps"]["push"]
    assert push_step["attempts"] == 3, push_step
    assert push_step["retry_trail"] == trail, push_step
    assert push_step["gate_evidence"]["full_suite"] == "cached", push_step


# ---------------------------------------------------------------------------
# SA-0MVWNHLQQ002TFZR — a lost SSH transport is reported distinctly from a
# red suite, so "full suite green" is never mistaken for a successful push
# ---------------------------------------------------------------------------


def test_phase_finish_reports_a_lost_transport_distinctly(sandbox):
    """AC3: an exhausted transport close reports ``connection_closed``.

    The suite may have been green; the ref update never reached origin. The
    finish report must say so *distinctly* (``reason: connection_closed``, not
    ``test_failure``), the work-item comment must name the transport — never
    imply a successful push — and the commit stays local on its branch.
    """
    comments: list[str] = []

    def transport_lost_push(cwd, branch, commit_hash=None, **kwargs):
        evidence = kwargs.get("evidence_out")
        if evidence is not None:
            evidence.update(
                {
                    "success": False,
                    "reason": "connection_closed",
                    "gate_reason": None,
                    "transport_lost": True,
                    "stderr_tail": "Connection to github.com closed by remote host.",
                    "attempts": 3,
                    "retry_trail": [],
                }
            )
        raise implement.PushTransportLostError(
            commit_hash or "unknown",
            branch,
            "Connection to github.com closed by remote host.",
            3,
        )

    pushes: list[tuple] = []
    with (
        _stubbed_finish(sandbox, sandbox["feature_short"], pushes),
        patch.object(
            implement, "git_push_to_dev", side_effect=transport_lost_push
        ),
        patch.object(
            implement,
            "wl_add_comment",
            side_effect=lambda *args, **kwargs: comments.append(args[1]) or True,
        ),
    ):
        report = implement.phase_finish(
            "SA-TEST", json_output=True, no_refactor=True
        )

    assert report["success"] is False, report
    push_step = report["steps"]["push"]
    assert push_step["reason"] == "connection_closed", push_step
    assert push_step["transport_lost"] is True, push_step
    assert push_step["reason"] != "test_failure", (
        "a transport loss must not be reported as a red suite"
    )
    assert push_step["attempts"] == 3, push_step

    assert comments, "the refusal must be recorded on the work item"
    combined = "\n".join(comments)
    assert "transport lost" in combined.lower(), combined
    assert "NOT a test failure" in combined, combined
    assert "full suite green" not in combined.lower(), (
        "the comment must never imply the push succeeded: " + combined
    )
    # The push failed before local ``dev`` was advanced in this stubbed
    # fixture, so the restore records nothing to undo; the commit survives on
    # its feature branch for recovery.
    restore = report["steps"]["local_dev_restore"]
    assert restore["success"] is True, restore
    assert restore["target"] == "refs/remotes/origin/dev", restore
    assert "git push to dev transport lost" in report["message"], report["message"]


# ---------------------------------------------------------------------------
# SA-0MW3H07A8004Q54C — the pushed-commit guard refusal is distinguishable,
# and the recovery rebase retries the local-dev fast-forward
# ---------------------------------------------------------------------------


def _hooks_dir(sandbox: dict) -> Path:
    """The shared hooks directory for the sandbox's main repo and worktree."""
    common = gs.run_git(
        sandbox["main"], ["rev-parse", "--git-common-dir"], check=True
    ).stdout.strip()
    path = Path(common)
    if not path.is_absolute():
        path = sandbox["main"] / path
    return path / "hooks"


def _install_guard_refusal_hook(sandbox: dict, block: str) -> Path:
    """Install a pre-push hook that prints *block* on stderr and refuses."""
    hooks = _hooks_dir(sandbox)
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stderr.write({block!r})\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def _install_recording_guard_hook(sandbox: dict, marker: Path) -> Path:
    """Install a guard-shaped pre-push hook that records what it measured.

    It mirrors the pushed-commit guard's contract: for the gated ref it records
    ``<local_sha> <gate_head> <gate_tree> <dirty>`` and refuses unless the
    pushed sha *is* the gate tree's HEAD and the tree is clean.
    """
    hooks = _hooks_dir(sandbox)
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "import sys\n"
        f"MARKER = {str(marker)!r}\n"
        "def _git(*args):\n"
        "    return subprocess.run(\n"
        "        ['git', *args], capture_output=True, text=True\n"
        "    ).stdout.strip()\n"
        "head = _git('rev-parse', 'HEAD')\n"
        "root = _git('rev-parse', '--show-toplevel')\n"
        "dirty = _git('status', '--porcelain')\n"
        "for line in sys.stdin:\n"
        "    parts = line.split()\n"
        "    if len(parts) < 4:\n"
        "        continue\n"
        "    local_sha, remote_ref = parts[1], parts[2]\n"
        "    if remote_ref != 'refs/heads/dev':\n"
        "        continue\n"
        "    with open(MARKER, 'w', encoding='utf-8') as fh:\n"
        "        fh.write('%s %s %s %s\\n' % (local_sha, head, root, dirty))\n"
        "    if local_sha != head:\n"
        "        sys.stderr.write(\n"
        "            'ERROR: pre-push gate refuses this push: the tree it '\n"
        "            'would measure is not the pushed commit.\\n'\n"
        "        )\n"
        "        sys.exit(1)\n"
        "    if dirty:\n"
        "        sys.stderr.write(\n"
        "            'ERROR: pre-push gate refuses this push: the gate tree '\n"
        "            'is dirty at the pushed commit.\\n'\n"
        "        )\n"
        "        sys.exit(1)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def test_parser_ignores_non_guard_failures():
    """Only the guard's own diagnostic is a pushed-commit refusal."""
    assert implement._parse_pushed_commit_guard_refusal("") is None
    assert (
        implement._parse_pushed_commit_guard_refusal(
            "fatal: Authentication failed for 'origin'"
        )
        is None
    )


def test_parser_handles_the_dirty_tree_variant():
    """The dirty-tree refusal names the pushed sha and the gate tree."""
    details = implement._parse_pushed_commit_guard_refusal(
        "ERROR: pre-push gate refuses this push: the gate tree is dirty at "
        "the pushed commit.\n"
        "  pushed commit: abc1234\n"
        "  gate tree:     /tmp/main\n"
    )
    assert details == {"pushed_sha": "abc1234", "gate_tree": "/tmp/main"}


def test_pushed_commit_guard_refusal_is_classified_with_its_details(sandbox):
    """A guard refusal is reported distinctly, carrying sha / HEAD / tree.

    Pre-fix: the guard emits no ``gate_reason`` line, so the push was reported
    as a generic failure and the operator had to read the raw stderr.
    """
    block = (
        "ERROR: pre-push gate refuses this push: the tree it would measure is "
        "not the pushed commit.\n"
        "  pushed commit  (refs/heads/dev): 50167e00\n"
        "  checked-out HEAD:            2016e5236\n"
        "  gate tree:                   /home/rgardler/projects/llm\n"
        "  A full-suite run here would verify a different tree.\n"
    )
    _install_guard_refusal_hook(sandbox, block)

    evidence: dict = {}
    assert (
        implement.git_push_to_dev(
            str(sandbox["wt"]),
            BRANCH,
            sandbox["feature_short"],
            evidence_out=evidence,
        )
        is False
    )
    assert evidence["gate_reason"] == "refused_by_pushed_commit_guard", evidence
    assert evidence["pushed_commit_guard"] == {
        "pushed_sha": "50167e00",
        "gate_head": "2016e5236",
        "gate_tree": "/home/rgardler/projects/llm",
    }, evidence


def test_phase_finish_reports_refused_by_pushed_commit_guard(sandbox):
    """``phase_finish`` surfaces the guard refusal as a distinct push step."""
    comments: list[str] = []
    guard = {
        "pushed_sha": "50167e00",
        "gate_head": "2016e5236",
        "gate_tree": "/home/rgardler/projects/llm",
    }

    def guard_refusing_push(cwd, branch, commit_hash=None, **kwargs):
        evidence = kwargs.get("evidence_out")
        if evidence is not None:
            evidence.update(
                {
                    "success": False,
                    "gate_reason": "refused_by_pushed_commit_guard",
                    "stderr_tail": (
                        "ERROR: pre-push gate refuses this push: the tree it "
                        "would measure is not the pushed commit."
                    ),
                    "pushed_commit_guard": dict(guard),
                }
            )
        return False

    pushes: list[tuple] = []
    with _stubbed_finish(sandbox, sandbox["feature_short"], pushes):
        with patch.object(implement, "git_push_to_dev", side_effect=guard_refusing_push):
            with patch.object(
                implement,
                "wl_add_comment",
                side_effect=lambda *args, **kwargs: comments.append(args[1]) or True,
            ):
                report = implement.phase_finish(
                    "SA-TEST", json_output=True, no_refactor=True
                )

    assert report["success"] is False, report
    push = report["steps"]["push"]
    assert push["reason"] == "refused_by_pushed_commit_guard", push
    assert push["refused_by_pushed_commit_guard"] == guard, push
    assert any(
        "refused_by_pushed_commit_guard" in comment and "worktree" in comment.lower()
        for comment in comments
    ), f"the remedy must name the worktree push; got {comments}"


def test_recover_retries_the_fast_forward_with_the_rebased_commit(sandbox):
    """After the recovery rebase, the fast-forward is retried for the new tip."""
    recorded: list[tuple] = []
    advanced = {
        "method": "advanced",
        "success": True,
        "advanced": True,
        "commit": "rebased123",
        "advanced_to": "rebased123",
        "own_churn": ["feature.txt"],
    }

    def fake_ff(repo_root, commit_hash, parent_branch="dev"):
        recorded.append((repo_root, commit_hash, parent_branch))
        return dict(advanced)

    ff_retry: dict = {}
    run_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(implement, "run_cmd", return_value=run_ok):
        with patch.object(implement, "git_get_commit_hash", return_value="rebased123"):
            with patch.object(
                implement, "_fast_forward_local_dev_for_push", side_effect=fake_ff
            ):
                with patch.object(implement, "_assert_push_subject"):
                    with patch.object(implement, "git_push_to_dev", return_value=True):
                        with patch.object(implement, "wl_add_comment", return_value=True):
                            new_commit = implement._recover_non_fast_forward_push(
                                worktree_path=str(sandbox["wt"]),
                                repo_root=str(sandbox["main"]),
                                branch=BRANCH,
                                commit_hash=sandbox["feature_short"],
                                exc=implement.PushNonFastForwardError(
                                    sandbox["feature_short"],
                                    BRANCH,
                                    "non-fast-forward",
                                    "git rebase origin/dev",
                                ),
                                work_item_id="SA-TEST",
                                parent_branch="dev",
                                ff_retry_out=ff_retry,
                            )

    assert new_commit == "rebased123"
    assert recorded == [(str(sandbox["main"]), "rebased123", "dev")], recorded
    assert ff_retry == advanced, ff_retry


def test_phase_finish_records_the_fast_forward_retry(sandbox):
    """The recovery retry is recorded in the finish report."""
    advanced = {
        "method": "advanced",
        "success": True,
        "advanced": True,
        "commit": sandbox["feature_short"],
        "advanced_to": sandbox["feature_short"],
        "own_churn": ["feature.txt"],
    }
    calls = {"n": 0}

    def push_raises_once(cwd, branch, commit_hash=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise implement.PushNonFastForwardError(
                commit_hash or "x", branch, "non-fast-forward", "git rebase origin/dev"
            )
        return True

    def fake_recover(**kwargs):
        out = kwargs.get("ff_retry_out")
        if out is not None:
            out.update(advanced)
        return kwargs["commit_hash"]

    pushes: list[tuple] = []
    with _stubbed_finish(sandbox, sandbox["feature_short"], pushes):
        with patch.object(implement, "git_push_to_dev", side_effect=push_raises_once):
            with patch.object(
                implement, "_recover_non_fast_forward_push", side_effect=fake_recover
            ):
                report = implement.phase_finish(
                    "SA-TEST", json_output=True, no_refactor=True
                )

    assert report["success"] is True, report["message"]
    recorded = report["steps"]["local_dev_fast_forward"]
    assert recorded.get("retry") == advanced, recorded


def test_concurrent_move_recovery_pushes_the_clean_worktree_tree(sandbox):
    """AC1/AC3: origin/dev moves, recovery rebases, retries the ff, and the
    guard measures the clean worktree at the pushed commit."""
    main = sandbox["main"]
    wt = sandbox["wt"]
    remote = sandbox["remote"]

    # A sibling lands on origin/dev after the feature worktree forked from dev.
    (main / "sibling.txt").write_text("sibling\n", encoding="utf-8")
    sibling = gs.commit_all(main, "sibling commit")
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)
    assert _rev(remote, "dev") == sibling

    marker = Path(sandbox["tmp"]) / "guard-push-observed.txt"
    _install_recording_guard_hook(sandbox, marker)

    ff_retry: dict = {}
    with patch.object(implement, "wl_add_comment", return_value=True):
        new_commit = implement._recover_non_fast_forward_push(
            worktree_path=str(wt),
            repo_root=str(main),
            branch=BRANCH,
            commit_hash=sandbox["feature_short"],
            exc=implement.PushNonFastForwardError(
                sandbox["feature_short"],
                BRANCH,
                "non-fast-forward",
                "git rebase origin/dev",
            ),
            work_item_id="SA-TEST",
            parent_branch="dev",
            ff_retry_out=ff_retry,
        )

    # The rebased commit landed on origin/dev and the worktree holds it.
    assert _names_commit(_rev(remote, "dev"), new_commit), new_commit
    assert _names_commit(_rev(wt), new_commit), new_commit

    # AC3: at push time the guard measured the pushed commit, on a clean tree.
    observed = marker.read_text(encoding="utf-8").split()
    assert len(observed) >= 3, observed
    assert _names_commit(observed[0], new_commit), observed
    assert observed[1] == observed[0], (
        "the gate tree HEAD must be the pushed sha at push time; "
        f"observed local_sha={observed[0]} gate_head={observed[1]}"
    )
    assert gs.run_git(wt, ["status", "--porcelain"]).stdout.strip() == "", (
        "the gate tree must be clean at push time"
    )
    assert Path(observed[2]).resolve() == wt.resolve(), observed

    # AC1: the retried fast-forward advanced the main checkout's dev.
    assert ff_retry.get("method") == "advanced", ff_retry
    assert _names_commit(ff_retry.get("advanced_to") or "", new_commit), ff_retry
    assert _names_commit(_rev(main), new_commit), new_commit
