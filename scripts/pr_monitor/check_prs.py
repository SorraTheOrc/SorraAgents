#!/usr/bin/env python3
"""pr_monitor/check_prs.py — Hourly scan of open PRs for CI status.

Responsibilities:
1. List all open PRs in the repository.
2. For each PR, determine whether required CI checks are passing or failing.
3. If all required checks pass and we have not already notified, post a
   "ready for review" GitHub PR comment and a Worklog comment.
4. If required checks fail, post a Worklog comment and create a critical
   Worklog work item (deduplicated by PR URL).

Authentication:
- Uses ``gh`` CLI when available (GitHub Actions provides GITHUB_TOKEN
  automatically; ``gh`` is pre-installed on ubuntu-latest runners).
- Falls back to direct GitHub API calls with the token from
  GITHUB_TOKEN or PR_BOT_TOKEN environment variables when ``gh`` is
  unavailable.

Deduplication:
- "Ready for review" comments: scan existing PR comments for the bot marker.
- Failing CI work items: query Worklog for open critical items referencing
  the PR URL before creating a new one.

Usage:
    python -m scripts.pr_monitor.check_prs [--repo OWNER/REPO] [--dry-run]
        [--report PATH] [--quiet] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOG = logging.getLogger("pr_monitor")

# Marker embedded in our bot comments so we can detect them on re-run.
READY_COMMENT_MARKER = "<!-- pr-monitor-bot: ready-for-review -->"
FAIL_COMMENT_MARKER = "<!-- pr-monitor-bot: ci-failing -->"

# Work item tag used for PR-monitor-created issues.
WL_TAG = "ci-failure"

# GitHub API base URL.
GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _get_token() -> Optional[str]:
    """Return a GitHub API token from environment variables."""
    for var in ("GITHUB_TOKEN", "PR_BOT_TOKEN"):
        token = os.environ.get(var, "").strip()
        if token:
            return token
    return None


# ---------------------------------------------------------------------------
# GitHub API helpers (via gh CLI or requests fallback)
# ---------------------------------------------------------------------------


def _gh_api(endpoint: str, method: str = "GET", data: Optional[dict] = None) -> Any:
    """Call the GitHub API via ``gh api`` and return parsed JSON.

    ``endpoint`` should be a path like ``/repos/owner/repo/pulls``.
    """
    cmd = ["gh", "api", endpoint, "--method", method, "--paginate"]
    if data:
        for key, value in data.items():
            cmd += ["-f", f"{key}={value}"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            LOG.warning("gh api %s failed: %s", endpoint, result.stderr.strip())
            return None
        return json.loads(result.stdout)
    except Exception as exc:
        LOG.warning("gh api call raised %s: %s", type(exc).__name__, exc)
        return None


def _gh_api_post(endpoint: str, data: dict) -> Any:
    """POST to the GitHub API via ``gh api``."""
    cmd = ["gh", "api", endpoint, "--method", "POST"]
    for key, value in data.items():
        if isinstance(value, str):
            cmd += ["-f", f"{key}={value}"]
        else:
            cmd += ["-F", f"{key}={value}"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            LOG.warning("gh api POST %s failed: %s", endpoint, result.stderr.strip())
            return None
        return json.loads(result.stdout)
    except Exception as exc:
        LOG.warning("gh api POST raised %s: %s", type(exc).__name__, exc)
        return None


def _requests_api(
    endpoint: str,
    token: str,
    method: str = "GET",
    data: Optional[dict] = None,
) -> Any:
    """Call the GitHub API using the ``requests`` library as a fallback."""
    try:
        import requests  # type: ignore[import]
    except ImportError:
        LOG.error("requests library not available; install it or use gh CLI")
        return None

    url = f"{GITHUB_API}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results = []
    page = 1
    while True:
        params = {"per_page": 100, "page": page}
        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, params=params, timeout=30)
            elif method == "POST":
                resp = requests.post(url, headers=headers, json=data, timeout=30)
            else:
                LOG.error("Unsupported HTTP method: %s", method)
                return None
        except Exception as exc:
            LOG.warning("requests call raised %s: %s", type(exc).__name__, exc)
            return None

        if resp.status_code not in (200, 201):
            LOG.warning("API %s %s returned %d: %s", method, url, resp.status_code, resp.text[:200])
            return None

        payload = resp.json()
        if method != "GET" or not isinstance(payload, list):
            return payload
        if not payload:
            break
        results.extend(payload)
        if len(payload) < 100:
            break
        page += 1

    return results


# ---------------------------------------------------------------------------
# GitHub operations
# ---------------------------------------------------------------------------


def list_open_prs(repo: str, use_gh: bool, token: Optional[str]) -> list[dict]:
    """Return a list of open PR dicts for *repo* (``owner/repo``)."""
    endpoint = f"/repos/{repo}/pulls?state=open&per_page=100"
    if use_gh:
        result = _gh_api(f"/repos/{repo}/pulls?state=open&per_page=100")
    else:
        if not token:
            LOG.error("No GitHub token available and gh CLI not found")
            return []
        result = _requests_api(endpoint, token)
    if not isinstance(result, list):
        return []
    return result


def get_check_runs(repo: str, sha: str, use_gh: bool, token: Optional[str]) -> list[dict]:
    """Return check runs for the given commit SHA."""
    endpoint = f"/repos/{repo}/commits/{sha}/check-runs"
    if use_gh:
        data = _gh_api(endpoint)
    else:
        if not token:
            return []
        data = _requests_api(endpoint, token)
    if not data:
        return []
    if isinstance(data, dict):
        return data.get("check_runs", [])
    return []


def get_pr_comments(repo: str, pr_number: int, use_gh: bool, token: Optional[str]) -> list[dict]:
    """Return issue comments on a PR."""
    endpoint = f"/repos/{repo}/issues/{pr_number}/comments"
    if use_gh:
        result = _gh_api(endpoint)
    else:
        if not token:
            return []
        result = _requests_api(endpoint, token)
    if not isinstance(result, list):
        return []
    return result


def post_pr_comment(
    repo: str,
    pr_number: int,
    body: str,
    use_gh: bool,
    token: Optional[str],
    dry_run: bool,
) -> bool:
    """Post a comment to the given PR. Returns True on success."""
    if dry_run:
        LOG.info("[dry-run] Would post PR comment on PR #%d: %s", pr_number, body[:80])
        return True
    endpoint = f"/repos/{repo}/issues/{pr_number}/comments"
    if use_gh:
        result = _gh_api_post(endpoint, {"body": body})
    else:
        if not token:
            return False
        result = _requests_api(endpoint, token, method="POST", data={"body": body})
    return result is not None


# ---------------------------------------------------------------------------
# Worklog helpers
# ---------------------------------------------------------------------------


def _run_wl(args: list[str]) -> Optional[str]:
    """Run a ``wl`` command and return stdout, or None on failure."""
    cmd = ["wl"] + args
    try:
        out = subprocess.check_output(cmd, encoding="utf-8", stderr=subprocess.PIPE, timeout=60)
        return out
    except Exception as exc:
        LOG.warning("wl command failed: %s: %s", " ".join(cmd), exc)
        return None


def _wl_list_critical_ci_items() -> list[dict]:
    """List open critical work items tagged ci-failure."""
    out = _run_wl(["list", "--priority", "critical", "--tags", WL_TAG, "--json"])
    if not out:
        return []
    try:
        data = json.loads(out)
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def _wl_add_comment(item_id: str, comment: str, dry_run: bool) -> bool:
    """Add a comment to a Worklog work item. Returns True on success."""
    if dry_run:
        LOG.info("[dry-run] Would add Worklog comment to %s: %s", item_id, comment[:80])
        return True
    out = _run_wl(
        [
            "comment",
            "add",
            item_id,
            "--comment",
            comment,
            "--author",
            "pr-monitor-bot",
            "--json",
        ]
    )
    return out is not None


def _wl_create_critical_item(title: str, description: str, dry_run: bool) -> Optional[str]:
    """Create a critical Worklog work item. Returns the new item ID or None."""
    if dry_run:
        LOG.info("[dry-run] Would create critical Worklog item: %s", title)
        return "DRY-RUN-ID"
    out = _run_wl(
        [
            "create",
            "--title",
            title,
            "--description",
            description,
            "--priority",
            "critical",
            "--tags",
            WL_TAG,
            "--issue-type",
            "bug",
            "--json",
        ]
    )
    if not out:
        return None
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            return data.get("id") or (data.get("workItem") or {}).get("id")
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CI status evaluation
# ---------------------------------------------------------------------------


def evaluate_pr_checks(check_runs: list[dict]) -> tuple[str, list[str]]:
    """Determine overall CI status from a list of check run dicts.

    Returns a tuple of (overall_status, failing_check_names).
    overall_status is one of: ``"passing"``, ``"failing"``, ``"pending"``, ``"unknown"``.
    """
    if not check_runs:
        return "unknown", []

    failing = []
    pending = []
    for run in check_runs:
        status = (run.get("status") or "").lower()
        conclusion = (run.get("conclusion") or "").lower()
        name = run.get("name", "unknown")

        if status != "completed":
            pending.append(name)
            continue
        if conclusion in ("failure", "timed_out", "cancelled", "action_required", "startup_failure"):
            failing.append(name)

    if failing:
        return "failing", failing
    if pending:
        return "pending", []
    return "passing", []


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------


def _already_notified_ready(comments: list[dict]) -> bool:
    """True if a previous 'ready for review' bot comment exists."""
    for comment in comments:
        body = comment.get("body") or ""
        if READY_COMMENT_MARKER in body:
            return True
    return False


def _find_existing_failure_item(pr_url: str, items: list[dict]) -> Optional[str]:
    """Return the ID of an existing critical CI failure item that references *pr_url*."""
    for item in items:
        description = item.get("description") or (item.get("workItem") or {}).get("description") or ""
        title = item.get("title") or (item.get("workItem") or {}).get("title") or ""
        if pr_url in description or pr_url in title:
            return item.get("id") or (item.get("workItem") or {}).get("id")
    return None


# ---------------------------------------------------------------------------
# Per-PR processing
# ---------------------------------------------------------------------------


def _render_failing_ci_description(pr: dict, failing_checks: list[str]) -> str:
    pr_number = pr.get("number", "?")
    pr_title = pr.get("title", "(no title)")
    pr_url = pr.get("html_url", "")
    branch = (pr.get("head") or {}).get("ref", "?")
    checks_list = "\n".join(f"- {c}" for c in failing_checks) if failing_checks else "- (unknown)"
    return (
        f"## CI Failure Detected\n\n"
        f"Pull request #{pr_number} — **{pr_title}** has failing CI checks.\n\n"
        f"- **PR URL:** {pr_url}\n"
        f"- **Branch:** `{branch}`\n\n"
        f"## Failing Checks\n\n"
        f"{checks_list}\n\n"
        f"## Next Steps\n\n"
        f"1. Review failing check logs on the PR.\n"
        f"2. Fix the failing tests or configuration.\n"
        f"3. Push a fix commit and re-run CI.\n"
    )


def process_pr(
    pr: dict,
    repo: str,
    use_gh: bool,
    token: Optional[str],
    dry_run: bool,
) -> dict[str, Any]:
    """Evaluate CI for one PR and take the appropriate action.

    Returns a result dict describing the action taken.
    """
    pr_number = pr.get("number")
    pr_title = pr.get("title", "(no title)")
    pr_url = pr.get("html_url", "")
    sha = (pr.get("head") or {}).get("sha", "")

    if not sha:
        LOG.warning("PR #%s has no head SHA — skipping", pr_number)
        return {"pr": pr_number, "action": "skipped", "reason": "no_sha"}

    # Fetch CI check runs for the head commit.
    check_runs = get_check_runs(repo, sha, use_gh, token)
    status, failing_checks = evaluate_pr_checks(check_runs)

    LOG.info("PR #%s '%s' — CI status: %s", pr_number, pr_title, status)

    if status == "passing":
        # Check if we already posted a "ready" comment to avoid spam.
        comments = get_pr_comments(repo, pr_number, use_gh, token)
        if _already_notified_ready(comments):
            LOG.info("PR #%s: already notified ready — skipping", pr_number)
            return {"pr": pr_number, "action": "skipped", "reason": "already_notified_ready"}

        # Post "ready for review" comment on GitHub PR.
        ready_body = (
            f"{READY_COMMENT_MARKER}\n"
            f"🟢 **Ready for Review** — all CI checks are passing on PR #{pr_number}.\n\n"
            f"All required checks passed. This PR is ready for human review."
        )
        pr_commented = post_pr_comment(repo, pr_number, ready_body, use_gh, token, dry_run)
        LOG.info("PR #%s: posted ready-for-review comment on GitHub", pr_number)

        return {
            "pr": pr_number,
            "action": "notified_ready",
            "github_comment_posted": pr_commented,
        }

    if status == "failing":
        # Deduplicate: look for an existing critical work item for this PR.
        existing_items = _wl_list_critical_ci_items()
        existing_id = _find_existing_failure_item(pr_url, existing_items)

        if existing_id:
            LOG.info("PR #%s: existing failure work item %s — adding comment", pr_number, existing_id)
            follow_up = (
                f"CI is still failing on PR #{pr_number} '{pr_title}' ({pr_url}). "
                f"Failing checks: {', '.join(failing_checks) if failing_checks else 'unknown'}."
            )
            _wl_add_comment(existing_id, follow_up, dry_run)
            return {
                "pr": pr_number,
                "action": "updated_existing_failure_item",
                "work_item_id": existing_id,
            }

        # Create a new critical work item.
        description = _render_failing_ci_description(pr, failing_checks)
        title = f"[ci-failure] PR #{pr_number} — {pr_title}"
        new_id = _wl_create_critical_item(title, description, dry_run)
        LOG.info("PR #%s: created critical Worklog work item %s", pr_number, new_id)

        return {
            "pr": pr_number,
            "action": "created_failure_item",
            "work_item_id": new_id,
        }

    # Pending or unknown — do nothing.
    return {"pr": pr_number, "action": "skipped", "reason": f"ci_status_{status}"}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _detect_repo() -> Optional[str]:
    """Auto-detect owner/repo from git remote origin."""
    import re

    _SSH_RE = re.compile(r"git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$")
    _HTTPS_RE = re.compile(r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$")

    # Try GITHUB_REPOSITORY env variable first (set in GitHub Actions).
    env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env_repo:
        return env_repo

    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        url = proc.stdout.strip()
        for pattern in (_SSH_RE, _HTTPS_RE):
            m = pattern.match(url)
            if m:
                return f"{m.group('owner')}/{m.group('repo')}"
    except Exception:
        pass
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan open PRs for CI status and notify stakeholders."
    )
    parser.add_argument("--repo", help="GitHub repo in owner/repo format (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Do not post comments or create work items")
    parser.add_argument("--report", help="Write JSON report to this path")
    parser.add_argument("--quiet", action="store_true", help="Suppress JSON output to stdout")
    parser.add_argument("--verbose", action="count", default=0, help="Increase logging verbosity")
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    repo = args.repo or _detect_repo()
    if not repo:
        LOG.error(
            "Could not determine GitHub repository. "
            "Set --repo, GITHUB_REPOSITORY, or ensure git remote origin is a GitHub URL."
        )
        return 1

    use_gh = _gh_available()
    token = _get_token()

    if not use_gh and not token:
        LOG.error(
            "Neither gh CLI nor GITHUB_TOKEN/PR_BOT_TOKEN is available. "
            "Cannot authenticate to the GitHub API."
        )
        return 1

    if use_gh:
        LOG.info("Using gh CLI for GitHub API calls")
    else:
        LOG.info("gh CLI not found; using requests with token from environment")

    prs = list_open_prs(repo, use_gh, token)
    LOG.info("Found %d open PRs in %s", len(prs), repo)

    results = []
    for pr in prs:
        try:
            result = process_pr(pr, repo, use_gh, token, args.dry_run)
            results.append(result)
        except Exception as exc:
            pr_number = pr.get("number", "?")
            LOG.exception("Error processing PR #%s: %s", pr_number, exc)
            results.append({"pr": pr_number, "action": "error", "error": str(exc)})

    report = {
        "repo": repo,
        "dry_run": args.dry_run,
        "total_prs": len(prs),
        "results": results,
    }

    payload = json.dumps(report, indent=2)
    if args.report:
        directory = os.path.dirname(args.report)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
    if not args.quiet:
        print(payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())
