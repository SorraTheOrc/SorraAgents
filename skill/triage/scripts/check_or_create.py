#!/usr/bin/env python3
"""Lightweight implementation stub for check_or_create_critical_issue.

This script is intentionally small and follows repository conventions: it shells
out to the `wl` CLI (via subprocess) and implements conservative matching
heuristics. It is a deterministic helper used by the triage skill; tests should
mock subprocess/wl calls.
"""

import json
import shlex
import subprocess
import sys
from typing import Dict, Optional


def run_wl(args):
    cmd = ["wl"] + args
    try:
        out = subprocess.check_output(cmd, encoding="utf-8")
        return out
    except Exception:
        return None


def list_critical_issues():
    out = run_wl(["list", "--priority", "critical", "--tags", "test-failure", "--json"])
    if not out:
        return []
    try:
        return json.loads(out)
    except Exception:
        return []


def create_issue(title: str, body: str):
    # Use wl create --title "..." --description "..." --priority critical --tags "test-failure"
    args = [
        "create",
        "--title",
        title,
        "--description",
        body,
        "--priority",
        "critical",
        "--tags",
        "test-failure",
        "--json",
    ]
    out = run_wl(args)
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "expected JSON argument"}))
        sys.exit(2)
    try:
        payload = json.loads(sys.argv[1])
    except Exception:
        print(json.dumps({"error": "invalid JSON"}))
        sys.exit(2)

    test_name = payload.get("test_name") or payload.get("failure_signature", {}).get(
        "test_name"
    )
    stdout_excerpt = payload.get("stdout_excerpt") or payload.get(
        "failure_signature", {}
    ).get("stdout_excerpt", "")
    stack = payload.get("stack_trace") or payload.get("failure_signature", {}).get(
        "stack_trace", ""
    )
    commit = payload.get("commit_hash") or payload.get("failure_signature", {}).get(
        "commit_hash"
    )

    # Conservative matching: any incomplete (open|in_progress) issue with tag test-failure
    # whose title or body contains the test name is considered a match.
    candidates = list_critical_issues()
    match = None
    for c in candidates:
        title = c.get("title", "")
        body = c.get("description", "") or c.get("body", "") or ""
        status = c.get("status") or (c.get("workItem") or {}).get("status")
        if status and status.lower() not in ("open", "in_progress"):
            continue
        if test_name and (test_name in title or test_name in body):
            match = c
            break

    if match:
        # Attach a comment summarising new evidence
        issue_id = match.get("id") or (match.get("workItem") or {}).get("id")
        comment = f"Additional evidence: stdout excerpt:\n``\n{stdout_excerpt}\n```\n``\n{stack}\n```"
        run_wl(["comment", "add", issue_id, "--body", comment])
        result = {
            "issueId": issue_id,
            "created": False,
            "matchedId": issue_id,
            "reason": "matched_existing",
        }
        print(json.dumps(result))
        return

    # No match: create a new issue using minimal template
    title = f"[test-failure] {test_name} — failing test"
    body_lines = [
        f"Test name: {test_name}",
        "",
        "Failing output:",
        "",
        stdout_excerpt,
        "",
        "Stack trace:",
        "",
        stack,
    ]
    if commit:
        body_lines += ["", f"Failing commit: {commit}"]
    body = "\n".join(body_lines)

    created = create_issue(title, body)
    if not created:
        print(json.dumps({"error": "failed to create issue"}))
        sys.exit(2)

    # Normalize created result: wl create may return the full work item JSON
    new_id = None
    if isinstance(created, dict):
        new_id = created.get("id") or (created.get("workItem") or {}).get("id")

    print(json.dumps({"issueId": new_id, "created": True, "reason": "created_new"}))


if __name__ == "__main__":
    main()
