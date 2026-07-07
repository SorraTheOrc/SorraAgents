"""Tests for audit timeout handling in Ralph.

When the audit pi stream stalls (times out), Ralph should:
1. Remove the existing audit comment (if any)
2. Add a timeout comment explaining the failure
3. Treat the attempt as failed and proceed to retry
"""

import json
import subprocess


from skill.ralph.scripts.ralph_loop import RalphError, RalphLoop


def _make_comment(comment_id: str, text: str, created_at: str = "2026-06-01T00:00:00Z") -> dict:
    return {
        "id": comment_id,
        "workItemId": "WI",
        "author": "ralph",
        "comment": text,
        "createdAt": created_at,
        "references": [],
    }


AUDIT_COMMENT_TEXT = """# AMPA Audit Result

Ready to close: No

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | Tests pass | unmet | Tests failing |
"""


class TestIsStallError:
    """Tests for _is_stall_error detection."""

    def test_detects_stall_error(self):
        loop = RalphLoop(stream=True)
        err = RalphError("pi stream stalled after 60s waiting for stdout to close")
        assert loop._is_stall_error(err) is True

    def test_detects_stall_with_stderr(self):
        loop = RalphLoop(stream=True)
        err = RalphError("pi stream stalled after 900s waiting for pi to exit: some error")
        assert loop._is_stall_error(err) is True

    def test_does_not_detect_other_errors(self):
        loop = RalphLoop(stream=True)
        err = RalphError("pi run failed: some error")
        assert loop._is_stall_error(err) is False

    def test_does_not_detect_generic_errors(self):
        loop = RalphLoop(stream=True)
        err = RalphError("Worklog command failed: something")
        assert loop._is_stall_error(err) is False

    def test_non_ralph_error(self):
        loop = RalphLoop(stream=True)
        err = ValueError("not a ralph error")
        assert loop._is_stall_error(err) is False


class TestWlCommentDelete:
    """Tests for _wl_comment_delete."""

    def test_deletes_comment(self):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')

        loop = RalphLoop(runner=runner)
        loop._wl_comment_delete("WI-C1")

        assert any("delete" in c and "WI-C1" in c for c in calls)

    def test_deletes_comment_includes_json_flag(self):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')

        loop = RalphLoop(runner=runner)
        loop._wl_comment_delete("WI-C1")

        deleted_cmd = [c for c in calls if "delete" in c][0]
        assert "--json" in deleted_cmd


class TestLatestAuditCommentId:
    """Tests for _latest_audit_comment_id."""

    def test_returns_id_of_latest_audit_comment(self):
        audit_comment = _make_comment("WI-C2", AUDIT_COMMENT_TEXT, "2026-06-02T00:00:00Z")
        other_comment = _make_comment("WI-C1", "some other comment", "2026-06-01T00:00:00Z")

        def runner(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                "comments": [other_comment, audit_comment],
            }))

        loop = RalphLoop(runner=runner)
        result = loop._latest_audit_comment_id("WI")
        assert result == "WI-C2"

    def test_returns_none_when_no_audit_comment(self):
        def runner(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                "comments": [
                    _make_comment("WI-C1", "some comment"),
                ],
            }))

        loop = RalphLoop(runner=runner)
        result = loop._latest_audit_comment_id("WI")
        assert result is None

    def test_returns_none_when_no_comments(self):
        def runner(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"comments": []}))

        loop = RalphLoop(runner=runner)
        result = loop._latest_audit_comment_id("WI")
        assert result is None


class TestHandleAuditTimeout:
    """Tests for _handle_audit_timeout."""

    def test_removes_old_audit_and_adds_timeout_comment(self):
        deleted = []
        added = []

        def runner(cmd):
            if "delete" in cmd:
                deleted.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')
            if "add" in cmd:
                added.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                "comments": [_make_comment("WI-C1", AUDIT_COMMENT_TEXT)],
            }))

        loop = RalphLoop(runner=runner, pi_stream_timeout=60.0)
        loop._handle_audit_timeout("WI")

        assert len(deleted) == 1
        assert len(added) == 1
        # Verify the added comment mentions the timeout
        added_comment = " ".join(added[0])
        assert "timed out" in added_comment.lower()
        assert "60" in added_comment

    def test_handles_no_existing_audit_gracefully(self):
        added = []

        def runner(cmd):
            if "add" in cmd:
                added.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"comments": []}))

        loop = RalphLoop(runner=runner, pi_stream_timeout=90.0)
        # Should not raise even with no existing audit comment
        loop._handle_audit_timeout("WI")

        assert len(added) == 1
        added_comment = " ".join(added[0])
        assert "90" in added_comment


