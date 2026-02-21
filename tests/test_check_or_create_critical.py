import json
import sys

import skill.triage.scripts.check_or_create as cc


def test_match_existing(monkeypatch, capsys):
    """If an incomplete test-failure issue exists matching the test name, it is returned and enhanced."""

    def fake_run_wl(args):
        # list call
        if args and args[0] == "list":
            return json.dumps(
                [
                    {
                        "id": "SA-EX",
                        "title": "[test-failure] test_foo — failing",
                        "description": "Test name: test_foo",
                        "status": "open",
                    }
                ]
            )
        # comment add
        if args and args[0] == "comment":
            return "{}"
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    sys_argv = sys.argv
    try:
        sys.argv = [
            "prog",
            json.dumps({"test_name": "test_foo", "stdout_excerpt": "fail"}),
        ]
        cc.main()
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert out["created"] is False
        assert out["matchedId"] == "SA-EX"
        assert out["reason"] == "matched_existing"
    finally:
        sys.argv = sys_argv


def test_create_new_issue_success(monkeypatch, capsys):
    """When no matching issue exists the script creates a new critical work item."""

    def fake_run_wl(args):
        if args and args[0] == "list":
            return json.dumps([])
        if args and args[0] == "create":
            # emulate wl create returning a work item
            return json.dumps({"id": "SA-NEW"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    sys_argv = sys.argv
    try:
        sys.argv = [
            "prog",
            json.dumps({"test_name": "test_bar", "stdout_excerpt": "err"}),
        ]
        cc.main()
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert out["created"] is True
        assert out["issueId"] == "SA-NEW"
        assert out["reason"] == "created_new"
    finally:
        sys.argv = sys_argv


def test_create_failure_no_wl(monkeypatch, capsys):
    """If WL create fails, the script exits with error status."""

    def fake_run_wl(args):
        # simulate WL unavailable or failing
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl)

    sys_argv = sys.argv
    try:
        sys.argv = [
            "prog",
            json.dumps({"test_name": "test_baz", "stdout_excerpt": "err"}),
        ]
        try:
            cc.main()
            assert False, "expected SystemExit"
        except SystemExit as e:
            # main uses exit code 2 for errors
            assert e.code == 2
            captured = capsys.readouterr()
            out = json.loads(captured.out)
            assert "error" in out
    finally:
        sys.argv = sys_argv


def test_idempotence(monkeypatch, capsys):
    """A second run for the same signature should match the previously-created issue."""

    # First run: no candidates, create returns SA-FOO
    created_called = {"called": False}

    def fake_run_wl_first(args):
        if args and args[0] == "list":
            return json.dumps([])
        if args and args[0] == "create":
            created_called["called"] = True
            return json.dumps({"id": "SA-FOO"})
        return None

    monkeypatch.setattr(cc, "run_wl", fake_run_wl_first)
    sys_argv = sys.argv
    try:
        sys.argv = [
            "prog",
            json.dumps({"test_name": "test_qux", "stdout_excerpt": "err"}),
        ]
        cc.main()
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert out["created"] is True
        assert out["issueId"] == "SA-FOO"
        assert created_called["called"]

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
                        }
                    ]
                )
            if args and args[0] == "comment":
                return "{}"
            return None

        monkeypatch.setattr(cc, "run_wl", fake_run_wl_second)
        cc.main()
        captured = capsys.readouterr()
        out2 = json.loads(captured.out)
        assert out2["created"] is False
        assert out2["matchedId"] == "SA-FOO"
    finally:
        sys.argv = sys_argv
