#!/usr/bin/env python3
"""Non-destructive tracer for the GIT_DIR launcher-leak propagation path.

Purpose
-------
Reproduce, without ever touching the live checkout, the two facts that
underpin the 2026-09-26 leaked-``GIT_DIR`` incident investigation
(SA-0MUIZSXEY008NGR8):

1. **A spawn that forwards the parent environment propagates
   repository-override variables.** The herdr downtime worker's
   ``buildDowntimeSpawnOptions`` (ContextHub
   ``packages/herdr/src/downtime-worker.ts``) builds the child environment
   with ``{ ...process.env, HERDR_RESOLVED_CWD, AUDIT_PHASE2_PARALLELISM }``.
   Every variable already present in the herdr process — including
   ``GIT_DIR`` / ``GIT_WORK_TREE`` / ``GIT_CONFIG*`` — is forwarded to the
   spawned pane, and from there to ``send-to-pi.sh`` → ``run-pi-agent.sh`` →
   ``pi`` → test subprocesses.

2. **Git sets repository-overriding variables for hook subprocesses.** When
   ``git worktree add`` runs the ``post-checkout`` hook, git exports
   ``GIT_EXEC_PATH`` and ``GIT_PREFIX`` (empty) into the hook environment —
   the signature observed in the incident. A hook that itself forwards its
   environment therefore carries that context further down the process tree.

Background
----------
The in-repo defence-in-depth layers (``shared.git_sandbox`` scrub,
``run_tests._run_cmd``, ``audit_runner._call_pi``, the ``--strict-git-env``
release gate) neutralise the *effect* of a leaked override at every in-repo
execution path, but the exporting launcher lives outside the ``skill/`` tree
(see ``docs/dev/test-isolation.md`` §9.3). This probe captures the evidence
that the external spawn boundary is the propagation point.

Safety
------
Every scenario runs against a throwaway repository created inside the system
temp directory. The script never sets ``GIT_DIR`` to a live checkout and
refuses to run a scenario whose repository is nested inside another git work
tree. The ``/proc`` scan is read-only.

Usage
-----
    python3 docs/dev/trace_git_env_propagation.py [--json]
    python3 docs/dev/trace_git_env_propagation.py --session-env [--json]

Exit codes:
    0  both propagation facts were demonstrated (the expected outcome);
       with ``--session-env``, the session and a fresh child are clean;
    1  a fact was NOT demonstrated, or an override was present.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

# Load the shared override-var list and safety helper by location so the script
# runs from any cwd without importing the whole ``skill`` package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from skill.shared.git_sandbox import (
    REPOSITORY_OVERRIDE_ENV_VARS,
    assert_outside_git_worktree,
)

#: Process ``comm`` substrings that identify a freshly launched agent/pane.
DEFAULT_PROCESS_PATTERNS: tuple[str, ...] = (
    "pi",
    "node",
    "herdr",
    "run-pi-agent",
    "send-to-pi",
    "audit_runner",
    "run_tests",
)


def repository_overrides_in_env(env: Mapping[str, str]) -> tuple[str, ...]:
    """Return the repository-override variable names present in *env*.

    Order follows :data:`REPOSITORY_OVERRIDE_ENV_VARS` so output is stable.
    """
    return tuple(name for name in REPOSITORY_OVERRIDE_ENV_VARS if name in env)


def _read_environ(path: Path) -> dict[str, str]:
    """Read a ``/proc/<pid>/environ`` file into a mapping (best-effort)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse ``env``-style (newline-separated) output into a mapping."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _read_env_dump(path: Path) -> dict[str, str]:
    """Read ``env``-style (newline-separated) output into a mapping."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    return _parse_env_text(text)


def capture_fresh_child_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Capture the environment a freshly spawned child process inherits.

    Spawns ``env`` (no ``git`` performed) with an environment built by
    forwarding *env* (default :data:`os.environ`) — the same
    ``{ ...parent_env }`` forwarding the herdr launcher uses — and returns
    the mapping the child observed. A fresh child of a clean session reports
    no repository-override variables.
    """
    source = dict(os.environ if env is None else env)
    proc = subprocess.run(
        ["env"],
        env=source,
        capture_output=True,
        text=True,
        check=True,
    )
    return _parse_env_text(proc.stdout)


def scan_proc_environ(
    proc_root: str | os.PathLike[str] = "/proc",
    *,
    patterns: Iterable[str] = DEFAULT_PROCESS_PATTERNS,
) -> list[dict[str, object]]:
    """Scan live processes for repository-override variables (read-only).

    Returns a list of ``{"pid", "comm", "overrides"}`` records for every
    process whose command name matches one of *patterns* and whose
    environment carries at least one repository-override variable. An empty
    list is the healthy outcome: no live agent/pane process inherits an
    override.
    """
    root = Path(proc_root)
    needles = tuple(p.lower() for p in patterns)
    findings: list[dict[str, object]] = []
    if not root.is_dir():
        return findings
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if not any(needle in comm.lower() for needle in needles):
            continue
        overrides = repository_overrides_in_env(_read_environ(entry / "environ"))
        if overrides:
            findings.append(
                {"pid": int(entry.name), "comm": comm, "overrides": list(overrides)}
            )
    return findings


