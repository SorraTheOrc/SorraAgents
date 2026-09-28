#!/usr/bin/env python3
"""Deterministic reproduction of the live-repo leak mechanism (F1 evidence).

Purpose
-------
Demonstrate — without ever touching the live checkout — that a real-git test
fixture mutates a *victim* repository when a git environment variable
(``GIT_DIR``) leaks into the fixture's process environment, even though the
fixture's working directory is an isolated ``tmp_path`` repository.

Background
----------
On 2026-09-24 a full-suite run mutated the live checkout: local ``dev`` was
moved to fixture commits, ``core.bare`` was flipped to ``true``, the identity
and ``core.hooksPath`` were rewritten, tracked ``.githooks/*`` files were
deleted, and fixture commits were pushed to the real ``origin/dev``
(SA-0MUG0WFP8008WN63). The four unguarded real-git helpers implicated were:

  * ``skill/audit/tests/test_audit_runner_launch_context.py``
    ``_make_real_git_project_with_worktree``  → branch ``wl-OSL-1-test``
  * ``skill/audit/tests/test_audit_runner_merge_gate.py`` ``_make_real_repo``
    → ``git init --bare`` (sets ``core.bare=true``), ``branch -M dev``,
    ``remote add origin``, ``push origin dev``
  * ``skill/test/tests/test_run_tests_scope.py`` ``_make_repo``
    → ``branch -M dev``
  * ``tests/unit/test-code-freeze-marker.mjs`` / ``test-pre-push-hook.mjs``
    → ``feature-x`` / ``wl-SA-001-test-feature``

Hypotheses investigated (time-boxed, see ``test-isolation.md``):

  1. **Environment leak** — ``GIT_DIR`` (or ``GIT_WORK_TREE`` / ``GIT_CONFIG``)
     inherited by every ``git`` subprocess, overriding ``cwd``. REPRODUCED here.
  2. **TMPDIR / basetemp misresolution** — pytest's ``tmp_path`` falling back
     to the project root. NOT reproduced; ``tmp_path`` uses the system temp
     dir (``tempfile.gettempdir()``), which is cwd-independent.

Safety
------
Every scenario runs against a *throwaway victim repository created inside the
system temp dir* with its own local bare ``origin``. The script only ever sets
``GIT_DIR`` to that victim, so even the leaked commands cannot reach the live
checkout. The script refuses to run if the victim directory is inside a git
repository.

Usage
-----
    python3 docs/dev/repro_live_repo_leak.py [--runs N]

Exit codes:
    0  the leak was demonstrated on every run (the expected outcome);
    1  the leak was NOT demonstrated (investigate before trusting the guard).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths and git helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Git environment variables that override the target repository.
LEAK_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG", "GIT_CONFIG_GLOBAL")


def _clean_env() -> dict[str, str]:
    """Environment with every repository-overriding git variable removed."""
    env = dict(os.environ)
    for name in LEAK_VARS:
        env.pop(name, None)
    return env


def _git(args: list[str], cwd: Path, env: dict[str, str] | None = None):
    """Run git with a repository-override-free environment."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env if env is not None else _clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_not_in_git_repo(path: Path) -> None:
    """Refuse to proceed when *path* sits inside any git repository."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    proc = _git(["rev-parse", "--show-toplevel"], probe)
    assert proc.returncode != 0, (
        f"refusing to run: {path} is inside git repository "
        f"{proc.stdout.strip()!r}; the reproduction must be hermetic"
    )


def _new_victim(root: Path) -> tuple[Path, Path]:
    """Create a victim repo (with a bare origin) under *root*.

    The victim simulates the live checkout: a ``dev`` branch, a real commit,
    an ``origin`` remote pointing at a local bare repo.
    """
    _assert_not_in_git_repo(root)
    victim = root / "victim"
    victim.mkdir(parents=True)
    bare = root / "victim-origin.git"
    bare.mkdir()

    _git(["init", "--bare", "-q"], bare)
    _git(["init", "-q"], victim)
    _git(["config", "user.email", "real@example.com"], victim)
    _git(["config", "user.name", "Real"], victim)
    (victim / "src").mkdir()
    (victim / "src" / "main.py").write_text("print('real')\n", encoding="utf-8")
    _git(["add", "-A"], victim)
    _git(["commit", "-qm", "real dev commit"], victim)
    _git(["branch", "-M", "dev"], victim)
    _git(["remote", "add", "origin", str(bare)], victim)
    _git(["push", "-q", "origin", "dev"], victim)
    return victim, bare


def _snapshot(victim: Path) -> tuple[str, str]:
    """Return (refs, local-config) fingerprints for *victim*."""
    refs = _git(
        ["for-each-ref", "--format=%(refname) %(objectname)",
         "refs/heads", "refs/remotes"],
        victim,
    ).stdout
    cfg = _git(["config", "--local", "--list"], victim).stdout
    return refs, cfg


# ---------------------------------------------------------------------------
# Fixture imports (the REAL, currently unguarded helpers)
# ---------------------------------------------------------------------------

from audit.tests.test_audit_runner_launch_context import (
    _make_real_git_project_with_worktree,
)
from audit.tests.test_audit_runner_merge_gate import _make_real_repo
from test.tests.test_run_tests_scope import _make_repo


def _scenario_launch_context(work: Path) -> None:
    _make_real_git_project_with_worktree(work / "inner", prefix="OSL")


def _scenario_merge_gate(work: Path) -> None:
    _make_real_repo(work / "inner")


def _scenario_run_tests_scope(work: Path) -> None:
    _make_repo(work / "inner")


SCENARIOS: dict[str, Callable[[Path], None]] = {
    "launch-context _make_real_git_project_with_worktree": _scenario_launch_context,
    "merge-gate _make_real_repo": _scenario_merge_gate,
    "run-tests-scope _make_repo": _scenario_run_tests_scope,
}


# ---------------------------------------------------------------------------
# One reproduction run
# ---------------------------------------------------------------------------


def run_once() -> dict[str, object]:
    """Run every scenario once and return the captured signatures."""
    root = Path(tempfile.mkdtemp(prefix="wl-leak-repro-"))
    results: dict[str, object] = {"root": str(root), "scenarios": {}}
    try:
        for name, scenario in SCENARIOS.items():
            victim, _bare = _new_victim(root / name.replace(" ", "_"))
            before_refs, before_cfg = _snapshot(victim)

            # Leak GIT_DIR into the child environment — the only difference
            # from a correctly-isolated fixture invocation.
            leaked_env = dict(os.environ)
            leaked_env["GIT_DIR"] = str(victim / ".git")
            previous = {k: os.environ.get(k) for k in LEAK_VARS}
            for k in LEAK_VARS:
                os.environ.pop(k, None)
            os.environ["GIT_DIR"] = str(victim / ".git")
            try:
                work = root / f"{name.replace(' ', '_')}-work"
                (work / "inner").mkdir(parents=True)
                scenario(work)
                outcome = "completed"
            except Exception as exc:  # noqa: BLE001
                outcome = f"raised {type(exc).__name__}"
            finally:
                for k, v in previous.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

            after_refs, after_cfg = _snapshot(victim)
            new_refs = sorted(
                set(after_refs.splitlines()) - set(before_refs.splitlines())
            )
            new_cfg = sorted(
                set(after_cfg.splitlines()) - set(before_cfg.splitlines())
            )
            core_bare = _git(["config", "--local", "core.bare"], victim).stdout.strip()
            results["scenarios"][name] = {
                "victim": str(victim),
                "outcome": outcome,
                "leaked": before_refs != after_refs or before_cfg != after_cfg,
                "new_refs": new_refs,
                "new_config": new_cfg,
                "core_bare": core_bare,
            }
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3,
                        help="consecutive runs to demonstrate (default 3)")
    args = parser.parse_args()

    all_leaked = True
    for index in range(1, args.runs + 1):
        print(f"=== run {index}/{args.runs} ===")
        results = run_once()
        for name, data in results["scenarios"].items():  # type: ignore[union-attr]
            print(f"[{'LEAK' if data['leaked'] else 'CLEAN'}] {name} ({data['outcome']})")
            for line in data["new_refs"]:
                print(f"    + ref     {line}")
            for line in data["new_config"]:
                print(f"    + config  {line}")
            if data["core_bare"] == "true":
                print("    + core.bare=true")
            all_leaked = all_leaked and bool(data["leaked"])
        print()

    if all_leaked:
        print("RESULT: leak mechanism REPRODUCED in all scenarios and runs.")
        return 0
    print("RESULT: leak NOT reproduced; investigate before relying on the guard.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
