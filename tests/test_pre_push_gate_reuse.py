"""Cross-consumer reuse of a tree-keyed full-suite verdict by the dev-push gate.

Proof that a green full-suite verdict recorded for tree *T* by a tree-keyed
caller (the finish gate's ``run_cached(..., fingerprint=T)`` path) **is**
served to the pre-push gate for the same *T* (parent SA-0MVXUCGPK003G2KV
AC3/AC7). This started life as the F2 baseline (SA-0MW7C6A9R000PD98) that
asserted the opposite — the gate then keyed on the resolved checkout's loose
worktree state (``GATE_SUBJECT_PREFIX`` hard-coded
``fingerprint_kind=worktree``), so the push executed the identical suite a
second time. F4 (SA-0MW7C6BMZ00931JG) keys the gate on the pushed commit's
tree; the baseline assertion is **flipped, not deleted**.

The fixture is hermetic (temp git repo, fake suite runner, isolated semaphore
namespace). A same-tree run is a hit; a different tree *T′* stays a miss.

``executed`` vs ``cached`` is driven by staged cache entries and fake runners
(never wall-clock timing), following
``skill/shared/test-writing-guidelines.md``.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test.scripts.run_tests as rt
import test_cache as tc
from shared import git_sandbox as gs

# The fake suite invocation's output. Green: a real gate would pass with it.
SUMMARY_OUTPUT = (
    "Test Files  3 passed (3)\n"
    "Tests  12 passed (12)\n"
    "Duration  1.5s\n"
)


def git(repo: Path, *args: str) -> None:
    gs.run_git(repo, args, check=True)


def tree_of(repo: Path, sha: str) -> str:
    """Return the 40-char tree sha of *sha* (``git rev-parse <sha>^{tree}``).

    This is the fingerprint ``run_cached(..., fingerprint=…)`` keys the
    verdict on — the would-be-commit tree later consumers bind to.
    """
    proc = gs.run_git(repo, ["rev-parse", f"{sha}^{{tree}}"], check=True)
    return proc.stdout.strip()


def recording_runner(calls: list[str], output: str = SUMMARY_OUTPUT):
    """Return a cache-protocol runner that records each invocation in *calls*."""

    def runner(command: str, cwd: str | Path, timeout: int) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(stdout=output, stderr="", returncode=0)

    return runner


@pytest.fixture
def gate_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo whose pushed commit *B* (tree ``T``) is HEAD.

    ``dev`` and ``feature`` hold commits *A* (tree ``T′``) and *B* (tree
    ``T``); the working tree is clean, so the gate resolves the pushed commit
    **in place** — the same checkout (and therefore the same per-repo cache
    dir) where the tree-keyed verdict is recorded. This isolates the defect
    to the gate's key, never to which directory a verdict landed in.
    """
    repo = gs.init_repo(tmp_path / "repo")
    (repo / "file.txt").write_text("one\n")
    gs.commit_all(repo, "A")  # dev -> A (tree T')
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("two\n")
    gs.commit_all(repo, "B")  # feature -> B (tree T)
    git(repo, "checkout", "-q", "feature")  # HEAD == pushed commit B, clean

    monkeypatch.setattr(rt, "REPO_ROOT", repo)
    # Isolate the semaphore namespace: this suite's outer run already holds a
    # host-wide slot, so nested acquisitions must not contend for it
    # (SA-0MUY6U05F008RG86).
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(rt.nested_semaphore_dir(tmp_path)))
    return repo


@pytest.fixture
def pushed_sha(gate_repo: Path) -> str:
    """The pushed commit *B* — the one the dev-push gate gates on."""
    return gs.run_git(gate_repo, ["rev-parse", "feature"], check=True).stdout.strip()


@pytest.fixture
def other_sha(gate_repo: Path) -> str:
    """A different commit *A* in the same repo — a different tree *T′*."""
    return gs.run_git(gate_repo, ["rev-parse", "dev"], check=True).stdout.strip()


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Replace the gate's real suite spawn with a recording fake (green)."""
    calls: list[dict[str, str]] = []

    def fake(cmd: list[str], **kwargs):
        calls.append({"cmd": " ".join(cmd), "cwd": str(kwargs.get("cwd"))})
        return SimpleNamespace(returncode=0, stdout=SUMMARY_OUTPUT, stderr="")

    monkeypatch.setattr(rt, "_run_cmd", fake)
    return calls