def capture_hook_env(
    *, repo_root: str | os.PathLike[str]
) -> dict[str, str]:
    """Capture the environment git gives a ``post-checkout`` hook subprocess.

    Creates a throwaway repository under *repo_root* (which must not be nested
    inside another git work tree), installs a ``post-checkout`` hook that dumps
    its environment, runs ``git worktree add`` and returns the captured
    environment. Only the repository-override and ``GIT_*`` bookkeeping
    variables are relevant; the full mapping is returned for the caller to
    filter.
    """
    root = Path(repo_root)
    assert_outside_git_worktree(root)
    root.mkdir(parents=True, exist_ok=True)

    def _git(args: list[str], *, cwd: Path | None = None) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd or root),
            check=True,
            capture_output=True,
            text=True,
        )

    _git(["init", "-q", "-b", "dev"])
    (root / "README.md").write_text("probe\n")
    _git(["add", "-A"])
    _git(
        [
            "-c",
            "user.name=Git Env Probe",
            "-c",
            "user.email=probe@example.invalid",
            "commit",
            "-q",
            "-m",
            "probe",
        ]
    )

    hooks = root / ".githooks"
    hooks.mkdir(exist_ok=True)
    env_dump = root / "hook-env.txt"
    hook = hooks / "post-checkout"
    hook.write_text(
        "#!/bin/sh\n"
        f"env > {env_dump}\n"
        "exit 0\n"
    )
    hook.chmod(0o755)
    _git(["config", "core.hooksPath", ".githooks"])

    worktree = root.parent / f"{root.name}-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "HEAD"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )
    return _read_env_dump(env_dump)


def demonstrate_spawn_propagation(
    *, repo_root: str | os.PathLike[str]
) -> dict[str, str]:
    """Prove that forwarding the parent env leaks overrides into a child.

    Mirrors the herdr spawn semantics (``env: { ...process.env, ... }``):
    a child spawned with an environment built by spreading the parent env
    inherits any repository-override variable the parent carried. Runs
    against a throwaway repository; never points an override at a live repo.
    """
    root = Path(repo_root)
    assert_outside_git_worktree(root)
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "dev"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )

    parent_env = dict(os.environ)
    parent_env.update(
        {
            "GIT_DIR": str(root / ".git"),
            "GIT_WORK_TREE": str(root),
            "GIT_EXEC_PATH": "/usr/lib/git-core",
        }
    )
    # `{ ...process.env, HERDR_RESOLVED_CWD }` — only new keys are added; no
    # existing repository-override variable is filtered.
    child_env = {
        **parent_env,
        "HERDR_RESOLVED_CWD": str(root),
        "AUDIT_PHASE2_PARALLELISM": "1",
    }
    return {
        name: child_env[name]
        for name in repository_overrides_in_env(child_env)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", action="store_true", help="emit the report as JSON"
    )
    parser.add_argument(
        "--session-env",
        action="store_true",
        help=(
            "capture the current session environment and a freshly spawned "
            "child (no git) and report repository-override variables; exits 1 "
            "when any override is present"
        ),
    )
    args = parser.parse_args(argv)

    if args.session_env:
        session_env = dict(os.environ)
        child_env = capture_fresh_child_env(session_env)
        session_overrides = repository_overrides_in_env(session_env)
        child_overrides = repository_overrides_in_env(child_env)
        session_report: dict[str, object] = {
            "session_overrides": list(session_overrides),
            "fresh_child_overrides": list(child_overrides),
            "clean": not session_overrides and not child_overrides,
        }
        if args.json:
            print(json.dumps(session_report, indent=2, sort_keys=True))
        else:
            print("Controlled fresh-session environment dump")
            print("=" * 60)
            print(
                "current session repository-override vars: "
                + (", ".join(session_overrides) if session_overrides else "(none)")
            )
            print(
                "fresh child repository-override vars:    "
                + (", ".join(child_overrides) if child_overrides else "(none)")
            )
        return 0 if session_report["clean"] else 1

    with tempfile.TemporaryDirectory(prefix="git-env-probe-") as tmp:
        tmp_path = Path(tmp)

        live_proc_findings = scan_proc_environ()

        hook_env = capture_hook_env(repo_root=tmp_path / "hook-repo")
        hook_git_vars = {
            name: value
            for name, value in hook_env.items()
            if name.startswith("GIT_")
        }

        spawned = demonstrate_spawn_propagation(repo_root=tmp_path / "spawn-repo")

    hook_sets_git_context = bool(hook_git_vars.get("GIT_EXEC_PATH"))
    spawn_leaks = bool(spawned)

    report: dict[str, object] = {
        "live_process_overrides": live_proc_findings,
        "hook_git_env": hook_git_vars,
        "spawned_child_overrides": spawned,
        "hook_sets_git_context": hook_sets_git_context,
        "spawn_forwards_overrides": spawn_leaks,
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("GIT environment propagation probe")
        print("=" * 60)
        print("\n1. Live agent/pane processes with repository-override vars")
        if live_proc_findings:
            for finding in live_proc_findings:
                overrides = finding["overrides"]
                print(
                    f"   - pid {finding['pid']} ({finding['comm']}): {overrides}"
                )
        else:
            print("   (none — healthy: no live process inherits an override)")
        print("\n2. Git hook subprocess environment (GIT_* set by git)")
        if hook_git_vars:
            for name, value in sorted(hook_git_vars.items()):
                print(f"   {name}={value!r}")
        else:
            print("   (no GIT_* variables captured)")
        print("\n3. Overrides forwarded into a spawned child (spread parent env)")
        if spawned:
            for name, value in sorted(spawned.items()):
                print(f"   {name}={value!r}")
        else:
            print("   (none — unexpected)")

    ok = hook_sets_git_context and spawn_leaks
    if not args.json:
        print("\n" + "=" * 60)
        print(
            "RESULT: propagation "
            + ("CONFIRMED (both facts demonstrated)" if ok else "NOT confirmed")
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
