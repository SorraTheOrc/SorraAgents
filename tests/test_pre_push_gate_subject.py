"""The dev-push full-suite gate is bound to the pushed commit (SA-0MVTRBT50005DUEH).

Regression tests for AC5: the ``--gate-commit`` subject resolution of
``run_tests.py``. The gate must never accept a cache entry computed from any
state other than the pushed commit's clean checkout, and it must refuse the
push (running nothing) when the pushed commit cannot be bound to a clean
checkout at all.

``executed`` vs ``cached`` is driven by staged cache entries and a fake
``_run_cmd`` (never by wall-clock timing — see
``skill/shared/test-writing-guidelines.md``).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test.scripts.run_tests as rt
import test_cache as tc
from shared import git_sandbox as gs

SUMMARY_OUTPUT = (
    "Test Files  3 passed (3)\n"
    "Tests  12 passed (12)\n"
    "Duration  1.5s\n"
)


def git(repo: Path, *args: str) -> None:
    gs.run_git(repo, args, check=True)


def head(repo: Path) -> str:
    return gs.run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()


def add_detached_worktree(repo: Path, path: Path, sha: str) -> Path:
    gs.run_git(repo, ["worktree", "add", "--detach", str(path), sha], check=True)
    return path


@pytest.fixture
def gate_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo with two commits; ``dev`` stays on the first.

    The second commit (on branch ``feature``) stands in for the pushed commit,
    so the main checkout's own state is a *different* state — the exact shape
    of the reported defect.
    """
    repo = gs.init_repo(tmp_path / "repo")
    (repo / "file.txt").write_text("one\n")
    gs.commit_all(repo, "A")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("two\n")
    gs.commit_all(repo, "B")
    git(repo, "checkout", "-q", "dev")

    monkeypatch.setattr(rt, "REPO_ROOT", repo)
    # Isolate the semaphore namespace: this suite's outer run already holds a
    # host-wide slot, so nested acquisitions must not contend for it
    # (SA-0MUY6U05F008RG86).
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(rt.nested_semaphore_dir(tmp_path)))
    return repo


