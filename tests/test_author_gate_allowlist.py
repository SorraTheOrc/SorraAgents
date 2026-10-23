#!/usr/bin/env python3
"""Regression tests for the ``wl sync`` author-identity allow-list.

Covers SA-0MVB57M02008QPJS (child SA-0MVBD5MXS007WJT0). The 2026-10-15
incident: worklog sync commits authored ``pi@local`` (the host framework
identity used by agent checkouts) failed the sync author-identity gate, so
``wl sync`` refused to merge inbound data and every local worklog mutation was
stranded. The operator decision was to add ``pi@local`` to the project's
``syncAllowedAuthors`` allow-list (``.worklog/config.yaml``) while keeping the
gate exact-email: arbitrary foreign emails and empty author emails stay
refused.

These tests pin the *resolved* contract by driving the **real** exported gate
(``checkAuthorIdentity`` from the installed worklog package) — never a
reimplementation of its decision logic:

* ``pi@local`` passes when (and only when) it is in the allow-list.
* An arbitrary foreign email is still refused.
* An empty author email is refused even with ``allowForeignAuthor``.
* Incident-era sentinel identities (``t@t.com`` et al., the leaked-``GIT_DIR``
  incident) stay refused and are never allow-listed (SA-0MV9JVLBQ0090RNC AC4).

Everything is hermetic: the gate is a pure function over the commit list and
the options, so no git repository, remote, or real ``wl sync`` is involved.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / ".worklog" / "config.yaml"

#: Host framework identity used by agent checkouts — the incident author.
PI_LOCAL = "pi@local"
#: The operator's GitHub identity, the project's canonical author.
OPERATOR_IDENTITY = "250240+SorraTheOrc@users.noreply.github.com"
#: A local ``user.email`` that is deliberately *not* in the allow-list, so a
#: passing ``pi@local`` commit can only pass because of the allow-list.
UNLISTED_EXPECTED_EMAIL = "local-checkout@example.invalid"

#: Runs the real gate in Node and prints its decision as JSON. The module
#: path and the payload are passed through the environment so no shell quoting
#: of commit data is involved.
_GATE_SCRIPT = """
const { pathToFileURL } = await import('node:url');
const module = await import(pathToFileURL(process.env.AUTHOR_GATE_MODULE).href);
if (typeof module.checkAuthorIdentity !== 'function') {
  console.error('author gate module does not export checkAuthorIdentity');
  process.exit(3);
}
const payload = JSON.parse(process.env.AUTHOR_GATE_PAYLOAD);
process.stdout.write(JSON.stringify(
  module.checkAuthorIdentity(payload.commits, payload.options),
));
"""


@lru_cache(maxsize=1)
def _gate_module() -> Path:
    """Resolve the installed worklog ``sync.js`` that exports the gate.

    Resolution order: ``AUTHOR_GATE_MODULE`` override, then the ``wl``
    executable on ``PATH`` (its realpath sibling ``sync.js``). Tests skip
    with an actionable reason when neither is available rather than
    hard-coding a global install path.
    """
    override = os.environ.get("AUTHOR_GATE_MODULE")
    if override:
        candidate = Path(override)
    else:
        wl_bin = shutil.which("wl")
        if wl_bin is None:
            pytest.skip("wl executable not on PATH; cannot resolve the real author gate")
        candidate = Path(wl_bin).resolve().parent / "sync.js"
    if not candidate.is_file():
        pytest.skip(f"worklog author gate module not found at {candidate}")
    return candidate


def _project_allowlist() -> list[str]:
    """Read the project's real ``syncAllowedAuthors`` from the repo config."""
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return config.get("syncAllowedAuthors")