def record_tree_verdict(repo: Path, tree: str) -> dict[str, object]:
    """Record a green full-suite verdict for *tree* through the tree-keyed path.

    Exactly what the finish gate does (``implement.py finish`` calls
    ``run_cached(..., fingerprint=<tree sha>)``): the verdict lands in the
    ``fingerprint_kind="tree"`` namespace keyed on the tree SHA.
    """
    calls: list[str] = []
    result = tc.run_cached(
        rt.pytest_command(),
        cwd=str(repo),
        fingerprint=tree,
        scope="full",
        runner=recording_runner(calls),
    )
    assert result["cached"] is False
    assert result["fingerprint_kind"] == tc.FINGERPRINT_KIND_TREE
    assert result["git_state"] == tree
    assert calls, "the tree-keyed caller must have executed to produce the verdict"
    return result


def run_gate(
    capsys: pytest.CaptureFixture,
    repo: Path,
    sha: str,
) -> tuple[int, dict, str]:
    """Run the full-suite gate against *sha*; return (code, json, stderr)."""
    argv = [
        "--suite",
        "pytest",
        "--scope",
        "full",
        "--json",
        "--project-root",
        str(repo),
        "--gate-commit",
        sha,
    ]
    code = rt.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip().startswith("{") else {}
    return code, payload, captured.err


# ── The duplicate-suite vector, resolved (AC2/AC3) ───────────────────────────


def test_same_tree_keyed_verdict_is_served_to_the_gate(
    gate_repo: Path,
    pushed_sha: str,
    executed: list[dict[str, str]],
    capsys: pytest.CaptureFixture,
) -> None:
    """A green tree-keyed verdict for *T* IS served to the gate for the same *T*.

    ── flipped F2 baseline (SA-0MW7C6A9R000PD98) ──
    The gate now keys on the pushed commit's tree SHA
    (``fingerprint_kind="tree"``), so the verdict the finish gate's
    ``run_cached(..., fingerprint=T)`` path recorded for *T* is reused for the
    very same *T*: the push does not pay for the suite twice. (Before F4 this
    assertion held the opposite: ``executed`` was non-empty because the gate
    keyed on the loose worktree state.)
    """
    pushed_tree = tree_of(gate_repo, pushed_sha)
    record_tree_verdict(gate_repo, pushed_tree)

    code, payload, stderr = run_gate(capsys, gate_repo, pushed_sha)

    assert code == 0, stderr
    assert not executed, (
        "the gate must serve the tree-keyed verdict recorded above — a second "
        "suite run for the same tree is the duplicate-suite defect "
        "(SA-0MW7C6A9R000PD98) this item fixes."
    )
    assert payload.get("cached") is True
    assert payload["gate"]["cache_hit"] is True
    assert payload["gate"]["tree"] == pushed_tree
    served = tc.lookup(
        rt.pytest_command(),
        pushed_tree,
        cwd=str(gate_repo),
        fingerprint_kind=tc.FINGERPRINT_KIND_TREE,
    )
    assert served is not None, "the tree-keyed verdict must be present"
    assert payload["cache_entries"][0]["fingerprint_kind"] == tc.FINGERPRINT_KIND_TREE
    assert payload["cache_entries"][0]["git_state"] == pushed_tree
    verdict = [
        line
        for line in stderr.splitlines()
        if line.startswith("run_tests: gate-verdict")
    ]
    assert verdict and "verdict=cached" in verdict[0]
    assert "cache_hit=true" in verdict[0]
    assert f"tree={pushed_tree}" in verdict[0]


# ── AC5: the recording side works — the defect is the gate's key ─────────────


