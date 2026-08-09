import json

import pytest

from scripts.cleanup import cleanup_stale_remote_branches, lib, prune_local_branches
from skill.cleanup.scripts import lib as skill_lib
from skill.cleanup.scripts.inspect_current_branch import inspect_current_branch


class DummyRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def run(self, cmd):
        key = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        self.calls.append(key)
        return self.responses.get(key, lib.CommandResult(cmd, 0, "", ""))


def test_prune_local_branches_dry_run(tmp_path, monkeypatch):
    runner = DummyRunner(
        {
            "git rev-parse --abbrev-ref HEAD": lib.CommandResult(
                [], 0, "feature/test", ""
            ),
            "git for-each-ref --format=%(refname:short) refs/heads/": lib.CommandResult(
                [], 0, "main\nfeature/test\nfeature/old\n", ""
            ),
            "git merge-base --is-ancestor feature/old main": lib.CommandResult(
                [], 0, "", ""
            ),
            "git show-ref --verify --quiet refs/heads/main": lib.CommandResult(
                [], 0, "", ""
            ),
        }
    )
    monkeypatch.setattr(prune_local_branches, "lib", lib)
    monkeypatch.setattr(lib, "CommandRunner", lambda: runner)
    monkeypatch.setattr(lib, "ensure_tool_available", lambda tool: True)

    report_path = tmp_path / "local.json"
    exit_code = prune_local_branches.main(["--dry-run", "--report", str(report_path)])
    assert exit_code == 0
    payload = json.loads(report_path.read_text())
    assert payload["operation"] == "prune_local_branches"
    assert payload["dry_run"] is True


def test_cleanup_stale_remote_branches_dry_run(tmp_path, monkeypatch):
    runner = DummyRunner(
        {
            "git remote show origin": lib.CommandResult([], 0, "HEAD branch: main", ""),
            "git show-ref --verify --quiet refs/remotes/origin/main": lib.CommandResult(
                [], 0, "", ""
            ),
            "git for-each-ref --format=%(refname:short)\t%(committerdate:iso8601) refs/remotes/origin/": lib.CommandResult(
                [],
                0,
                "origin/old\t2023-01-01 00:00:00 +0000\n",
                "",
            ),
            "git merge-base --is-ancestor origin/old origin/main": lib.CommandResult(
                [], 0, "", ""
            ),
        }
    )
    monkeypatch.setattr(cleanup_stale_remote_branches, "lib", lib)
    monkeypatch.setattr(lib, "CommandRunner", lambda: runner)
    monkeypatch.setattr(lib, "ensure_tool_available", lambda tool: True)

    report_path = tmp_path / "remote.json"
    exit_code = cleanup_stale_remote_branches.main(
        ["--dry-run", "--days", "1", "--report", str(report_path)]
    )
    assert exit_code == 0
    payload = json.loads(report_path.read_text())
    assert payload["operation"] == "cleanup_stale_remote_branches"
    assert payload["dry_run"] is True


class TestWorkItemParser:
    """Work-item ID parsing from branch names in inspect_current_branch.py.

    Covers the acceptance criteria for SA-0MM1AVMSA1JQFH0V: numeric IDs
    (WL-123), alphanumeric hash IDs (SA-0MLPU8H3B1LWK3B3), branches without
    ID tokens, and edge cases.
    """

    @staticmethod
    def _inspect(branch_name: str, monkeypatch) -> dict:
        runner = DummyRunner(
            {
                "git rev-parse --abbrev-ref HEAD": lib.CommandResult(
                    [], 0, branch_name, ""
                ),
                "git remote show origin": lib.CommandResult(
                    [], 0, "HEAD branch: main", ""
                ),
            }
        )
        monkeypatch.setattr(skill_lib, "ensure_tool_available", lambda tool: True)
        return inspect_current_branch(runner, None)

    @pytest.mark.parametrize(
        ("branch", "expected"),
        [
            ("feature/WL-123-fix", "WL-123"),
            ("wl-SA-123-fix", "SA-123"),
        ],
    )
    def test_numeric_suffix_ids(self, branch, expected, monkeypatch):
        report = self._inspect(branch, monkeypatch)
        assert report["work_item_token"] == expected
        assert report["work_item_id"] == expected

    @pytest.mark.parametrize(
        ("branch", "expected"),
        [
            ("wl-SA-0MLPU8H3B1LWK3B3-audit-gate", "SA-0MLPU8H3B1LWK3B3"),
            ("SA-0MLPU8H3B1LWK3B3", "SA-0MLPU8H3B1LWK3B3"),
            ("feature/SA-0MLPU8H3B1LWK3B3-fix", "SA-0MLPU8H3B1LWK3B3"),
            (
                "wl-SA-0MSJELSWS002UF60-audit-skill-should-run-skill-test-itself",
                "SA-0MSJELSWS002UF60",
            ),
        ],
    )
    def test_alphanumeric_hash_ids(self, branch, expected, monkeypatch):
        report = self._inspect(branch, monkeypatch)
        assert report["work_item_token"] == expected
        assert report["work_item_id"] == expected

    def test_other_worklog_prefixes(self, monkeypatch):
        """Hash IDs from other worklogs (WL-, LP-, OSL-) parse fully."""
        for branch, expected in [
            ("wl-WL-0MSK9TUCA00206M7-rca", "WL-0MSK9TUCA00206M7"),
            ("wl-LP-0MSL1OX51003DOP4-fix", "LP-0MSL1OX51003DOP4"),
            ("wl-OSL-0MSABC7SB001NVUN-task", "OSL-0MSABC7SB001NVUN"),
        ]:
            report = self._inspect(branch, monkeypatch)
            assert report["work_item_token"] == expected
            assert report["work_item_id"] == expected

    @pytest.mark.parametrize("branch", ["main", "develop", "hotfix-urgent"])
    def test_no_id_token(self, branch, monkeypatch):
        report = self._inspect(branch, monkeypatch)
        assert report["work_item_token"] == ""
        assert report["work_item_id"] == ""

    def test_edge_no_digit_after_dash(self, monkeypatch):
        """Branches with letter-only suffixes must not produce a token."""
        report = self._inspect("feature/hotfix-urgent-fix", monkeypatch)
        assert report["work_item_token"] == ""
        assert report["work_item_id"] == ""


class TestSummarizeWorkItemParser:
    """parse_work_item in summarize_branches.py must behave identically to
    inspect_current_branch.py for numeric and alphanumeric hash IDs."""

    @staticmethod
    def _parse(branch: str) -> tuple[str, str]:
        from skill.cleanup.scripts.summarize_branches import parse_work_item

        return parse_work_item(branch)

    def test_numeric_id(self):
        assert self._parse("feature/WL-123-fix") == ("WL-123", "WL-123")

    def test_hash_id(self):
        assert self._parse("wl-SA-0MLPU8H3B1LWK3B3-audit-gate") == (
            "SA-0MLPU8H3B1LWK3B3",
            "SA-0MLPU8H3B1LWK3B3",
        )

    @pytest.mark.parametrize("branch", ["main", "develop", "hotfix-urgent"])
    def test_no_id_token(self, branch):
        assert self._parse(branch) == ("", "")


