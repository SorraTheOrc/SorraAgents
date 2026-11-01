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

The check is callable from bash (``--json`` / exit codes); ``--fix`` repairs
self-referential defects to ``remote=origin`` / ``merge=refs/heads/<b>`` only
when ``origin`` has the branch (SA-0MVX359TI0069ECK), ``--dry-run`` previews
without writing. Exit codes: ``0`` no defects (or all repaired), ``1``
defects found / still present, ``2`` environment error (not a git repository
/ origin remote missing). The Wire child (SA-0MVX35AUC003CNUD) consumes the
check from ``hygiene_check.sh`` and ``implement.py start``.
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


# ---------------------------------------------------------------------------
# Repair (SA-0MVX359TI0069ECK) — safe, machine-readable --fix
# ---------------------------------------------------------------------------

#: Remote name assumed to own the long-lived branches (never guessed; a
#: missing remote refuses with an actionable message instead).
DEFAULT_REMOTE_NAME = "origin"


def _remote_configured(repo: str | os.PathLike[str], name: str) -> bool:
    """True when *name* is a configured remote (has a URL)."""
    proc = _run_git(repo, ["config", "--get", f"remote.{name}.url"])
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _remote_has_branch(
    repo: str | os.PathLike[str], name: str, branch: str
) -> bool:
    """True when *name* advertises ``refs/heads/<branch>``.

    Uses ``git ls-remote`` (no fetch, no checkout) so a local bare remote in
    a scratch test is just as deterministic as ``https://github.com/...`` and
    the guarantee is "the remote has the branch" — never a guess.
    """
    proc = _run_git(repo, ["ls-remote", name, f"refs/heads/{branch}"])
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def repair_branch_upstream(
    repo: str | os.PathLike[str],
    branch: str,
    *,
    remote_name: str = DEFAULT_REMOTE_NAME,
    dry_run: bool = False,
) -> dict[str, str | None]:
    """Repair *branch*'s self-referential upstream — config-only, safe.

    Restores the healthy shape ``branch.<name>.remote = <remote_name>`` /
    ``branch.<name>.merge = refs/heads/<branch>`` **only** when *remote_name*
    is a configured remote whose ``refs/heads/<branch>`` exists (verified with
    ``git ls-remote``). Any other state refuses with an actionable message and
    changes nothing — the remote is never guessed and the branch is never
    rewritten to a place the remote does not have.

    Safety (parent SA-0MVX1M6HH004YPX2 AC2):

    * never repairs a ``wl-*`` branch — there ``remote = "."`` is the
      legitimate worktree-ownership signal;
    * only writes ``branch.<name>.*`` config keys — never checks out, never
      touches a working tree;
    * ``dry_run=True`` returns the would-be outcome without writing anything.

    Args:
        repo: Repository to operate on.
        branch: Local branch name to repair.
        remote_name: The remote to point the branch at (default ``origin``).
        dry_run: Preview mode — no config is written.

    Returns:
        A per-branch result mapping with ``branch``, ``outcome``
        (``repaired`` | ``would-repair`` | ``refused`` | ``skipped``),
        ``remote_before``/``merge_before``, ``remote_after``/``merge_after``
        and an actionable ``message``.
    """
    result: dict[str, str | None] = {
        "branch": branch,
        "outcome": "skipped",
        "remote_before": None,
        "merge_before": None,
        "remote_after": None,
        "merge_after": None,
        "message": "",
    }
    if branch.startswith(WORKTREE_BRANCH_PREFIX):
        result["message"] = (
            f"branch {branch!r} is a wl-* worktree branch: remote='.' is the "
            "worktree-ownership signal and is never rewritten"
        )
        return result

    remote, merge = _tracking_config(_local_config(repo), branch)
    if remote != ".":
        result["message"] = (
            f"branch {branch!r} is not self-referential (remote={remote!r}); "
            "nothing to repair"
        )
        return result
    result["remote_before"] = remote
    result["merge_before"] = merge
    result["remote_after"] = remote_name
    result["merge_after"] = f"refs/heads/{branch}"

    if not _remote_configured(repo, remote_name):
        result["outcome"] = "refused"
        result["message"] = (
            f"cannot repair branch {branch!r}: remote {remote_name!r} is not "
            f"configured (git config --get remote.{remote_name}.url is empty). "
            "Add the remote first, e.g. `git remote add origin <url>`."
        )
        return result
    if not _remote_has_branch(repo, remote_name, branch):
        result["outcome"] = "refused"
        result["message"] = (
            f"cannot repair branch {branch!r}: remote {remote_name!r} does not "
            f"advertise refs/heads/{branch} (git ls-remote {remote_name} "
            f"refs/heads/{branch} is empty). Push the branch to {remote_name} "
            "first, or choose the remote that has it — the repair never "
            "rewrites to a non-existent branch."
        )
        return result

    if dry_run:
        result["outcome"] = "would-repair"
        result["message"] = (
            f"would set branch.{branch}.remote={remote_name} and "
            f"branch.{branch}.merge=refs/heads/{branch}"
        )
        return result

    remote_set = _run_git(
        repo, ["config", f"branch.{branch}.remote", remote_name]
    )
    merge_set = _run_git(
        repo, ["config", f"branch.{branch}.merge", f"refs/heads/{branch}"]
    )
    if remote_set.returncode != 0 or merge_set.returncode != 0:
        result["outcome"] = "refused"
        result["message"] = (
            f"failed to write branch.{branch} config (git config rc="
            f"{remote_set.returncode}/{merge_set.returncode})"
        )
        return result
    result["outcome"] = "repaired"
    result["message"] = (
        f"branch.{branch}.remote={remote_name}, "
        f"branch.{branch}.merge=refs/heads/{branch}"
    )
    return result


