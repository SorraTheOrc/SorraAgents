"""Unit tests for check_or_create.py triage helper."""

import json
import sys

import triage.scripts.check_or_create as cc

# ---------------------------------------------------------------------------
# list_critical_issues() output-shape parsing
# ---------------------------------------------------------------------------


def test_list_critical_issues_parses_workitems_wrapper(monkeypatch):
    """list_critical_issues() parses the real wl list output {"workItems": [...]}.

    The function now queries each incomplete status separately
    (SA-0MSPPI2FL005UQCR), so the mock filters by the requested --status.
    """

    def fake_run_wl(args):
        status = args[args.index("--status") + 1] if "--status" in args else None
        all_items = [
            {
                "id": "SA-WRAP-1",
                "title": "[test-failure] test_wrap_a — failing",
                "description": "Test name: test_wrap_a",
                "status": "open",
                "updatedAt": "2026-02-20T00:00:00Z",
            },
            {
                "id": "SA-WRAP-2",
                "title": "[test-failure] test_wrap_b — failing",
                "description": "Test name: test_wrap_b",
                "status": "in-progress",
                "updatedAt": "2026-02-21T00:00:00Z",
            },
        ]
        filtered = [it for it in all_items if it["status"] == status]
        return json.dumps({"success": True, "count": len(filtered), "workItems": filtered})

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)
    items = cc.list_critical_issues()
    assert len(items) == 2
    assert items[0]["id"] == "SA-WRAP-1"
    assert items[1]["id"] == "SA-WRAP-2"


def test_list_critical_issues_legacy_items_wrapper(monkeypatch):
    """list_critical_issues() still parses the legacy {"items": [...]} shape."""

    def fake_run_wl(args):
        return json.dumps(
            {
                "items": [
                    {
                        "id": "SA-LEGACY",
                        "title": "[test-failure] test_legacy — failing",
                        "description": "Test name: test_legacy",
                        "status": "open",
                        "updatedAt": "2026-02-20T00:00:00Z",
                    }
                ]
            }
        )

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)
    items = cc.list_critical_issues()
    # The legacy mock returns the same items for both status queries; the
    # function merges them. De-duplicate by id to count distinct items.
    distinct = {it["id"] for it in items}
    assert len(distinct) == 1
    assert "SA-LEGACY" in distinct


# ---------------------------------------------------------------------------
# Heuristic 1: exact test name match
# ---------------------------------------------------------------------------


def test_match_existing_exact_name(monkeypatch, capsys):
    """If an incomplete test-failure issue exists matching the test name, return it.

    Uses the real `wl list --json` output shape ({"success", "count", "workItems"})
    so the full check_or_create path is exercised against the wrapper.
    """

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps(
                {
                    "success": True,
                    "count": 1,
                    "workItems": [
                        {
                            "id": "SA-EX",
                            "title": "[test-failure] test_foo — failing",
                            "description": "Test name: test_foo",
                            "status": "open",
                            "updatedAt": "2026-02-20T00:00:00Z",
                        }
                    ],
                }
            )
        if args and args[0] == "comment":
            return "{}"
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create({"test_name": "test_foo", "stdout_excerpt": "fail"})
    assert result["created"] is False
    assert result["matchedId"] == "SA-EX"
    assert "matched_existing" in result["reason"]


# ---------------------------------------------------------------------------
# Heuristic 2: token overlap + stacktrace top-frame
# ---------------------------------------------------------------------------


def test_match_heuristic_2_token_overlap(monkeypatch):
    """Token overlap in title + top-frame in body matches via heuristic 2."""

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps(
                [
                    {
                        "id": "SA-H2",
                        "title": "[test-failure] scheduler heartbeat failing",
                        "description": 'File "scheduler.py", line 42\nHeartbeatError',
                        "status": "open",
                        "updatedAt": "2026-02-20T00:00:00Z",
                    }
                ]
            )
        if args and args[0] == "comment":
            return "{}"
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create(
        {
            "test_name": "test_scheduler_heartbeat",
            "stdout_excerpt": "fail",
            "stack_trace": 'File "scheduler.py", line 42\nHeartbeatError',
        }
    )
    assert result["created"] is False
    assert result["matchedId"] == "SA-H2"
    assert "token_overlap" in result["reason"]


# ---------------------------------------------------------------------------
# Heuristic 3: commit hash match
# ---------------------------------------------------------------------------