def _run_gate(
    commits: list[dict[str, str]],
    *,
    allowed_authors: object,
    expected_author_email: str = UNLISTED_EXPECTED_EMAIL,
    allow_foreign_author: bool = False,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Drive the real ``checkAuthorIdentity`` and return its decision.

    The subprocess inherits a scrubbed environment: every ``GIT_*`` variable
    is dropped (they would redirect git at a fixture), so a gate that secretly
    shelled out to git could not silently pass.
    """
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node executable not on PATH; cannot drive the real author gate")

    payload = {
        "commits": commits,
        "options": {
            "remoteTrackingRef": "refs/worklog/remotes/origin/worklog/data",
            "expectedAuthorEmail": expected_author_email,
            "allowForeignAuthor": allow_foreign_author,
            "allowedAuthors": allowed_authors,
        },
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env["AUTHOR_GATE_MODULE"] = str(_gate_module())
    env["AUTHOR_GATE_PAYLOAD"] = json.dumps(payload)
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [node_bin, "--input-type=module", "-e", _GATE_SCRIPT],
        cwd=str(cwd) if cwd else str(Path(os.path.abspath(os.sep))),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, (
        f"author gate harness failed (exit {result.returncode}): "
        f"{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_project_allowlist_contains_pi_local_and_operator_identity():
    """AC2: the project's real allow-list carries both required identities.

    Red before the ``pi@local`` config change, green after: without the entry
    the incident author is refused by the gate (see
    ``test_pi_local_is_refused_without_the_allowlist_entry``).
    """
    allowlist = _project_allowlist()

    assert isinstance(allowlist, list), (
        f"syncAllowedAuthors must stay an explicit list, got {allowlist!r}"
    )
    assert PI_LOCAL in allowlist
    assert OPERATOR_IDENTITY in allowlist


def test_project_allowlist_is_exact_email_not_allow_all():
    """AC1: widening the gate keeps it exact-email — no wildcard/allow-all."""
    allowlist = _project_allowlist()

    assert all(isinstance(entry, str) and entry.strip() for entry in allowlist), (
        f"allow-list entries must be non-empty email strings, got {allowlist!r}"
    )
    assert "*" not in allowlist
    assert not any(entry.strip() == "@" for entry in allowlist)


def test_pi_local_authored_commit_passes_the_project_allowlist():
    """AC1: a ``pi@local``-authored commit is merged instead of stranding sync."""
    decision = _run_gate(
        [{"hash": "66b8e59b", "authorEmail": PI_LOCAL}],
        allowed_authors=_project_allowlist(),
    )

    assert decision["allowed"] is True, decision["message"]
    assert decision["violations"] == []
    assert decision["message"] == ""


def test_arbitrary_foreign_email_is_still_refused():
    """AC1: the allow-list does not weaken the gate for unknown authors."""
    decision = _run_gate(
        [{"hash": "deadbeef", "authorEmail": "evil@example.com"}],
        allowed_authors=_project_allowlist(),
    )

    assert decision["allowed"] is False
    assert [v["reason"] for v in decision["violations"]] == ["foreign-email"]
    assert decision["violations"][0]["authorEmail"] == "evil@example.com"
    assert "evil@example.com" in decision["message"]


def test_pi_local_is_refused_without_the_allowlist_entry():
    """AC1 (red-before evidence): the allow-list entry is what admits pi@local.

    With the pre-fix allow-list (operator identity only) the very same commit
    is refused, so the regression test would fail if the ``pi@local`` entry
    were ever removed.
    """
    decision = _run_gate(
        [{"hash": "66b8e59b", "authorEmail": PI_LOCAL}],
        allowed_authors=[OPERATOR_IDENTITY],
    )

    assert decision["allowed"] is False
    assert [v["reason"] for v in decision["violations"]] == ["foreign-email"]


def test_empty_author_email_is_refused_even_with_allow_foreign_author():
    """AC1: the empty-email gate is never bypassed by the escape hatch."""
    decision = _run_gate(
        [{"hash": "cafebabe", "authorEmail": ""}],
        allowed_authors=_project_allowlist(),
        allow_foreign_author=True,
    )

    assert decision["allowed"] is False
    assert [v["reason"] for v in decision["violations"]] == ["empty-email"]


def test_gate_decision_needs_no_git_repository(tmp_path):
    """AC3: the pinned decision is hermetic — no git repo, remote, or sync.

    Runs the gate from an empty temporary directory with every ``GIT_*``
    variable pointed at non-existent paths; a gate that reached for git would
    fail instead of returning a decision.
    """
    decision = _run_gate(
        [{"hash": "66b8e59b", "authorEmail": PI_LOCAL}],
        allowed_authors=_project_allowlist(),
        cwd=tmp_path,
        extra_env={
            "GIT_DIR": str(tmp_path / "no-such-dir"),
            "GIT_WORK_TREE": str(tmp_path / "no-such-worktree"),
        },
    )

    assert decision["allowed"] is True, decision["message"]


def _incident_sentinel_emails() -> frozenset[str]:
    """Return ``INCIDENT_ERA_EMAILS`` from ``scripts/check_git_identity.py``.

    Imported from the identity guard so the quarantine list has a single
    source of truth (a duplicated literal could drift from the guard).
    """
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import check_git_identity

    return check_git_identity.INCIDENT_ERA_EMAILS


def test_incident_sentinel_emails_are_absent_from_the_project_allowlist():
    """AC4: the quarantine is preserved — no incident sentinel is whitelisted.

    ``t@t.com`` (and the other leaked-``GIT_DIR`` sentinels) must stay out of
    ``syncAllowedAuthors``: whitelisting one would silently admit incident-era
    worklog history the gate is designed to refuse.
    """
    allowlist = {entry.strip().lower() for entry in _project_allowlist()}
    sentinels = _incident_sentinel_emails()

    assert not (allowlist & set(sentinels)), (
        f"incident-era sentinel emails must never be allow-listed, found "
        f"{sorted(allowlist & set(sentinels))} in {sorted(allowlist)}"
    )


def test_incident_sentinel_authored_commit_is_refused_by_the_project_allowlist():
    """AC4: the real gate refuses a ``t@t.com`` commit on this allow-list.

    Complements ``test_arbitrary_foreign_email_is_still_refused``: that test
    would keep passing if ``t@t.com`` were added to the allow-list, whereas
    this one fails — it pins the quarantine itself, not just "some foreign
    email is refused".
    """
    decision = _run_gate(
        [{"hash": "345ea15", "authorEmail": "t@t.com"}],
        allowed_authors=_project_allowlist(),
    )

    assert decision["allowed"] is False
    assert [v["reason"] for v in decision["violations"]] == ["foreign-email"]
    assert decision["violations"][0]["authorEmail"] == "t@t.com"

    # Red-before evidence: the very same commit IS admitted when a sentinel is
    # (wrongly) allow-listed, so this test fails if the quarantine is broken —
    # it is not vacuously green because "some foreign email" is refused.
    sentinel_allowlisted = _run_gate(
        [{"hash": "345ea15", "authorEmail": "t@t.com"}],
        allowed_authors=["t@t.com"],
    )
    assert sentinel_allowlisted["allowed"] is True