def repair_defects(
    repo: str | os.PathLike[str],
    *,
    remote_name: str = DEFAULT_REMOTE_NAME,
    dry_run: bool = False,
) -> list[dict[str, str | None]]:
    """Repair (or dry-run) every self-referential **defect** in *repo*.

    ``wl-*`` worktree branches are never touched (they are ``expected``, not
    defects; see :func:`repair_branch_upstream`). Returns one result mapping
    per defect, ordered by branch name.
    """
    findings = detect_self_referential_upstreams(repo)
    return [
        repair_branch_upstream(
            repo,
            str(finding["branch"]),
            remote_name=remote_name,
            dry_run=dry_run,
        )
        for finding in findings
        if finding.get("type") == "defect"
    ]


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="self_referential_upstream",
        description=(
            "Report local branches whose git tracking config resolves to the "
            "repository itself (branch.<name>.remote = '.') — the defect that "
            "silently breaks `git pull` on worktree machines."
        ),
        epilog=(
            "Exit codes: 0 = no defects (or all repaired); 1 = defects "
            "present or unrepaired; 2 = usage or repository error."
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
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Repair detected defects to remote=origin / merge=refs/heads/<b> "
            "when origin has the branch (refuses otherwise; never touches "
            "wl-* branches, never checks out)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --fix: preview the repair without writing any config.",
    )
    args = parser.parse_args(argv)

    try:
        repo = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path.cwd()
        if args.fix:
            repairs = repair_defects(repo, dry_run=args.dry_run)
            remaining = detect_self_referential_upstreams(repo)
            defects = _defects(remaining)
            message = ""
            if defects and not args.dry_run:
                message = (
                    f"{len(defects)} defect(s) could not be repaired (verified "
                    "against the origin remote); re-run with --json to inspect."
                )
            if args.dry_run and defects:
                message = (
                    "dry-run: no config written; repair preview above."
                )
            if args.json:
                payload = {
                    "repo": str(repo),
                    "ok": not defects,
                    "defect_count": len(defects),
                    "repairs": repairs,
                    "message": message,
                }
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for repair in repairs:
                    print(
                        f"[{repair['outcome']}] {repair['branch']}: "
                        f"{repair['message']}"
                    )
                if message:
                    print(message)
                print(
                    f"{len(defects)} defect(s) remaining in {repo}."
                )
            # 0 = nothing left to do; 1 = defects remain after repair;
            # 2 = environment error (remote missing entirely / not a repo).
            env_error = any(
                r["outcome"] == "refused" and "not configured" in (r.get("message") or "")
                for r in repairs
            )
            return 2 if env_error else (1 if defects else 0)
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