def test_tree_keyed_same_tree_hit_at_the_cache_layer(
    gate_repo: Path,
    pushed_sha: str,
) -> None:
    """A same-tree hit is served at the cache layer (the write is not broken).

    This pins AC5: the baseline failure above is attributable to the gate's
    look-up key, not to a broken cache write. A second tree-keyed caller with
    the same *T* is served without executing; a different tree *T′* is a miss.
    Both assertions hold today and after F4 — F4 does not touch this test.
    """
    repo = gate_repo
    pushed_tree = tree_of(repo, pushed_sha)
    other_tree = tree_of(repo, gs.run_git(repo, ["rev-parse", "dev"], check=True).stdout.strip())

    calls: list[str] = []
    runner = recording_runner(calls)
    first = tc.run_cached(
        rt.pytest_command(), cwd=str(repo), fingerprint=pushed_tree, runner=runner
    )
    assert first["cached"] is False
    assert len(calls) == 1

    second = tc.run_cached(
        rt.pytest_command(), cwd=str(repo), fingerprint=pushed_tree, runner=runner
    )
    assert second["cached"] is True
    assert second["git_state"] == pushed_tree
    assert len(calls) == 1, "the same tree *T* must be served from cache"

    served = tc.lookup(
        rt.pytest_command(),
        pushed_tree,
        cwd=str(repo),
        fingerprint_kind=tc.FINGERPRINT_KIND_TREE,
    )
    assert served is not None and served["scope"] == "full"

    other = tc.lookup(
        rt.pytest_command(),
        other_tree,
        cwd=str(repo),
        fingerprint_kind=tc.FINGERPRINT_KIND_TREE,
    )
    assert other is None, "a different tree *T'* must never hit"


# ── AC2: a different tree T′ stays a miss (today and after F4) ───────────────


def test_different_tree_verdict_is_never_served_to_the_gate(
    gate_repo: Path,
    pushed_sha: str,
    other_sha: str,
    executed: list[dict[str, str]],
    capsys: pytest.CaptureFixture,
) -> None:
    """A verdict recorded for *T* never gates a different tree *T′*.

    POST-FIX EXPECTATION (F4): the gate's tree-keyed lookup for *T′* is
    likewise a miss — this test's assertions are unchanged by F4, they are
    the "miss for a different tree" half of the contract that keeps F4 from
    serving *T*'s verdict for *T′*.
    """
    pushed_tree = tree_of(gate_repo, pushed_sha)
    other_tree = tree_of(gate_repo, other_sha)
    assert pushed_tree != other_tree, "the fixture must exercise two distinct trees"
    record_tree_verdict(gate_repo, pushed_tree)  # only *T* has a verdict
    assert tc.lookup(
        rt.pytest_command(),
        other_tree,
        cwd=str(gate_repo),
        fingerprint_kind=tc.FINGERPRINT_KIND_TREE,
    ) is None, "no verdict may exist for T' before the gate runs"

    # The other tree's commit is the main checkout's HEAD: the gate binds it
    # in place, same cache dir, and must STILL execute (no verdict for T′).
    git(gate_repo, "checkout", "-q", "dev")

    code, payload, stderr = run_gate(capsys, gate_repo, other_sha)

    assert code == 0, stderr
    assert executed, "no verdict exists for tree T'; the suite must run"
    assert payload.get("cached") is False
    assert payload["gate"]["cache_hit"] is False
    assert "cache_hit=false" in stderr
    # The gate stored a FRESH verdict for T′ (it never served T's verdict for
    # a different tree); the earlier T verdict is untouched.
    assert tc.lookup(
        rt.pytest_command(),
        other_tree,
        cwd=str(gate_repo),
        fingerprint_kind=tc.FINGERPRINT_KIND_TREE,
    ) is not None


# ── SA-0MWFJS3820032YB4: the finish lock window excludes the suite ───────────
#
# ``implement.py finish`` used to reach this gate only through the push — while
# holding the exclusive main-checkout lock — so concurrent finishes serialised
# their suite runs. It now resolves the gate's verdict for the would-be-commit
# tree *before* taking the lock (``resolve_prelock_full_suite_verdict`` →
# ``run_tests(worktree, scope="full", fingerprint=<tree>)``). These tests prove
# the two halves that make that a speed-up rather than a second suite run: the
# verdict the *finish* entry point records is the tree-keyed entry this gate
# serves, and a push with no pre-lock verdict still executes the suite.


