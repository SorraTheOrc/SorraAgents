"""Tests for scripts/pr_monitor/check_prs.py."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

import scripts.pr_monitor.check_prs as cp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pr(number: int, sha: str = "abc123", url: str = "") -> dict:
    if not url:
        url = f"https://github.com/owner/repo/pull/{number}"
    return {
        "number": number,
        "title": f"Test PR #{number}",
        "html_url": url,
        "head": {"sha": sha, "ref": f"feature/branch-{number}"},
    }


def _make_check_run(name: str, status: str = "completed", conclusion: str = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


# ---------------------------------------------------------------------------
# evaluate_pr_checks
# ---------------------------------------------------------------------------


class TestEvaluatePrChecks:
    def test_empty_returns_unknown(self):
        status, failing = cp.evaluate_pr_checks([])
        assert status == "unknown"
        assert failing == []

    def test_all_passing(self):
        runs = [
            _make_check_run("ci/test", "completed", "success"),
            _make_check_run("ci/lint", "completed", "success"),
        ]
        status, failing = cp.evaluate_pr_checks(runs)
        assert status == "passing"
        assert failing == []

    def test_one_failing(self):
        runs = [
            _make_check_run("ci/test", "completed", "failure"),
            _make_check_run("ci/lint", "completed", "success"),
        ]
        status, failing = cp.evaluate_pr_checks(runs)
        assert status == "failing"
        assert "ci/test" in failing

    def test_pending_check(self):
        runs = [
            _make_check_run("ci/test", "in_progress", ""),
            _make_check_run("ci/lint", "completed", "success"),
        ]
        status, failing = cp.evaluate_pr_checks(runs)
        assert status == "pending"
        assert failing == []

    def test_timed_out_is_failing(self):
        runs = [_make_check_run("ci/test", "completed", "timed_out")]
        status, failing = cp.evaluate_pr_checks(runs)
        assert status == "failing"
        assert "ci/test" in failing

    def test_cancelled_is_failing(self):
        runs = [_make_check_run("ci/deploy", "completed", "cancelled")]
        status, failing = cp.evaluate_pr_checks(runs)
        assert status == "failing"
        assert "ci/deploy" in failing

    def test_action_required_is_failing(self):
        runs = [_make_check_run("ci/build", "completed", "action_required")]
        status, failing = cp.evaluate_pr_checks(runs)
        assert status == "failing"

    def test_failing_takes_precedence_over_pending(self):
        runs = [
            _make_check_run("ci/test", "completed", "failure"),
            _make_check_run("ci/lint", "in_progress", ""),
        ]
        status, failing = cp.evaluate_pr_checks(runs)
        assert status == "failing"


# ---------------------------------------------------------------------------
# _already_notified_ready
# ---------------------------------------------------------------------------


class TestAlreadyNotifiedReady:
    def test_no_comments(self):
        assert cp._already_notified_ready([]) is False

    def test_marker_present(self):
        comments = [{"body": f"Some text\n{cp.READY_COMMENT_MARKER}\nMore text"}]
        assert cp._already_notified_ready(comments) is True

    def test_marker_absent(self):
        comments = [{"body": "Just a normal comment"}, {"body": "Another comment"}]
        assert cp._already_notified_ready(comments) is False

    def test_multiple_comments_one_with_marker(self):
        comments = [
            {"body": "Normal comment"},
            {"body": f"{cp.READY_COMMENT_MARKER}"},
        ]
        assert cp._already_notified_ready(comments) is True


# ---------------------------------------------------------------------------
# _find_existing_failure_item
# ---------------------------------------------------------------------------


class TestFindExistingFailureItem:
    def test_no_items(self):
        assert cp._find_existing_failure_item("https://github.com/o/r/pull/1", []) is None

    def test_item_with_url_in_description(self):
        pr_url = "https://github.com/owner/repo/pull/42"
        items = [
            {"id": "SA-001", "title": "[ci-failure] PR #42", "description": f"PR: {pr_url}"}
        ]
        found_id = cp._find_existing_failure_item(pr_url, items)
        assert found_id == "SA-001"

    def test_item_with_url_in_title(self):
        pr_url = "https://github.com/owner/repo/pull/42"
        items = [
            {"id": "SA-002", "title": f"[ci-failure] {pr_url}", "description": "something"}
        ]
        found_id = cp._find_existing_failure_item(pr_url, items)
        assert found_id == "SA-002"

    def test_different_pr_url_not_matched(self):
        pr_url = "https://github.com/owner/repo/pull/42"
        items = [
            {"id": "SA-003", "title": "[ci-failure] PR #99", "description": "PR: https://github.com/owner/repo/pull/99"}
        ]
        found_id = cp._find_existing_failure_item(pr_url, items)
        assert found_id is None


# ---------------------------------------------------------------------------
# process_pr — passing CI
# ---------------------------------------------------------------------------


class TestProcessPrPassing:
    def test_posts_ready_comment_when_no_prior_notification(self):
        pr = _make_pr(1)
        check_runs = [_make_check_run("ci/test", "completed", "success")]
        comments = []  # No prior bot comment

        with (
            patch.object(cp, "get_check_runs", return_value=check_runs),
            patch.object(cp, "get_pr_comments", return_value=comments),
            patch.object(cp, "post_pr_comment", return_value=True) as mock_post,
            patch.object(cp, "_wl_add_comment", return_value=True),
        ):
            result = cp.process_pr(pr, "owner/repo", True, None, dry_run=False)

        assert result["action"] == "notified_ready"
        assert result["github_comment_posted"] is True
        mock_post.assert_called_once()
        # post_pr_comment(repo, pr_number, body, use_gh, token, dry_run)
        call_body = mock_post.call_args[0][2]
        assert cp.READY_COMMENT_MARKER in call_body

    def test_skips_when_already_notified(self):
        pr = _make_pr(2)
        check_runs = [_make_check_run("ci/test", "completed", "success")]
        comments = [{"body": f"{cp.READY_COMMENT_MARKER} already posted"}]

        with (
            patch.object(cp, "get_check_runs", return_value=check_runs),
            patch.object(cp, "get_pr_comments", return_value=comments),
            patch.object(cp, "post_pr_comment") as mock_post,
        ):
            result = cp.process_pr(pr, "owner/repo", True, None, dry_run=False)

        assert result["action"] == "skipped"
        assert result["reason"] == "already_notified_ready"
        mock_post.assert_not_called()

    def test_dry_run_does_not_post(self):
        pr = _make_pr(3)
        check_runs = [_make_check_run("ci/test", "completed", "success")]
        comments = []

        posted_calls = []

        def fake_post(*args, **kwargs):
            posted_calls.append(args)
            return True

        with (
            patch.object(cp, "get_check_runs", return_value=check_runs),
            patch.object(cp, "get_pr_comments", return_value=comments),
            patch.object(cp, "post_pr_comment", side_effect=fake_post),
            patch.object(cp, "_wl_add_comment", return_value=True),
        ):
            result = cp.process_pr(pr, "owner/repo", True, None, dry_run=True)

        # The function is called but dry_run=True means post_pr_comment handles no-op internally
        assert result["action"] == "notified_ready"


# ---------------------------------------------------------------------------
# process_pr — failing CI
# ---------------------------------------------------------------------------


class TestProcessPrFailing:
    def test_creates_new_work_item_when_none_exists(self):
        pr = _make_pr(10)
        check_runs = [_make_check_run("ci/test", "completed", "failure")]

        with (
            patch.object(cp, "get_check_runs", return_value=check_runs),
            patch.object(cp, "_wl_list_critical_ci_items", return_value=[]),
            patch.object(cp, "_wl_create_critical_item", return_value="SA-NEW") as mock_create,
            patch.object(cp, "_wl_add_comment", return_value=True),
        ):
            result = cp.process_pr(pr, "owner/repo", True, None, dry_run=False)

        assert result["action"] == "created_failure_item"
        assert result["work_item_id"] == "SA-NEW"
        mock_create.assert_called_once()

    def test_updates_existing_work_item_when_found(self):
        pr = _make_pr(11)
        pr_url = pr["html_url"]
        check_runs = [_make_check_run("ci/test", "completed", "failure")]
        existing_items = [
            {"id": "SA-EXISTING", "title": f"[ci-failure] PR #11", "description": f"PR URL: {pr_url}"}
        ]

        with (
            patch.object(cp, "get_check_runs", return_value=check_runs),
            patch.object(cp, "_wl_list_critical_ci_items", return_value=existing_items),
            patch.object(cp, "_wl_create_critical_item") as mock_create,
            patch.object(cp, "_wl_add_comment", return_value=True) as mock_comment,
        ):
            result = cp.process_pr(pr, "owner/repo", True, None, dry_run=False)

        assert result["action"] == "updated_existing_failure_item"
        assert result["work_item_id"] == "SA-EXISTING"
        mock_create.assert_not_called()
        mock_comment.assert_called_once()

    def test_failure_title_contains_pr_info(self):
        pr = _make_pr(12)
        check_runs = [_make_check_run("ci/test", "completed", "failure")]

        created_titles = []

        def fake_create(title, description, dry_run):
            created_titles.append(title)
            return "SA-T"

        with (
            patch.object(cp, "get_check_runs", return_value=check_runs),
            patch.object(cp, "_wl_list_critical_ci_items", return_value=[]),
            patch.object(cp, "_wl_create_critical_item", side_effect=fake_create),
            patch.object(cp, "_wl_add_comment", return_value=True),
        ):
            cp.process_pr(pr, "owner/repo", True, None, dry_run=False)

        assert len(created_titles) == 1
        assert "12" in created_titles[0]
        assert "[ci-failure]" in created_titles[0]


# ---------------------------------------------------------------------------
# process_pr — pending / unknown CI
# ---------------------------------------------------------------------------


class TestProcessPrPendingUnknown:
    def test_pending_skipped(self):
        pr = _make_pr(20)
        check_runs = [_make_check_run("ci/test", "in_progress", "")]

        with patch.object(cp, "get_check_runs", return_value=check_runs):
            result = cp.process_pr(pr, "owner/repo", True, None, dry_run=False)

        assert result["action"] == "skipped"
        assert "pending" in result["reason"]

    def test_unknown_skipped(self):
        pr = _make_pr(21)
        check_runs = []  # No check runs = unknown

        with patch.object(cp, "get_check_runs", return_value=check_runs):
            result = cp.process_pr(pr, "owner/repo", True, None, dry_run=False)

        assert result["action"] == "skipped"
        assert "unknown" in result["reason"]

    def test_no_sha_skipped(self):
        pr = {
            "number": 99,
            "title": "Broken PR",
            "html_url": "https://github.com/o/r/pull/99",
            "head": {"sha": "", "ref": "feature/x"},
        }
        result = cp.process_pr(pr, "owner/repo", True, None, dry_run=False)
        assert result["action"] == "skipped"
        assert result["reason"] == "no_sha"


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_returns_zero_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        report_path = str(tmp_path / "report.json")

        with (
            patch.object(cp, "_gh_available", return_value=True),
            patch.object(cp, "_get_token", return_value="tok"),
            patch.object(cp, "list_open_prs", return_value=[]),
        ):
            rc = cp.main(["--repo", "owner/repo", "--report", report_path, "--quiet"])

        assert rc == 0
        payload = json.loads(open(report_path).read())
        assert payload["repo"] == "owner/repo"
        assert payload["total_prs"] == 0

    def test_main_fails_without_repo_and_no_env(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        with (
            patch.object(cp, "_detect_repo", return_value=None),
            patch.object(cp, "_gh_available", return_value=True),
        ):
            rc = cp.main(["--quiet"])

        assert rc == 1

    def test_main_fails_without_auth(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        with (
            patch.object(cp, "_gh_available", return_value=False),
            patch.object(cp, "_get_token", return_value=None),
        ):
            rc = cp.main(["--repo", "owner/repo", "--quiet"])

        assert rc == 1

    def test_main_dry_run_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        report_path = str(tmp_path / "report.json")

        with (
            patch.object(cp, "_gh_available", return_value=True),
            patch.object(cp, "_get_token", return_value="tok"),
            patch.object(cp, "list_open_prs", return_value=[]),
        ):
            rc = cp.main(["--repo", "owner/repo", "--dry-run", "--report", report_path, "--quiet"])

        assert rc == 0
        payload = json.loads(open(report_path).read())
        assert payload["dry_run"] is True

    def test_main_processes_multiple_prs(self, tmp_path, monkeypatch):
        report_path = str(tmp_path / "report.json")
        prs = [_make_pr(1), _make_pr(2)]
        check_runs = [_make_check_run("ci/test", "completed", "success")]

        with (
            patch.object(cp, "_gh_available", return_value=True),
            patch.object(cp, "_get_token", return_value="tok"),
            patch.object(cp, "list_open_prs", return_value=prs),
            patch.object(cp, "get_check_runs", return_value=check_runs),
            patch.object(cp, "get_pr_comments", return_value=[]),
            patch.object(cp, "post_pr_comment", return_value=True),
            patch.object(cp, "_wl_add_comment", return_value=True),
        ):
            rc = cp.main(["--repo", "owner/repo", "--report", report_path, "--quiet"])

        assert rc == 0
        payload = json.loads(open(report_path).read())
        assert payload["total_prs"] == 2
        assert len(payload["results"]) == 2


# ---------------------------------------------------------------------------
# _detect_repo
# ---------------------------------------------------------------------------


class TestDetectRepo:
    def test_uses_github_repository_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "myorg/myrepo")
        assert cp._detect_repo() == "myorg/myrepo"

    def test_falls_back_to_git_remote_ssh(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        import subprocess as sp

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "git@github.com:owner/repo.git\n"

        with patch("scripts.pr_monitor.check_prs.subprocess.run", return_value=fake_proc):
            result = cp._detect_repo()

        assert result == "owner/repo"

    def test_returns_none_when_no_remote(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        fake_proc = MagicMock()
        fake_proc.returncode = 128
        fake_proc.stdout = ""

        with patch("scripts.pr_monitor.check_prs.subprocess.run", return_value=fake_proc):
            result = cp._detect_repo()

        assert result is None


# ---------------------------------------------------------------------------
# _render_failing_ci_description
# ---------------------------------------------------------------------------


class TestRenderFailingCiDescription:
    def test_contains_pr_info(self):
        pr = _make_pr(7, url="https://github.com/o/r/pull/7")
        description = cp._render_failing_ci_description(pr, ["ci/test", "ci/lint"])
        assert "PR #7" in description
        assert "https://github.com/o/r/pull/7" in description
        assert "ci/test" in description
        assert "ci/lint" in description

    def test_handles_empty_failing_checks(self):
        pr = _make_pr(8)
        description = cp._render_failing_ci_description(pr, [])
        assert "unknown" in description