def test_match_heuristic_3_commit_hash(monkeypatch):
    """Commit hash in body matches via heuristic 3."""

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps(
                [
                    {
                        "id": "SA-H3",
                        "title": "[test-failure] some_other_test",
                        "description": "Failing commit: abc123def",
                        "status": "in_progress",
                        "updatedAt": "2026-02-20T00:00:00Z",
                    }
                ]
            )
        if args and args[0] == "comment":
            return "{}"
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create(
        {
            "test_name": "test_unrelated",
            "stdout_excerpt": "err",
            "commit_hash": "abc123def",
        }
    )
    assert result["created"] is False
    assert result["matchedId"] == "SA-H3"
    assert "commit_or_ci_url" in result["reason"]


# ---------------------------------------------------------------------------
# Create new issue
# ---------------------------------------------------------------------------


def test_create_new_issue_success(monkeypatch, capsys):
    """When no matching issue exists, create a new critical work item."""

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps([])
        if args and args[0] == "create":
            return json.dumps({"id": "SA-NEW"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create({"test_name": "test_bar", "stdout_excerpt": "err"})
    assert result["created"] is True
    assert result["issueId"] == "SA-NEW"
    assert result["reason"] == "created_new"


def test_create_issue_uses_template_sections(monkeypatch):
    """Created issue body contains all template sections."""

    captured_body = {}

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps([])
        if args and args[0] == "create":
            # Capture the description argument
            idx = args.index("--description")
            captured_body["body"] = args[idx + 1]
            return json.dumps({"id": "SA-TPL"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    cc.check_or_create(
        {
            "test_name": "test_tpl",
            "stdout_excerpt": "some output",
            "stack_trace": "traceback here",
            "commit_hash": "deadbeef",
        }
    )

    body = captured_body["body"]
    assert "## Failure Signature" in body
    assert "## Evidence" in body
    assert "## Steps To Reproduce" in body
    assert "## Impact" in body
    assert "## Suggested Triage Steps" in body
    assert "## Suspected Owner" in body
    assert "## Links" in body
    assert "test_tpl" in body
    assert "deadbeef" in body
    assert "Build" in body
    assert "pytest -q -r a --disable-warnings -k test_tpl" in body


# ---------------------------------------------------------------------------
# Create failure
# ---------------------------------------------------------------------------


def test_create_failure_no_wl(monkeypatch):
    """If WL create fails, return error dict."""

    def fake_run_wl(args):
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create({"test_name": "test_baz", "stdout_excerpt": "err"})
    assert "error" in result


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_idempotence(monkeypatch):
    """A second run for the same signature matches the previously-created issue."""

    # First run: no candidates, create returns SA-FOO
    def fake_run_wl_first(args):
        if args and args[0] == "list":
            return json.dumps([])
        if args and args[0] == "create":
            return json.dumps({"id": "SA-FOO"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl_first)

    out1 = cc.check_or_create({"test_name": "test_qux", "stdout_excerpt": "err"})
    assert out1["created"] is True
    assert out1["issueId"] == "SA-FOO"

    # Second run: list returns the created item
    def fake_run_wl_second(args):
        if args and args[0] == "list":
            return json.dumps(
                [
                    {
                        "id": "SA-FOO",
                        "title": "[test-failure] test_qux",
                        "description": "Test name: test_qux",
                        "status": "open",
                        "updatedAt": "2026-02-20T00:00:00Z",
                    }
                ]
            )
        if args and args[0] == "comment":
            return "{}"
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl_second)

    out2 = cc.check_or_create({"test_name": "test_qux", "stdout_excerpt": "err"})
    assert out2["created"] is False
    assert out2["matchedId"] == "SA-FOO"


# ---------------------------------------------------------------------------
# Skips completed issues
# ---------------------------------------------------------------------------


def test_skip_completed_issues(monkeypatch):
    """Completed issues are not considered matches."""

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps(
                [
                    {
                        "id": "SA-DONE",
                        "title": "[test-failure] test_skip — fixed",
                        "description": "Test name: test_skip",
                        "status": "completed",
                        "updatedAt": "2026-02-20T00:00:00Z",
                    }
                ]
            )
        if args and args[0] == "create":
            return json.dumps({"id": "SA-NEWSKIP"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create({"test_name": "test_skip", "stdout_excerpt": "fail"})
    assert result["created"] is True


# ---------------------------------------------------------------------------
# Prefers most recent match
# ---------------------------------------------------------------------------


def test_prefers_most_recent_match(monkeypatch):
    """When multiple candidates match, the most recently updated is preferred."""

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps(
                [
                    {
                        "id": "SA-OLD",
                        "title": "[test-failure] test_multi",
                        "description": "Test name: test_multi",
                        "status": "open",
                        "updatedAt": "2026-02-01T00:00:00Z",
                    },
                    {
                        "id": "SA-NEW",
                        "title": "[test-failure] test_multi",
                        "description": "Test name: test_multi",
                        "status": "open",
                        "updatedAt": "2026-02-20T00:00:00Z",
                    },
                ]
            )
        if args and args[0] == "comment":
            return "{}"
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create({"test_name": "test_multi", "stdout_excerpt": "fail"})
    assert result["matchedId"] == "SA-NEW"


# ---------------------------------------------------------------------------
# Missing test_name
# ---------------------------------------------------------------------------


def test_missing_test_name():
    """Returns error when test_name is not provided."""
    result = cc.check_or_create({"stdout_excerpt": "fail"})
    assert "error" in result


# ---------------------------------------------------------------------------
# Child mode: create child work item for test failures
# ---------------------------------------------------------------------------


def test_child_mode_creates_child_work_item(monkeypatch):
    """With parent_id provided, create a child work item linked to the parent."""

    captured_args = {}

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps([])
        if args and args[0] == "create":
            # Capture the --parent argument if present
            if "--parent" in args:
                idx = args.index("--parent")
                captured_args["parent"] = args[idx + 1]
            return json.dumps({"id": "SA-CHILD"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create({
        "test_name": "test_child_mode",
        "stdout_excerpt": "fail",
        "parent_work_item_id": "SA-PARENT",
    })
    assert result["created"] is True
    assert result["issueId"] == "SA-CHILD"
    assert captured_args.get("parent") == "SA-PARENT"


def test_child_mode_matched_issue_linked_to_parent(monkeypatch):
    """When matching an existing issue in child mode, add dependency so parent blocks on it."""

    captured_args = {}

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps(
                [
                    {
                        "id": "SA-EXISTING",
                        "title": "[test-failure] test_child_match",
                        "description": "Test name: test_child_match",
                        "status": "open",
                        "updatedAt": "2026-02-20T00:00:00Z",
                    }
                ]
            )
        if args and args[0] == "create":
            return json.dumps({"id": "SA-NEWCHILD"})
        if args and args[0] == "comment":
            return "{}"
        if args and args[0] == "dep":
            # Capture the dependency add call (strip --json for comparison)
            captured_args["dep_add"] = [a for a in args[1:] if a != "--json"]
            return "{}"
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create({
        "test_name": "test_child_match",
        "stdout_excerpt": "fail",
        "parent_work_item_id": "SA-PARENT2",
    })
    assert result["created"] is False
    # When matching an existing issue in child mode, add dependency
    # so parent (SA-PARENT2) depends on the issue (SA-EXISTING)
    assert captured_args.get("dep_add") == ["add", "SA-PARENT2", "SA-EXISTING"]


def test_child_mode_parent_failure_detection(monkeypatch):
    """When parent_work_item_id is invalid or missing, fall back to regular mode."""

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps([])
        if args and args[0] == "create":
            return json.dumps({"id": "SA-NOPARENT"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    # Missing parent_work_item_id - should create without parent
    result = cc.check_or_create({"test_name": "test_no_parent", "stdout_excerpt": "fail"})
    assert result["created"] is True
    assert result["issueId"] == "SA-NOPARENT"


def test_child_mode_with_existing_matched_issue_adds_dependency(monkeypatch):
    """When matching existing issue with parent_id, add dependency (not parent link)."""

    captured_dep_calls = []

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps(
                [
                    {
                        "id": "SA-HAVE",
                        "title": "[test-failure] test_with_dependency",
                        "description": "Test name: test_with_dependency",
                        "status": "open",
                        "updatedAt": "2026-02-20T00:00:00Z",
                    }
                ]
            )
        if args and args[0] == "comment":
            return "{}"
        if args and args[0] == "dep":
            captured_dep_calls.append(args)
            return "{}"
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    result = cc.check_or_create({
        "test_name": "test_with_dependency",
        "stdout_excerpt": "fail",
        "parent_work_item_id": "SA-PARENT-DEPS",
    })
    assert result["created"] is False
    assert result["matchedId"] == "SA-HAVE"
    # Verify dependency was added so parent blocks on the matched issue
    assert len(captured_dep_calls) == 1
    assert "add" in captured_dep_calls[0]
    assert "SA-PARENT-DEPS" in captured_dep_calls[0]
    assert "SA-HAVE" in captured_dep_calls[0]


# ---------------------------------------------------------------------------
# CLI main() integration
# ---------------------------------------------------------------------------


def test_main_cli(monkeypatch, capsys):
    """main() reads sys.argv and prints JSON result."""

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps([])
        if args and args[0] == "create":
            return json.dumps({"id": "SA-CLI"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            json.dumps({"test_name": "test_cli", "stdout_excerpt": "err"}),
        ],
    )

    cc.main()
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["created"] is True
    assert out["issueId"] == "SA-CLI"