def _declare_pytest_suite(repo: Path) -> str:
    """Give *repo* a declared pytest suite and return the new HEAD (clean).

    ``implement.run_tests`` resolves the full suite through the test skill (a
    declared ``pytest.ini`` or pytest-style ``tests/test_*.py``); without one
    there is no suite to run and no cache entry to serve. The commit keeps the
    working tree clean so the gate can still bind the pushed commit in place.
    """
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "declare a pytest suite")
    return gs.run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()


def test_finish_entry_point_verdict_is_served_to_the_gate(
    gate_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A verdict recorded by ``implement.run_tests`` IS served to the gate.

    This goes through the real finish-side entry point (suite resolution, guard
    delegation, cache write) rather than the cache layer directly, so it pins
    what the lock-window change actually relies on: the pre-lock run and the
    push's ``--gate-commit`` run agree on the cache key for the same tree. If
    they diverged, a finish push would re-execute the full suite inside the
    lock — the serialisation this item removes.
    """
    from skill.implement.scripts import implement

    repo = gate_repo
    pushed_sha = _declare_pytest_suite(repo)
    pushed_tree = tree_of(repo, pushed_sha)
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)

    executions: list[dict[str, str]] = []

    def fake_run_cmd(cmd, **kwargs):
        executions.append({"cmd": " ".join(cmd), "cwd": str(kwargs.get("cwd"))})
        return SimpleNamespace(returncode=0, stdout=SUMMARY_OUTPUT, stderr="")

    monkeypatch.setattr(rt, "_run_cmd", fake_run_cmd)

    # Exactly what ``phase_finish`` does before it takes the lock.
    verdict = implement.run_tests(str(repo), scope="full", fingerprint=pushed_tree)

    assert verdict["success"] is True, verdict
    assert verdict["skipped"] is False, verdict
    assert verdict["cached"] is False
    assert tc.lookup(
        rt.pytest_command(),
        pushed_tree,
        cwd=str(repo),
        fingerprint_kind=tc.FINGERPRINT_KIND_TREE,
    ) is not None, "the pre-lock run must record a tree-keyed verdict"
    assert len(executions) == 1, executions

    # The push that follows serves it: no second execution, and the runner's
    # own provenance line says so.
    code, payload, stderr = run_gate(capsys, repo, pushed_sha)

    assert code == 0, stderr
    assert len(executions) == 1, (
        "the gate must serve the pre-lock verdict for the same tree instead of "
        f"executing the suite again (executions: {executions})"
    )
    assert payload.get("cached") is True, payload
    assert payload["gate"]["cache_hit"] is True
    assert payload["gate"]["tree"] == pushed_tree
    assert "verdict=cached" in stderr
    assert "cache_hit=true" in stderr


def test_gate_executes_when_no_prelock_verdict_exists(
    gate_repo: Path,
    pushed_sha: str,
    executed: list[dict[str, str]],
    capsys: pytest.CaptureFixture,
) -> None:
    """Without the pre-lock verdict the gate still executes (fail closed).

    The speed-up is an optimisation, never a bypass: a push whose tree has no
    recorded verdict pays for the suite at the gate, exactly as before. This is
    also the degraded path when no would-be-commit tree exists to bind the
    pre-lock verdict to.
    """
    pushed_tree = tree_of(gate_repo, pushed_sha)
    assert tc.lookup(
        rt.pytest_command(),
        pushed_tree,
        cwd=str(gate_repo),
        fingerprint_kind=tc.FINGERPRINT_KIND_TREE,
    ) is None

    code, payload, stderr = run_gate(capsys, gate_repo, pushed_sha)

    assert code == 0, stderr
    assert executed, "the gate must execute the suite when no verdict exists"
    assert payload.get("cached") is False
    assert payload["gate"]["cache_hit"] is False


# ── SA-0MWFJS9OL001ZTTC: a lost push race must not re-run the full suite ─────
#
# ``dev`` is a shared ref, so concurrent finishes frequently lose the
# non-fast-forward race. ``finish`` rebases and retries; the rebased commit has
# a **new tree** but the **same patch**, and today that new tree misses the
# tree-keyed gate cache and re-executes the whole suite inside the
# main-checkout lock (observed at ~5-18 min per rebase round). The runner now
# also keys the verdict on the change set's patch content, so the post-rebase
# gate run serves the verdict measured before the race instead of re-running.


def _rev(repo: Path, rev: str) -> str:
    """Full commit sha of *rev* in *repo*."""
    return gs.run_git(repo, ["rev-parse", rev], check=True).stdout.strip()


def _advance_dev(repo: Path, path: str, content: str, message: str) -> None:
    """Commit a concurrent dev advance at *path*; leave ``feature`` checked out."""
    git(repo, "checkout", "-q", "dev")
    (repo / path).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    git(repo, "checkout", "-q", "feature")


def test_rebased_push_is_served_from_the_change_set_verdict(
    gate_repo: Path,
    pushed_sha: str,
    executed: list[dict[str, str]],
    capsys: pytest.CaptureFixture,
) -> None:
    """A pure rebase re-serves the pre-race verdict instead of re-running.

    Sequence (the observed lost-race path): the pre-lock verdict is recorded
    for the would-be-commit tree while the worktree still sits on ``dev``; a
    concurrent push advances ``dev``; the feature branch is rebased and the
    pushed commit is rewritten with a new tree; the dev-push gate then runs for
    the rebased commit. The suite must not execute a second time — the patch,
    and therefore the evidence, is unchanged.
    """
    repo = gate_repo
    would_be_tree = tree_of(repo, pushed_sha)
    git(repo, "checkout", "-q", "dev")
    record_tree_verdict(repo, would_be_tree)  # the pre-lock verdict
    assert not executed

    _advance_dev(repo, "unrelated.txt", "unrelated\n", "concurrent dev advance")
    pre_rebase = _rev(repo, "feature")
    git(repo, "rebase", "-q", "dev")
    rebased_sha = _rev(repo, "feature")
    rebased_tree = tree_of(repo, rebased_sha)

    assert rebased_sha != pre_rebase, "the fixture must rewrite the commit"
    assert rebased_tree != would_be_tree, "the rebase must produce a new tree"

    code, payload, stderr = run_gate(capsys, repo, rebased_sha)

    assert code == 0, stderr
    assert executed == [], (
        "the rebased push must be served from the identical change set — a "
        "second suite run per rebase round is the cost this item removes "
        f"(executions: {executed})"
    )
    assert payload.get("cached") is True
    assert payload["gate"]["cache_hit"] is True
    assert payload["gate"]["fingerprint_kind"] == tc.FINGERPRINT_KIND_CHANGE_SET
    assert payload["gate"]["tree"] == rebased_tree
    assert payload["gate"]["reused_from_tree"] == would_be_tree, (
        "the served verdict was measured on the pre-race tree and must say so"
    )
    verdict = [
        line
        for line in stderr.splitlines()
        if line.startswith("run_tests: gate-verdict")
    ]
    assert verdict, stderr
    assert "verdict=cached" in verdict[0]
    assert "cache_hit=true" in verdict[0]
    assert f"fingerprint_kind={tc.FINGERPRINT_KIND_CHANGE_SET}" in verdict[0]
    assert f"reused_from_tree={would_be_tree}" in verdict[0]
    # The inherited verdict is mirrored under the pushed tree's key for
    # tree-keyed consumers, stamped with its origin and the tree it was
    # actually measured on — never presented as direct tree evidence.
    mirrored = tc.lookup(
        rt.pytest_command(),
        rebased_tree,
        cwd=str(repo),
        fingerprint_kind=tc.FINGERPRINT_KIND_TREE,
    )
    assert mirrored is not None
    assert mirrored["verdict_origin"] == "change_set_reuse"
    assert mirrored["measured_tree"] == would_be_tree


def test_rebased_push_with_a_content_change_still_executes(
    gate_repo: Path,
    pushed_sha: str,
    executed: list[dict[str, str]],
    capsys: pytest.CaptureFixture,
) -> None:
    """Negative: a genuinely different patch is never served from the cache.

    The reuse is only ever for provably identical content. Here the rebased
    commit carries an extra content edit, so the patch differs and the suite
    must execute — the safety half of the change-set key.
    """
    repo = gate_repo
    would_be_tree = tree_of(repo, pushed_sha)
    git(repo, "checkout", "-q", "dev")
    record_tree_verdict(repo, would_be_tree)
    assert not executed

    _advance_dev(repo, "unrelated.txt", "unrelated\n", "concurrent dev advance")
    (repo / "file.txt").write_text("three\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "change the content")
    git(repo, "rebase", "-q", "dev")
    rebased_sha = _rev(repo, "feature")
    rebased_tree = tree_of(repo, rebased_sha)
    assert rebased_tree != would_be_tree

    code, payload, stderr = run_gate(capsys, repo, rebased_sha)

    assert code == 0, stderr
    assert executed, "a different patch must never reuse the recorded verdict"
    assert payload.get("cached") is False
    assert payload["gate"]["cache_hit"] is False
    assert payload["gate"].get("reused_from_tree") is None


def test_post_push_verification_accepts_a_change_set_served_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The finish path's read-only verification also honours the change-set key.

    ``phase_finish`` refuses the terminal transition unless the just-pushed
    commit has green full-suite evidence. A change-set-served push deliberately
    writes no tree-bound entry, so the verification must look under the
    change-set key too — otherwise this optimisation would turn a green push
    into ``missing`` and refuse to finalise.
    """
    from skill.implement.scripts import implement

    repo = gs.init_repo(tmp_path / "repo")
    # A declared suite is committed on the parent branch so it is present both
    # before and after the rebase (the verification resolves it from the
    # pushed-commit checkout).
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    gs.commit_all(repo, "A")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("two\n", encoding="utf-8")
    gs.commit_all(repo, "B")

    monkeypatch.setattr(rt, "REPO_ROOT", repo)
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(rt.nested_semaphore_dir(tmp_path)))
    monkeypatch.delenv("IMPLEMENT_TEST_COMMAND", raising=False)

    would_be_tree = tree_of(repo, "feature")
    monkeypatch.setattr(rt, "_run_cmd", lambda cmd, **kw: SimpleNamespace(
        returncode=0, stdout=SUMMARY_OUTPUT, stderr=""
    ))

    # The pre-lock run: HEAD is the parent branch, the verdict is bound to the
    # would-be-commit tree of the change set.
    git(repo, "checkout", "-q", "dev")
    verdict = implement.run_tests(str(repo), scope="full", fingerprint=would_be_tree)
    assert verdict["success"] is True, verdict
    assert verdict["skipped"] is False, verdict

    _advance_dev(repo, "unrelated.txt", "unrelated\n", "concurrent dev advance")
    git(repo, "rebase", "-q", "dev")
    rebased_sha = _rev(repo, "feature")
    assert tree_of(repo, rebased_sha) != would_be_tree

    code, payload, stderr = run_gate(capsys, repo, rebased_sha)
    assert code == 0, stderr
    assert payload["gate"]["fingerprint_kind"] == tc.FINGERPRINT_KIND_CHANGE_SET

    verification = implement._resolve_post_push_verification(
        str(repo), str(repo), rebased_sha
    )

    assert verification["verdict"] == implement.POST_PUSH_VERDICT_GREEN, verification
    assert verification["success"] is True
    entry = verification["entries"][0]
    assert entry["fingerprint_kind"] in {
        tc.FINGERPRINT_KIND_TREE,
        tc.FINGERPRINT_KIND_CHANGE_SET,
    }
    # The served verdict names the tree it was actually measured on, whichever
    # key it was read from (the change-set hit mirrors a flagged tree entry).
    measured = entry["change_set_target_tree"] or entry["measured_tree"]
    assert measured == would_be_tree