class TestAuditTimeoutInRunSingleItem:
    """Tests that run_single_item handles audit timeouts correctly."""

    def test_audit_timeout_triggers_cleanup_and_returns_max_attempts(self):
        """When audit stalls, the old audit comment should be removed,
        a timeout comment added, and the attempt should fail."""
        calls = {"pi": 0, "delete": [], "add": []}

        def runner(cmd):
            if cmd and cmd[0] == "pi":
                calls["pi"] += 1
                if calls["pi"] == 1:
                    # implement - succeeds
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                        "type": "agent_end",
                        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}],
                    }))
                if calls["pi"] == 2:
                    # audit - simulates stall error
                    raise RalphError("pi stream stalled after 60s waiting for stdout to close")
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}],
                }))
            if cmd and cmd[:3] == ["wl", "audit-show", "WI"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "success": True,
                    "workItemId": "WI",
                    "audit": None,
                }))
            if cmd and cmd[:2] == ["wl", "show"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "workItem": {"id": "WI", "stage": "in_progress", "status": "open"},
                    "children": [],
                }))
            if cmd and cmd[:3] == ["wl", "comment", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "comments": [_make_comment("WI-C1", AUDIT_COMMENT_TEXT)],
                }))
            if cmd and cmd[:3] == ["wl", "comment", "delete"]:
                calls["delete"].append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')
            if cmd and cmd[:3] == ["wl", "comment", "add"]:
                calls["add"].append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')
            return subprocess.CompletedProcess(cmd, 0, stdout="")

        loop = RalphLoop(
            runner=runner,
            max_attempts=2,
            stream=False,
            pi_stream_timeout=60.0,
        )
        result = loop.run_single_item("WI")

        assert result["status"] == "max_attempts"
        assert len(calls["delete"]) == 1
        assert len(calls["add"]) == 1


class TestAuditTimeoutInMainRunLoop:
    """Tests that the main run loop handles audit timeouts correctly."""

    def test_audit_timeout_in_main_loop_removes_comment_and_retries(self):
        """When the main loop's audit stalls, it should remove the old audit
        comment, add a timeout comment, and retry."""
        calls = {"pi": 0, "delete": [], "add": [], "audit_show": [], "updates": []}
        stage = ["in_progress"]  # mutable list so runner can change it
        status = ["closed"]  # closed satisfies _scope_in_review

        def runner(cmd):
            if cmd and cmd[0] == "pi":
                calls["pi"] += 1
                # First implement call
                if calls["pi"] == 1:
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                        "type": "agent_end",
                        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}],
                    }))
                # First audit call - stall
                if calls["pi"] == 2:
                    raise RalphError("pi stream stalled after 60s waiting for stdout to close")
                # Retry implement call
                if calls["pi"] == 3:
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                        "type": "agent_end",
                        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}],
                    }))
                # Retry audit call - success
                if calls["pi"] == 4:
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                        "type": "agent_end",
                        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Ready to close: Yes"}]}],
                    }))
                # Fallback for any other pi calls
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Ready to close: Yes"}]}],
                }))
            if cmd and cmd[:3] == ["wl", "audit-show", "WI"]:
                calls["audit_show"].append(cmd)
                if len(calls["audit_show"]) >= 2:
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                        "success": True,
                        "workItemId": "WI",
                        "audit": {
                            "workItemId": "WI",
                            "readyToClose": True,
                            "auditedAt": "2026-06-05T12:00:00Z",
                            "summary": "All good",
                            "rawOutput": "Ready to close: Yes",
                            "author": "audit-agent",
                        },
                    }))
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "success": True,
                    "workItemId": "WI",
                    "audit": None,
                }))
            if cmd and cmd[:2] == ["wl", "show"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "workItem": {"id": "WI", "stage": stage[0], "status": status[0], "effort": "Small", "risk": "Low"},
                    "children": [],
                }))
            if cmd and cmd[:3] == ["wl", "comment", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "comments": [_make_comment("WI-C1", AUDIT_COMMENT_TEXT)],
                }))
            if cmd and cmd[:3] == ["wl", "comment", "delete"]:
                calls["delete"].append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')
            if cmd and cmd[:3] == ["wl", "comment", "add"]:
                calls["add"].append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')
            if cmd and cmd[:3] == ["wl", "update", "WI"]:
                calls["updates"].append(cmd)
                if "--stage" in cmd and "in_review" in cmd:
                    stage[0] = "in_review"
                    status[0] = "closed"
                return subprocess.CompletedProcess(cmd, 0, stdout='{"success":true}')
            return subprocess.CompletedProcess(cmd, 0, stdout="")

        loop = RalphLoop(
            runner=runner,
            max_attempts=3,
            stream=False,
            pi_stream_timeout=60.0,
        )
        result = loop.run("WI")

        assert result["status"] == "success"
        assert len(calls["delete"]) == 1
        assert len(calls["add"]) == 1
