"""Detect local branches whose git tracking config resolves to themselves.

Long-lived branches (``dev``/``main``) whose upstream is the repository
itself (``branch.<name>.remote = "."``) make a plain ``git pull`` a silent
no-op: it fetches from ``.``, prints ``From .`` / ``Already up to date``,
exits 0, and no remote data ever arrives. The epic root cause is documented
in SA-0MVX1M6HH004YPX2; this module is the detection half of the fix
(SA-0MVX359B7008R9F3).

Why the defect is machine-wide: ``branch.<name>.remote`` is one key per
**branch name** stored in the shared ``.git/config``, so the breakage is
independent of which checkout runs the pull.

The ``remote = "."`` form is *legitimate* only for ``wl-*`` worktree
branches: ``git worktree add --track -b wl-* ... <local-base>`` writes it as
the worktree-ownership signal. Everything else with a self-referential
upstream is a defect.

Classification contract (pinned by ``tests/test_self_referential_upstream.py``):

* ``dev``, ``main`` and any branch named in the repository's release config
  (for this repo's ship pipeline: ``dev``/``main``) are **long-lived**: a
  self-referential upstream is a **defect**;
* ``wl-*`` branches are **worktree branches**: ``remote = "."`` is expected
  and reported as ``type == "expected"`` — never a defect (callers filter on
  ``type == "defect"`` and ignore the rest);
* a healthy ``origin``-tracked branch (``remote = origin``,
  ``merge = refs/heads/<branch>``) is **not** self-referential and produces
  no finding.

The check is callable from bash (``--json`` / exit codes); the Repair child
(SA-0MVX359TI0069ECK) joins ``--fix`` onto the same entry point and the Wire
child (SA-0MVX35AUC003CNUD) consumes it from ``hygiene_check.sh`` and
``implement.py start``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# The module must be importable both as ``shared.self_referential_upstream``
# (test/bootstrap consumers prepend ``<repo>/skill`` to ``sys.path``) and as a
# standalone script from bash (``python3 skill/shared/self_referential_upstream.py``).
# Guarded bootstrap: a no-op when the importing process already resolved the
# package, so it never shadows an active ``shared`` package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_ROOT = _REPO_ROOT / "skill"
for _path in (str(_REPO_ROOT), str(_SKILL_ROOT)):
    if _path not in sys.path:
        sys.path.append(_path)

#: Shared choke point for subprocess git invocation: repository-override
#: environment variables (``GIT_DIR`` and friends) are scrubbed so a leaked
#: value can never redirect the check onto another repository
#: (SA-0MUIA3OE40001QJX; see ``shared/git_sandbox.py``).
from shared.git_sandbox import git_env_for_repo

#: Branch names with this prefix own the ``remote = "."`` worktree signal and
#: are never defects.
WORKTREE_BRANCH_PREFIX = "wl-"


class NotARepositoryError(RuntimeError):
    """Raised when *repo* does not contain a readable git repository."""


def _run_git(repo: str | os.PathLike[str], args: list[str]) -> subprocess.CompletedProcess:
    """Run a read-only plumbing command against *repo* with a hermetic env."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=git_env_for_repo(repo),
        capture_output=True,
        text=True,
        check=False,
    )


def _branch_names(repo: str | os.PathLike[str]) -> list[str]:
    """Local branch names via plumbing (never a path/``wl-*`` name pattern)."""
    proc = _run_git(repo, ["for-each-ref", "refs/heads", "--format=%(refname:short)"])
    if proc.returncode != 0:
        raise NotARepositoryError(
            f"not a git repository (or unreadable): {repo}: {proc.stderr.strip()}"
        )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _local_config(repo: str | os.PathLike[str]) -> dict[str, str]:
    """The repository's local config (``branch.<name>.remote``/``merge`` live here)."""
    proc = _run_git(repo, ["config", "--local", "--list"])
    if proc.returncode != 0:
        raise NotARepositoryError(
            f"not a git repository (or unreadable): {repo}: {proc.stderr.strip()}"
        )
    config: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    return config


