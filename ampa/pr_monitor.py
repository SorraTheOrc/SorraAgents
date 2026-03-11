"""PR monitor scheduled command.

Periodically enumerates all open pull requests in the repository, checks
their CI status (required check runs / statuses), and takes action:

* **All required checks passing** — post a Worklog comment and a GitHub
  PR comment indicating the PR is "ready for review" (only once per PR
  to avoid noise).
* **Required checks failing** — post a Worklog comment and create a
  critical Worklog work item linking to the PR and failing checks.

The command uses the ``gh`` CLI for GitHub API access.  If ``gh`` is not
available the runner logs a clear error and exits gracefully.

Configuration
-------------
Behaviour is driven from ``CommandSpec.metadata``:

* ``dedup`` (bool, default ``True``) — when true the runner will not
  re-post a "ready for review" comment if one already exists on the PR.
* ``max_prs`` (int, default ``50``) — maximum number of open PRs to
  evaluate per run (to avoid hitting API rate limits).
* ``gh_command`` (str, default ``"gh"``) — path or name of the ``gh``
  CLI binary.

The command does **not** require LLM availability.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

LOG = logging.getLogger("ampa.pr_monitor")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_PRS: int = 50
_DEFAULT_GH_COMMAND: str = "gh"
_READY_COMMENT_MARKER: str = "<!-- ampa-pr-monitor:ready -->"
_FAILURE_COMMENT_MARKER: str = "<!-- ampa-pr-monitor:failure -->"


# ---------------------------------------------------------------------------
# PRMonitorRunner
# ---------------------------------------------------------------------------


class PRMonitorRunner:
    """Run the PR monitor scheduled command.

    Parameters
    ----------
    run_shell:
        Callable with the same signature as :func:`subprocess.run`.
    command_cwd:
        Working directory for shell commands.
    notifier:
        Object with a ``notify(title, body, message_type)`` method used
        for Discord notifications.
    wl_shell:
        Optional separate callable for ``wl`` commands.  Defaults to
        *run_shell*.
    """

    def __init__(
        self,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str,
        notifier: Optional[Any] = None,
        wl_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ) -> None:
        self.run_shell = run_shell
        self.command_cwd = command_cwd
        self._notifier = notifier
        self._wl_shell = wl_shell or run_shell

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, spec: Any) -> Dict[str, Any]:
        """Execute one PR-monitor cycle.

        1. Verify ``gh`` CLI is available.
        2. List open PRs via ``gh pr list``.
        3. For each PR, check CI status via ``gh pr checks``.
        4. Post comments / create work items as appropriate.
        5. Return a result dict summarising the run.

        Returns
        -------
        dict
            Keys:
            * ``action`` — ``"completed"``, ``"no_prs"``, ``"gh_unavailable"``,
              ``"list_failed"``
            * ``prs_checked`` — number of PRs evaluated
            * ``ready_prs`` — list of PR numbers marked ready
            * ``failing_prs`` — list of PR numbers with failing CI
            * ``skipped_prs`` — list of PR numbers skipped (already notified)
            * ``note`` — human-readable summary
        """
        metadata: Dict[str, Any] = getattr(spec, "metadata", {}) or {}
        gh_cmd = str(metadata.get("gh_command", _DEFAULT_GH_COMMAND))
        dedup = _coerce_bool(metadata.get("dedup", True))
        try:
            max_prs = int(metadata.get("max_prs", _DEFAULT_MAX_PRS))
        except (TypeError, ValueError):
            max_prs = _DEFAULT_MAX_PRS

        # 1. Check gh availability
        if not self._gh_available(gh_cmd):
            note = "pr-monitor: gh CLI not available — aborting"
            LOG.error(note)
            return {
                "action": "gh_unavailable",
                "prs_checked": 0,
                "ready_prs": [],
                "failing_prs": [],
                "skipped_prs": [],
                "note": note,
            }

        # 2. List open PRs
        prs = self._list_open_prs(gh_cmd, max_prs)
        if prs is None:
            note = "pr-monitor: failed to list open PRs"
            LOG.error(note)
            return {
                "action": "list_failed",
                "prs_checked": 0,
                "ready_prs": [],
                "failing_prs": [],
                "skipped_prs": [],
                "note": note,
            }
        if not prs:
            note = "pr-monitor: no open PRs found"
            LOG.info(note)
            return {
                "action": "no_prs",
                "prs_checked": 0,
                "ready_prs": [],
                "failing_prs": [],
                "skipped_prs": [],
                "note": note,
            }

        # 3. Evaluate each PR
        ready_prs: List[int] = []
        failing_prs: List[int] = []
        skipped_prs: List[int] = []

        for pr in prs:
            pr_number = pr.get("number")
            if pr_number is None:
                continue
            pr_number = int(pr_number)
            pr_title = pr.get("title", f"PR #{pr_number}")
            pr_url = pr.get("url", "")

            check_status = self._get_check_status(gh_cmd, pr_number)
            if check_status is None:
                LOG.warning(
                    "pr-monitor: could not retrieve check status for PR #%d",
                    pr_number,
                )
                continue

            all_passing, failing_checks, pending_checks = check_status

            if pending_checks and not failing_checks:
                # Checks still running — skip this PR for now
                LOG.info(
                    "pr-monitor: PR #%d has pending checks — skipping",
                    pr_number,
                )
                skipped_prs.append(pr_number)
                continue

            if all_passing:
                # Check for dedup — has a ready comment been posted already?
                if dedup and self._has_existing_comment(
                    gh_cmd, pr_number, _READY_COMMENT_MARKER
                ):
                    LOG.info(
                        "pr-monitor: PR #%d already marked ready — skipping",
                        pr_number,
                    )
                    skipped_prs.append(pr_number)
                    continue

                self._handle_ready_pr(
                    gh_cmd, pr_number, pr_title, pr_url
                )
                ready_prs.append(pr_number)
            elif failing_checks:
                self._handle_failing_pr(
                    gh_cmd, pr_number, pr_title, pr_url, failing_checks
                )
                failing_prs.append(pr_number)

        note = (
            f"pr-monitor: checked {len(prs)} PR(s) — "
            f"{len(ready_prs)} ready, {len(failing_prs)} failing, "
            f"{len(skipped_prs)} skipped"
        )
        LOG.info(note)

        # Send summary notification
        self._notify_summary(ready_prs, failing_prs, skipped_prs, len(prs))

        return {
            "action": "completed",
            "prs_checked": len(prs),
            "ready_prs": ready_prs,
            "failing_prs": failing_prs,
            "skipped_prs": skipped_prs,
            "note": note,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _gh_available(self, gh_cmd: str) -> bool:
        """Return True if the gh CLI is available."""
        try:
            proc = self.run_shell(
                f"{gh_cmd} --version",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            return proc.returncode == 0
        except Exception:
            LOG.exception("pr-monitor: exception checking gh availability")
            return False

    def _list_open_prs(
        self, gh_cmd: str, max_prs: int
    ) -> Optional[List[Dict[str, Any]]]:
        """List open PRs using gh CLI.  Returns None on failure."""
        try:
            cmd = (
                f"{gh_cmd} pr list --state open --json number,title,url,headRefName "
                f"--limit {max_prs}"
            )
            proc = self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
        except Exception:
            LOG.exception("pr-monitor: exception listing open PRs")
            return None

        if proc.returncode != 0:
            LOG.warning(
                "pr-monitor: gh pr list failed rc=%s stderr=%r",
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return None

        stdout = (proc.stdout or "").strip()
        if not stdout:
            return []

        try:
            data = json.loads(stdout)
            if isinstance(data, list):
                return data
            return []
        except Exception:
            LOG.warning(
                "pr-monitor: gh pr list returned invalid JSON: %r",
                stdout[:512],
            )
            return None

    def _get_check_status(
        self, gh_cmd: str, pr_number: int
    ) -> Optional[Tuple[bool, List[str], List[str]]]:
        """Get check status for a PR.

        Returns ``(all_passing, failing_check_names, pending_check_names)``
        or None on failure.
        """
        try:
            cmd = (
                f"{gh_cmd} pr checks {pr_number} "
                f"--json name,state,conclusion --required"
            )
            proc = self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
        except Exception:
            LOG.exception(
                "pr-monitor: exception checking status for PR #%d", pr_number
            )
            return None

        # gh pr checks returns exit code 1 when checks are failing, so we
        # must parse stdout regardless of returncode.
        stdout = (proc.stdout or "").strip()
        if not stdout:
            # No checks configured — treat as all passing
            if proc.returncode == 0:
                return (True, [], [])
            return None

        try:
            checks = json.loads(stdout)
        except Exception:
            LOG.warning(
                "pr-monitor: invalid JSON from gh pr checks for PR #%d: %r",
                pr_number,
                stdout[:512],
            )
            return None

        if not isinstance(checks, list):
            return None

        failing: List[str] = []
        pending: List[str] = []

        for check in checks:
            state = str(check.get("state", "")).upper()
            conclusion = str(check.get("conclusion", "")).upper()
            name = check.get("name", "(unknown)")

            if state == "COMPLETED" or state == "SUCCESS":
                if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED", ""):
                    # If state is SUCCESS with no conclusion, it's passing
                    if state == "SUCCESS":
                        continue
                    # COMPLETED with a passing conclusion
                    if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                        continue
                    # COMPLETED with empty conclusion — ambiguous, treat as pass
                    if not conclusion or conclusion == "NONE":
                        continue
                # COMPLETED but with failure/error conclusion
                if conclusion in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
                    failing.append(name)
                    continue
                # Unknown conclusion for COMPLETED — treat as pass
                continue
            elif state in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"):
                pending.append(name)
            elif state in ("FAILURE", "ERROR"):
                failing.append(name)
            else:
                # Unknown state — log and skip
                LOG.debug(
                    "pr-monitor: unknown check state=%r conclusion=%r for %s on PR #%d",
                    state,
                    conclusion,
                    name,
                    pr_number,
                )

        all_passing = len(failing) == 0 and len(pending) == 0
        return (all_passing, failing, pending)

    def _has_existing_comment(
        self, gh_cmd: str, pr_number: int, marker: str
    ) -> bool:
        """Check whether a comment with the given marker already exists on the PR."""
        try:
            cmd = f"{gh_cmd} pr view {pr_number} --json comments"
            proc = self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                return False
            stdout = (proc.stdout or "").strip()
            if not stdout:
                return False
            data = json.loads(stdout)
            comments = data.get("comments", [])
            for c in comments:
                body = c.get("body", "")
                if marker in body:
                    return True
            return False
        except Exception:
            LOG.exception(
                "pr-monitor: error checking existing comments on PR #%d",
                pr_number,
            )
            return False

    def _handle_ready_pr(
        self,
        gh_cmd: str,
        pr_number: int,
        pr_title: str,
        pr_url: str,
    ) -> None:
        """Post 'ready for review' comments on GitHub and Worklog."""
        LOG.info("pr-monitor: PR #%d (%s) — all checks passing", pr_number, pr_title)

        # Post GitHub PR comment
        comment_body = (
            f"{_READY_COMMENT_MARKER}\n"
            f"## All CI checks are passing\n\n"
            f"This PR is **ready for review**.\n\n"
            f"_Posted automatically by AMPA PR Monitor._"
        )
        self._post_gh_comment(gh_cmd, pr_number, comment_body)

        # Post Worklog comment (on any work item linked to this PR branch)
        wl_comment = (
            f"PR #{pr_number} ({pr_title}) — all CI checks passing, "
            f"ready for review. URL: {pr_url}"
        )
        self._post_wl_comment(pr_number, pr_title, wl_comment)

        # Send Discord notification
        try:
            if self._notifier is not None:
                self._notifier.notify(
                    title=f"PR #{pr_number} ready for review",
                    body=f"**{pr_title}**\nAll required checks are passing.\n{pr_url}",
                    message_type="command",
                )
        except Exception:
            LOG.exception(
                "pr-monitor: failed to send ready notification for PR #%d",
                pr_number,
            )

    def _handle_failing_pr(
        self,
        gh_cmd: str,
        pr_number: int,
        pr_title: str,
        pr_url: str,
        failing_checks: List[str],
    ) -> None:
        """Create critical work item and post comments for failing PR."""
        LOG.warning(
            "pr-monitor: PR #%d (%s) — %d check(s) failing: %s",
            pr_number,
            pr_title,
            len(failing_checks),
            ", ".join(failing_checks),
        )

        checks_str = ", ".join(failing_checks)

        # Create a critical Worklog work item
        wl_title = f"CI failing on PR #{pr_number}: {pr_title}"
        wl_desc = (
            f"The following required checks are failing on PR #{pr_number} "
            f"({pr_title}):\n\n"
            f"- {chr(10).join('- ' + c for c in failing_checks) if len(failing_checks) > 1 else failing_checks[0]}\n\n"
            f"PR URL: {pr_url}\n\n"
            f"discovered-from:SA-0MMJY1K3W15RI0F4\n\n"
            f"_Created automatically by AMPA PR Monitor._"
        )
        self._create_critical_work_item(wl_title, wl_desc)

        # Post GitHub PR comment about failure
        comment_body = (
            f"{_FAILURE_COMMENT_MARKER}\n"
            f"## CI checks are failing\n\n"
            f"The following required checks are failing:\n"
            f"{''.join('- ' + c + chr(10) for c in failing_checks)}\n"
            f"A critical work item has been created to track this.\n\n"
            f"_Posted automatically by AMPA PR Monitor._"
        )
        self._post_gh_comment(gh_cmd, pr_number, comment_body)

        # Send Discord notification
        try:
            if self._notifier is not None:
                self._notifier.notify(
                    title=f"CI failing on PR #{pr_number}",
                    body=(
                        f"**{pr_title}**\n"
                        f"Failing checks: {checks_str}\n"
                        f"{pr_url}"
                    ),
                    message_type="error",
                )
        except Exception:
            LOG.exception(
                "pr-monitor: failed to send failure notification for PR #%d",
                pr_number,
            )

    def _post_gh_comment(
        self, gh_cmd: str, pr_number: int, body: str
    ) -> bool:
        """Post a comment on a GitHub PR.  Returns True on success."""
        try:
            proc = self.run_shell(
                [gh_cmd, "pr", "comment", str(pr_number), "--body", body],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                LOG.warning(
                    "pr-monitor: gh pr comment failed for PR #%d: rc=%s stderr=%r",
                    pr_number,
                    proc.returncode,
                    (proc.stderr or "")[:512],
                )
                return False
            return True
        except Exception:
            LOG.exception(
                "pr-monitor: exception posting GH comment on PR #%d",
                pr_number,
            )
            return False

    def _post_wl_comment(
        self, pr_number: int, pr_title: str, comment: str
    ) -> None:
        """Post a Worklog comment.  Best-effort — failures are logged."""
        try:
            # Search for work items that reference this PR
            proc = self._wl_shell(
                f"wl search 'PR #{pr_number}' --json",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode == 0 and (proc.stdout or "").strip():
                try:
                    items = json.loads(proc.stdout.strip())
                    if isinstance(items, list) and items:
                        wid = items[0].get("id")
                        if wid:
                            self._wl_shell(
                                f'wl comment add {wid} --comment "{comment}" '
                                f'--author "ampa-pr-monitor" --json',
                                shell=True,
                                check=False,
                                capture_output=True,
                                text=True,
                                cwd=self.command_cwd,
                            )
                except Exception:
                    LOG.exception(
                        "pr-monitor: failed to parse wl search results for PR #%d",
                        pr_number,
                    )
        except Exception:
            LOG.exception(
                "pr-monitor: failed to post WL comment for PR #%d", pr_number
            )

    def _create_critical_work_item(self, title: str, description: str) -> Optional[str]:
        """Create a critical Worklog work item.  Returns the new item id or None."""
        try:
            proc = self._wl_shell(
                [
                    "wl", "create",
                    "--title", title,
                    "--description", description,
                    "--priority", "critical",
                    "--issue-type", "bug",
                    "--json",
                ],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            if proc.returncode != 0:
                LOG.warning(
                    "pr-monitor: wl create failed: rc=%s stderr=%r",
                    proc.returncode,
                    (proc.stderr or "")[:512],
                )
                return None
            stdout = (proc.stdout or "").strip()
            if stdout:
                try:
                    data = json.loads(stdout)
                    return data.get("id") or data.get("workItem", {}).get("id")
                except Exception:
                    pass
            return None
        except Exception:
            LOG.exception("pr-monitor: exception creating critical work item")
            return None

    def _notify_summary(
        self,
        ready_prs: List[int],
        failing_prs: List[int],
        skipped_prs: List[int],
        total: int,
    ) -> None:
        """Send a Discord summary notification for the entire run."""
        if not self._notifier:
            return
        try:
            lines = [f"Checked **{total}** open PR(s)."]
            if ready_prs:
                lines.append(
                    f"Ready for review: {', '.join(f'#{n}' for n in ready_prs)}"
                )
            if failing_prs:
                lines.append(
                    f"CI failing: {', '.join(f'#{n}' for n in failing_prs)}"
                )
            if skipped_prs:
                lines.append(
                    f"Skipped (already notified or pending): "
                    f"{', '.join(f'#{n}' for n in skipped_prs)}"
                )
            self._notifier.notify(
                title="PR Monitor Summary",
                body="\n".join(lines),
                message_type="command",
            )
        except Exception:
            LOG.exception("pr-monitor: failed to send summary notification")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> bool:
    """Coerce a metadata value to bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")
