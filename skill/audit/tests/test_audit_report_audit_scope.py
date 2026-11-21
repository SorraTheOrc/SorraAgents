"""Report-format contract: the persisted report records its ``## Audit Scope``.

AC4 (LP-0MWDDRV5U005GVHM): a report-format test asserting that the audit
report persisted for a work item carries a ``## Audit Scope`` section that
states **both** the resolved code root and the worklog root, so a reader can
tell which repository the evidence came from. The section must be emitted on
**every** persisted report, including runs where no explicit ``--code-root``
was passed (the default owning-project root is still recorded).

The tests drive the public ``cmd_issue`` entry point with ``persist=True`` and
capture the exact report text handed to ``persist_audit()`` — this proves the
section lands in the *persisted* report (not merely in stdout).

The contract is green since the implementation landed
(LP-0MWDDRWX6006YE0Z emitted the section; LP-0MWDDRVQ7005KOHN supplied the
cross-repo ``--code-root`` pump); the xfail markers were removed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner
from audit.tests.test_audit_runner_launch_context import (
    _CROSS_REPO_DESCRIPTION,
    _SAME_REPO_DESCRIPTION,
    _fake_pi_met,
    _make_cross_repo_pair,
    _make_minimal_runner,
    _make_same_repo_fixture,
)

#: The contract is green since the ``## Audit Scope`` report line landed
#: (LP-0MWDDRWX6006YE0Z) and the cross-repo code-root pump passed
#: (LP-0MWDDRVQ7005KOHN); the markers were removed.


def _path_mentioned(text: str, path: Path) -> bool:
    """True when *text* names *path* (either as given or resolved).

    The report may render a root exactly as supplied or after ``resolve()``
    (symlink / ``..`` normalisation), so accept either spelling.
    """
    return str(path) in text or str(path.resolve()) in text


def _audit_scope_section(report: str) -> str | None:
    """Return the body of the ``## Audit Scope`` section, or ``None``.

    The section runs from the ``## Audit Scope`` heading to the next ``## ``
    heading (or end of report). The heading match is whitespace-tolerant so a
    trailing space does not make the contract spuriously red.
    """
    lines = report.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == "## audit scope":
            body: list[str] = []
            for following in lines[index + 1:]:
                if following.startswith("## "):
                    break
                body.append(following)
            return "\n".join(body)
    return None


def _persisted_report(
    *, worklog_dir: Path, git_root: Path, description: str,
    code_root: str | None,
) -> tuple[int, list[tuple[str, str, dict]]]:
    """Run ``cmd_issue(persist=True)`` and capture ``persist_audit`` calls.

    Returns ``(rc, persist_calls)`` where each persist call is
    ``(issue_id, report, kwargs)``. The report argument is the exact text
    persisted to the worklog.
    """
    persist_calls: list[tuple[str, str, dict]] = []

    def fake_persist(issue_id, report, **kwargs):
        persist_calls.append((issue_id, report, kwargs))
        return 0

    base = _make_minimal_runner(
        description=description, git_cwd=git_root,
    )

    def runner(cmd):
        # Post-persist readback: ``wl audit-show OSL-1 --json`` must return a
        # stored audit referencing the item id, or ``cmd_issue`` reports a
        # readback failure and returns non-zero.
        if "audit-show" in " ".join(cmd):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "audit": {
                        "rawOutput": "Ready to close: Yes\n- OSL-1 --\n",
                        "auditedAt": "2026-01-01T00:00:00.000Z",
                    },
                }),
                stderr="",
            )
        return base(cmd)

    cmd_kwargs: dict = {
        "persist": True,
        "force": True,
        "runner": runner,
        "worklog_dir": str(worklog_dir),
    }
    if code_root is not None:
        cmd_kwargs["code_root"] = code_root

    # The launch/owning root is the worklog owner; the resolved git evidence
    # root may differ when an explicit code root is supplied.
    owning_root = worklog_dir.parent
    with (
        mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", owning_root),
        mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi_met,
        ),
        mock.patch.object(
            audit_runner, "persist_audit", side_effect=fake_persist,
        ),
        mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [], "fixes_applied": 0},
        ),
    ):
        rc = audit_runner.cmd_issue("OSL-1", **cmd_kwargs)
    return rc, persist_calls


class TestPersistedReportAuditScope:
    """AC4 (LP-0MWDDRV5U005GVHM): the persisted report states its audit scope.
    """

    def test_cross_repo_report_records_code_root_and_worklog(self, tmp_path):
        """A cross-repo item audited with an explicit ``--code-root`` persists
        a report whose ``## Audit Scope`` section names both the code root
        (repo B) and the worklog root (repo A) — the two are distinguishable,
        so the section cannot be satisfied by only one of them.
        """
        worklog_a, repo_a, repo_b = _make_cross_repo_pair(tmp_path)
        rc, persist_calls = _persisted_report(
            worklog_dir=worklog_a,
            git_root=repo_b,
            description=_CROSS_REPO_DESCRIPTION,
            code_root=str(repo_b),
        )

        assert rc == 0, "cross-repo audit with --code-root must exit green"
        assert persist_calls, "the run must persist exactly one report"
        _issue_id, report, _kwargs = persist_calls[-1]

        # The section exists and names BOTH roots. ``repo_b`` is the code
        # root; ``repo_a`` can only come from the worklog side, so asserting
        # both proves the section is not code-root-only.
        section = _audit_scope_section(report)
        assert section is not None, (
            "persisted report must contain a '## Audit Scope' section:\n"
            + report
        )
        assert _path_mentioned(section, repo_b), (
            f"Audit Scope must name the code root {repo_b}: {section!r}"
        )
        assert _path_mentioned(section, repo_a), (
            f"Audit Scope must name the worklog root {repo_a}: {section!r}"
        )

        # The cross-repo test keeps no marker: it turned green with the
        # cross-repo --code-root wiring (LP-0MWDDRVQ7005KOHN).
        assert "Ready to close:" in report
        assert "OSL-1" in report

    def test_default_report_records_owning_root_as_scope(self, tmp_path):
        """With **no** explicit code root the section is still emitted — the
        default owning-project root is recorded as the code root (AC4's
        "every persisted report" clause).
        """
        worklog_dir, repo = _make_same_repo_fixture(tmp_path)
        rc, persist_calls = _persisted_report(
            worklog_dir=worklog_dir,
            git_root=repo,
            description=_SAME_REPO_DESCRIPTION,
            code_root=None,
        )

        assert rc == 0, "same-repo audit without --code-root must exit green"
        assert persist_calls, "the run must persist exactly one report"
        _issue_id, report, _kwargs = persist_calls[-1]

        section = _audit_scope_section(report)
        assert section is not None, (
            "persisted report must emit '## Audit Scope' even with no "
            "explicit code root:\n" + report
        )
        assert _path_mentioned(section, repo), (
            f"Audit Scope must record the default owning code root {repo}: "
            f"{section!r}"
        )