@pytest.fixture
def pushed_sha(gate_repo: Path) -> str:
    return gs.run_git(gate_repo, ["rev-parse", "feature"], check=True).stdout.strip()


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Replace the real suite spawn with a recording fake (green)."""
    calls: list[dict[str, str]] = []

    def fake(cmd: list[str], **kwargs):
        calls.append({"cmd": " ".join(cmd), "cwd": str(kwargs.get("cwd"))})
        return SimpleNamespace(returncode=0, stdout=SUMMARY_OUTPUT, stderr="")

    monkeypatch.setattr(rt, "_run_cmd", fake)
    return calls


def run_gate(
    capsys: pytest.CaptureFixture,
    repo: Path,
    sha: str | None,
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
    ]
    if sha is not None:
        argv += ["--gate-commit", sha]
    code = rt.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip().startswith("{") else {}
    return code, payload, captured.err


# ── AC5.1: another tree's green entry cannot satisfy the gate ───────────────


def test_main_checkout_green_entry_does_not_satisfy_a_different_commit(
    gate_repo: Path, pushed_sha: str, executed: list[dict[str, str]],
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """A green full entry at the main checkout's state must not gate commit B.

    The suite must execute, in a checkout of the *pushed* commit, and the
    green entry left behind must be keyed on that commit's clean state.
    """
    main_state = tc.compute_git_state(gate_repo)
    tc.store(
        rt.pytest_command(), main_state, cwd=gate_repo,
        stdout=SUMMARY_OUTPUT, stderr="", exit_code=0, scope="full",
    )
    worktree = add_detached_worktree(gate_repo, tmp_path / "wt", pushed_sha)

    code, _payload, stderr = run_gate(capsys, gate_repo, pushed_sha)

    assert code == 0, stderr
    assert executed, "the suite must execute for the pushed commit"
    assert Path(executed[0]["cwd"]).resolve() == worktree.resolve(), (
        "the suite must run in the pushed commit's checkout"
    )
    assert "run_tests: gate-subject" in stderr
    assert "pushed=" + pushed_sha in stderr

    pushed_state = tc.compute_git_state(worktree)
    assert pushed_state != main_state
    entry = tc.query_cached(rt.pytest_command(), cwd=str(worktree))
    assert entry is not None, "a green entry must exist at the pushed commit"
    assert entry["scope"] == "full"
    assert entry["fingerprint_kind"] == "worktree"


# ── AC5.2: an entry at the pushed commit's clean state IS served ────────────


def test_green_entry_at_the_pushed_commit_is_served_from_cache(
    gate_repo: Path, pushed_sha: str, executed: list[dict[str, str]],
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """A staged green entry keyed on the pushed clean state is reused verbatim."""
    worktree = add_detached_worktree(gate_repo, tmp_path / "wt", pushed_sha)
    pushed_state = tc.compute_git_state(worktree)
    tc.store(
        rt.pytest_command(), pushed_state, cwd=worktree,
        stdout=SUMMARY_OUTPUT, stderr="", exit_code=0, scope="full",
    )

    code, payload, stderr = run_gate(capsys, gate_repo, pushed_sha)

    assert code == 0, stderr
    assert not executed, "a green entry at the pushed state must be served cached"
    assert payload["cached"] is True
    assert payload["cache_entries"][0]["fingerprint_kind"] == "worktree"
    assert payload["cache_entries"][0]["scope"] == "full"
    verdict = [line for line in stderr.splitlines() if line.startswith("run_tests: gate-verdict")]
    assert verdict, stderr
    assert "verdict=cached" in verdict[0]
    assert f"git_state={pushed_state}" in verdict[0]
    assert "fingerprint_kind=worktree" in verdict[0]


# ── AC5.3: a dirty push cwd at the pushed sha still gates the clean tree ────


def test_dirty_push_cwd_at_the_pushed_commit_gates_the_clean_worktree(
    gate_repo: Path, pushed_sha: str, executed: list[dict[str, str]],
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """The gate must prefer a clean checkout of the pushed commit over a dirty cwd.

    A green entry keyed on the *dirty* push cwd's state must not satisfy the
    gate: that state is not the pushed commit's clean state.
    """
    git(gate_repo, "checkout", "-q", "feature")
    (gate_repo / "file.txt").write_text("uncommitted edit\n")
    dirty_state = tc.compute_git_state(gate_repo)
    tc.store(
        rt.pytest_command(), dirty_state, cwd=gate_repo,
        stdout=SUMMARY_OUTPUT, stderr="", exit_code=0, scope="full",
    )
    worktree = add_detached_worktree(gate_repo, tmp_path / "clean-wt", pushed_sha)

    code, _payload, stderr = run_gate(capsys, gate_repo, pushed_sha)

    assert code == 0, stderr
    assert executed, "the dirty cwd's cached verdict must not be reused"
    assert Path(executed[0]["cwd"]).resolve() == worktree.resolve()
    assert "checkout=worktree" in stderr


# ── AC4/AC5.4: unmaterialisable subject refuses and runs nothing ────────────


def test_unmaterialisable_pushed_commit_refuses_with_actionable_message(
    gate_repo: Path, executed: list[dict[str, str]], capsys: pytest.CaptureFixture,
) -> None:
    """A commit that is not present locally refuses the push, running nothing."""
    missing = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    code, _payload, stderr = run_gate(capsys, gate_repo, missing)

    assert code == 2
    assert not executed, "no suite may run when the subject cannot be resolved"
    assert "run_tests: gate-refused" in stderr
    assert missing[:12] in stderr
    assert "TEST_SCOPE_SKIP=1" in stderr


def test_only_a_dirty_checkout_of_the_pushed_commit_refuses(
    gate_repo: Path, pushed_sha: str, executed: list[dict[str, str]],
    capsys: pytest.CaptureFixture,
) -> None:
    """A checkout at the pushed commit with dirty paths cannot be gated."""
    git(gate_repo, "checkout", "-q", "feature")
    (gate_repo / "file.txt").write_text("uncommitted edit\n")

    code, _payload, stderr = run_gate(capsys, gate_repo, pushed_sha)

    assert code == 2
    assert not executed
    assert "run_tests: gate-refused" in stderr
    assert "dirty" in stderr
    assert "TEST_SCOPE_SKIP=1" in stderr


# ── Default behaviour is untouched ─────────────────────────────────────────


def test_no_gate_commit_keeps_the_legacy_project_root_behaviour(
    gate_repo: Path, executed: list[dict[str, str]], tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Without --gate-commit the runner behaves exactly as before."""
    # A linked worktree at a *different* commit exists, but must be ignored.
    git(gate_repo, "checkout", "-q", "feature")
    add_detached_worktree(gate_repo, tmp_path / "wt", head(gate_repo))
    git(gate_repo, "checkout", "-q", "dev")

    code, _payload, stderr = run_gate(capsys, gate_repo, None)

    assert code == 0, stderr
    assert executed
    assert Path(executed[0]["cwd"]).resolve() == gate_repo.resolve()
    assert "run_tests: gate-subject" not in stderr
