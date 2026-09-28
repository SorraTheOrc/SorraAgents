#!/usr/bin/env python3
"""Git-identity guard that runs before ``wl sync`` (SA-0MUJ2VMF7000UHCX).

The Worklog author-identity gate refuses to merge incoming ``refs/worklog``
commits whose author email does not match the store's configured
``user.email``. When the shared checkout's identity is left as an incident
artefact — e.g. the 2026-09-26 leaked-``GIT_DIR`` incident rewrote
``.git/config`` to ``T <t@t.com>`` — ``wl sync`` refuses and Worklog data is
silently stranded.

This guard detects such a mismatch *before* the sync runs and prints an
actionable remedy, so the failure is visible rather than a cryptic refusal.
It is deliberately read-only: it never rewrites git config.

Detection rules (first match wins per rule; all are reported):

- ``core.bare`` is true — the leaked ``git init --bare`` signature.
- ``user.email`` is unset — the gate cannot match anything.
- ``user.email`` matches a known incident-era sentinel (e.g. ``t@t.com``).
- an explicit expected email is configured and differs;
- otherwise a *local* ``user.email`` override differs from the global identity
  (a local override is exactly what the incident wrote).

Any other combination is a pass. Using only a global identity, or a local
override that equals the global one, is healthy. The guard is fail-open on a
non-git root.

CLI::

    python3 scripts/check_git_identity.py [--repo-root PATH]
        [--expected-email EMAIL] [--json]

Exit codes: ``0`` OK, ``1`` identity mismatch detected, ``2`` usage error.

The expected email may also come from ``WORKLOG_EXPECTED_EMAIL`` or the
``worklog.expectedEmail`` git config key. Related: ``docs/dev/test-isolation.md``
§9.4 step 4.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Emails observed from the leaked-``GIT_DIR`` incident (2026-09-24/26) and its
#: fixture helpers. Kept lowercase for comparison.
INCIDENT_ERA_EMAILS = frozenset(
    {"t@t.com", "t@t", "test@test.com", "test@test", "cache test@test.com"}
)

#: Environment variable carrying the canonical identity to compare against.
ENV_EXPECTED_EMAIL = "WORKLOG_EXPECTED_EMAIL"

#: Git config key carrying the canonical identity to compare against.
CONFIG_EXPECTED_EMAIL = "worklog.expectedEmail"

#: Work item that owns this guard.
WORK_ITEM_ID = "SA-0MUJ2VMF7000UHCX"


@dataclass(frozen=True)
class IdentityState:
    """Raw identity values read (read-only) from a repository."""

    repo_root: str
    core_bare: bool
    effective_name: str | None
    effective_email: str | None
    local_name: str | None
    local_email: str | None
    global_name: str | None
    global_email: str | None
    expected_email: str | None


@dataclass(frozen=True)
class IdentityReport:
    """Decision over an :class:`IdentityState`."""

    state: IdentityState
    failures: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


# ── Read-only git plumbing ──────────────────────────────────────────────────


def _git(repo_root: str | os.PathLike[str], args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None


def _config(repo_root: str | os.PathLike[str], key: str, scope: str | None = None) -> str | None:
    """Return a git config value, or ``None`` when unset/absent/unreadable."""
    args = ["config"]
    if scope:
        args.append(scope)
    args += ["--get", key]
    proc = _git(repo_root, args)
    if proc is None or proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _is_git_repo(repo_root: str | os.PathLike[str]) -> bool:
    proc = _git(repo_root, ["rev-parse", "--git-dir"])
    return proc is not None and proc.returncode == 0


def _is_bare(repo_root: str | os.PathLike[str]) -> bool:
    proc = _git(repo_root, ["rev-parse", "--is-bare-repository"])
    if proc is None or proc.returncode != 0:
        return False
    return proc.stdout.strip() == "true"


def resolve_expected_email(
    repo_root: str | os.PathLike[str], cli_value: str | None = None
) -> str | None:
    """Resolve the canonical email: CLI → env → git config."""
    if cli_value and cli_value.strip():
        return cli_value.strip()
    env_value = os.environ.get(ENV_EXPECTED_EMAIL)
    if env_value and env_value.strip():
        return env_value.strip()
    config_value = _config(repo_root, CONFIG_EXPECTED_EMAIL)
    if config_value:
        return config_value
    return None


def read_identity_state(
    repo_root: str | os.PathLike[str], expected_email: str | None = None
) -> IdentityState:
    """Read the effective/local/global identity without mutating anything."""
    return IdentityState(
        repo_root=str(repo_root),
        core_bare=_is_bare(repo_root),
        effective_name=_config(repo_root, "user.name"),
        effective_email=_config(repo_root, "user.email"),
        local_name=_config(repo_root, "user.name", "--local"),
        local_email=_config(repo_root, "user.email", "--local"),
        global_name=_config(repo_root, "user.name", "--global"),
        global_email=_config(repo_root, "user.email", "--global"),
        expected_email=expected_email,
    )


# ── Pure decision logic ─────────────────────────────────────────────────────


def _norm(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    return value or None


def evaluate_identity(state: IdentityState) -> IdentityReport:
    """Decide whether *state* is a healthy identity (pure + deterministic)."""
    failures: list[str] = []
    warnings: list[str] = []

    effective = _norm(state.effective_email)
    local = _norm(state.local_email)
    glob = _norm(state.global_email)
    expected = _norm(state.expected_email)

    if state.core_bare:
        failures.append("core-bare")
    if not effective:
        failures.append("empty-email")
    if effective in INCIDENT_ERA_EMAILS:
        failures.append("incident-identity")
    elif local in INCIDENT_ERA_EMAILS:
        # A stale incident-era local override is a failure even if it is
        # somehow shadowed in the effective value.
        failures.append("incident-identity")

    if expected:
        if effective and effective != expected:
            failures.append("expected-mismatch")
    else:
        if local and glob and local != glob:
            failures.append("local-override")
        if not glob:
            warnings.append("no-global-identity")

    return IdentityReport(state=state, failures=tuple(failures), warnings=tuple(warnings))


def check_git_identity(
    repo_root: str | os.PathLike[str], expected_email: str | None = None
) -> IdentityReport:
    """Read and evaluate the identity of *repo_root*."""
    return evaluate_identity(read_identity_state(repo_root, expected_email))


# ── Reporting ───────────────────────────────────────────────────────────────


def format_report(report: IdentityReport, command: str = "wl sync") -> str:
    """Render a human-readable, actionable report."""
    state = report.state
    lines: list[str] = []
    if report.ok:
        email = state.effective_email or "(unset)"
        lines.append(f"OK: git identity is consistent (user.email={email}).")
        for warning in report.warnings:
            lines.append(f"  note: {warning}")
        return "\n".join(lines)

    lines.append(
        f"ERROR: git identity mismatch detected — `{command}`'s author-identity "
        "gate may refuse to merge Worklog data."
    )
    lines.append("")
    lines.append(f"  configured user.email : {state.effective_email or '(unset)'}")
    lines.append(f"  global user.email     : {state.global_email or '(unset)'}")
    if state.local_email is not None:
        lines.append(f"  local override        : {state.local_email}")
    if state.expected_email:
        lines.append(f"  expected user.email   : {state.expected_email}")
    lines.append(f"  reasons               : {', '.join(report.failures)}")
    if "empty-email" in report.failures:
        lines.append("  git user.email is not configured in this checkout.")
    lines.append("")
    lines.append("Remedy:")
    lines.append("  # restore the canonical (global) identity in this checkout:")
    lines.append("  git config --local --unset-all user.email")
    lines.append("  git config --local --unset-all user.name")
    lines.append("  # verify:")
    lines.append("  git config user.name && git config user.email")
    lines.append("  # if this checkout legitimately needs a different identity, align the")
    lines.append("  # local override with the canonical value instead.")
    lines.append("")
    lines.append(f"See {WORK_ITEM_ID} and docs/dev/test-isolation.md §9.4 step 4.")
    return "\n".join(lines)


def report_to_dict(report: IdentityReport) -> dict:
    """Machine-readable report (JSON output)."""
    state = report.state
    return {
        "ok": report.ok,
        "reasons": list(report.failures),
        "warnings": list(report.warnings),
        "repo_root": state.repo_root,
        "core_bare": state.core_bare,
        "user_name": state.effective_name,
        "user_email": state.effective_email,
        "local_name": state.local_name,
        "local_email": state.local_email,
        "global_name": state.global_name,
        "global_email": state.global_email,
        "expected_email": state.expected_email,
    }


def _default_repo_root() -> Path:
    proc = _git(os.getcwd(), ["rev-parse", "--show-toplevel"])
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path(os.getcwd())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detect a mismatched git user.email before `wl sync` runs "
            f"({WORK_ITEM_ID})."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root to inspect (default: current checkout).",
    )
    parser.add_argument(
        "--expected-email",
        default=None,
        help=(
            "Canonical identity to compare against (falls back to "
            f"${ENV_EXPECTED_EMAIL} then {CONFIG_EXPECTED_EMAIL})."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root) if args.repo_root else _default_repo_root()

    if not _is_git_repo(repo_root):
        # Fail-open: nothing to check outside a repository.
        if args.json:
            print(json.dumps({"ok": True, "reasons": [], "warnings": ["not-a-git-repo"]}))
        return 0

    expected = resolve_expected_email(repo_root, args.expected_email)
    report = check_git_identity(repo_root, expected)

    if args.json:
        print(json.dumps(report_to_dict(report)))
    else:
        print(format_report(report))

    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