def _tracking_config(config: dict[str, str], branch: str) -> tuple[str | None, str | None]:
    """The ``(remote, merge)`` tracking pair for *branch*, or ``(None, None)``."""
    remote = config.get(f"branch.{branch}.remote")
    merge = config.get(f"branch.{branch}.merge")
    return (remote, merge)


def _defect_message(branch: str, merge: str | None) -> str:
    merge_clause = f", merge {merge}" if merge else ""
    return (
        f"self-referential upstream: branch.{branch}.remote='.'{merge_clause} — the "
        "tracking config resolves inside this repository, so `git pull` fetches "
        "from '.' and silently retrieves nothing. Restore the upstream to a real "
        f"remote when it has the branch, e.g. `git config branch.{branch}.remote origin`."
    )


def _expected_message(branch: str) -> str:
    return (
        f"branch.{branch}.remote='.' is the worktree-ownership signal written by "
        "`git worktree add --track`; expected for wl-* branches, not a defect."
    )


def detect_self_referential_upstreams(
    repo: str | os.PathLike[str],
) -> list[dict[str, str | None]]:
    """Return every branch whose upstream resolves to the repository itself.

    A finding is emitted for **each** local branch whose
    ``branch.<name>.remote`` is ``"."`` — the self-referential-upstream
    signal. The finding's ``type`` is ``"defect"`` for any non-worktree
    branch (a ``remote = "."`` upstream is broken there) and ``"expected"``
    for ``wl-*`` worktree branches (the legitimate ownership signal), so
    callers filter on ``type == "defect"`` and ignore the rest.

    Healthy ``origin``-tracked branches (``remote != "."``) produce **no**
    finding; the ``merge = refs/heads/<branch>`` pair with a real remote is
    normal and never flagged (AC4 — see the reconciliation note in the work
    item).

    Args:
        repo: Path to the repository to inspect (a checkout or linked
            worktree; ``branch.<name>.*`` config lives in the common config).

    Returns:
        A list of findings sorted by branch name. Each finding is a mapping
        with the AC4 keys ``branch``, ``type`` (``defect``|``expected``),
        ``remote``, ``merge`` and ``message``. ``remote`` is always ``"."``;
        ``merge`` is ``None`` when the branch has no merge key.

    Raises:
        NotARepositoryError: when *repo* is not a readable git repository.
    """
    repo_path = Path(repo)
    config = _local_config(repo_path)
    findings: list[dict[str, str | None]] = []
    for branch in sorted(_branch_names(repo_path)):
        remote, merge = _tracking_config(config, branch)
        if remote != ".":
            continue
        if branch.startswith(WORKTREE_BRANCH_PREFIX):
            findings.append(
                {
                    "branch": branch,
                    "type": "expected",
                    "remote": remote,
                    "merge": merge,
                    "message": _expected_message(branch),
                }
            )
        else:
            findings.append(
                {
                    "branch": branch,
                    "type": "defect",
                    "remote": remote,
                    "merge": merge,
                    "message": _defect_message(branch, merge),
                }
            )
    return findings


def _defects(findings: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    """Only the ``type == "defect"`` findings."""
    return [f for f in findings if f.get("type") == "defect"]


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="self_referential_upstream",
        description=(
            "Report local branches whose git tracking config resolves to the "
            "repository itself (branch.<name>.remote = '.') — the defect that "
            "silently breaks `git pull` on worktree machines."
        ),
        epilog=(
            "Exit codes: 0 = no defects found; 1 = defects present; "
            "2 = usage or repository error."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository to check (default: the current directory).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (AC4 fields per finding).",
    )
    args = parser.parse_args(argv)

    try:
        repo = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path.cwd()
        findings = detect_self_referential_upstreams(repo)
    except (NotARepositoryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    defects = _defects(findings)
    if args.json:
        payload = {
            "repo": str(repo),
            "ok": not defects,
            "defect_count": len(defects),
            "expected_count": len(findings) - len(defects),
            "findings": findings,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for finding in findings:
            print(f"[{finding['type']}] {finding['branch']}: {finding['message']}")
        print(f"{len(findings)} finding(s); {len(defects)} defect(s) in {repo}.")
        print("Use --json for machine-readable output (AC4).")
    return 1 if defects else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code (0/1/2)."""
    return _run_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
