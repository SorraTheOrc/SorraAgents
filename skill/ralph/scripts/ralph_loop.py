#!/usr/bin/env python3
"""Ralph orchestration loop.

Implements an iterative implement->audit->remediate loop for a target work item.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

logger = logging.getLogger("ralph")


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class RalphError(RuntimeError):
    """Raised for orchestrator failures."""


@dataclass
class CriterionResult:
    text: str
    verdict: str
    evidence: str


@dataclass
class AuditParseResult:
    ready_to_close: bool
    criteria: list[CriterionResult]

    @property
    def unmet_or_partial(self) -> list[CriterionResult]:
        return [c for c in self.criteria if c.verdict in {"unmet", "partial"}]


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _run_json(runner: Runner, cmd: Sequence[str]) -> dict:
    proc = runner(cmd)
    if proc.returncode != 0:
        raise RalphError(f"Command failed ({' '.join(cmd)}): {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RalphError(f"Invalid JSON from {' '.join(cmd)}: {exc}") from exc
    if isinstance(data, dict) and data.get("success") is False:
        raise RalphError(f"Worklog command failed ({' '.join(cmd)}): {data.get('error', 'unknown error')}")
    return data


def parse_audit_report(report_text: str) -> AuditParseResult:
    lines = report_text.splitlines()
    ready = any(line.strip().lower().startswith("ready to close: yes") for line in lines)
    criteria: list[CriterionResult] = []
    for line in lines:
        striped = line.strip()
        if not striped.startswith("|"):
            continue
        parts = [p.strip() for p in striped.strip("|").split("|")]
        if len(parts) != 4:
            continue
        if parts[0] in {"#", "---"}:
            continue
        verdict = parts[2].lower()
        if verdict not in {"met", "unmet", "partial"}:
            continue
        criteria.append(CriterionResult(text=parts[1], verdict=verdict, evidence=parts[3]))
    return AuditParseResult(ready_to_close=ready, criteria=criteria)


def _build_remediation_prompt(findings: Iterable[CriterionResult]) -> str:
    items = list(findings)
    if not items:
        return ""
    lines = ["Use the previous audit findings to remediate these issues:"]
    for idx, finding in enumerate(items, start=1):
        lines.append(f"{idx}. [{finding.verdict}] {finding.text} ({finding.evidence})")
    return "\n".join(lines)


def _comment_hash(audit_text: str) -> str:
    return hashlib.sha256(audit_text.encode("utf-8")).hexdigest()[:16]


class RalphLoop:
    def __init__(
        self,
        runner: Runner | None = None,
        pi_bin: str = "pi",
        wl_bin: str = "wl",
        check_cmds: list[str] | None = None,
        max_attempts: int = 10,
        confirm_merge: bool = False,
        cancel_file: str | None = None,
    ):
        self.runner = runner or _default_runner
        self.pi_bin = pi_bin
        self.wl_bin = wl_bin
        self.max_attempts = max_attempts
        self.confirm_merge = confirm_merge
        self.cancel_file = cancel_file
        self.check_cmds = check_cmds or []

    def _wl_show(self, work_item_id: str, children: bool = False) -> dict:
        cmd = [self.wl_bin, "show", work_item_id, "--json"]
        if children:
            cmd.insert(3, "--children")
        return _run_json(self.runner, cmd)

    def _wl_comment_list(self, work_item_id: str) -> list[dict]:
        data = _run_json(self.runner, [self.wl_bin, "comment", "list", work_item_id, "--json"])
        return data.get("comments", [])

    def _wl_comment_add(self, work_item_id: str, comment: str) -> None:
        _run_json(
            self.runner,
            [
                self.wl_bin,
                "comment",
                "add",
                work_item_id,
                "--author",
                "ralph",
                "--comment",
                comment,
                "--json",
            ],
        )

    def _wl_update_audit(self, work_item_id: str, audit_text: str) -> None:
        _run_json(self.runner, [self.wl_bin, "update", work_item_id, "--audit-text", audit_text, "--json"])

    def _run_pi(self, prompt: str) -> str:
        proc = self.runner([self.pi_bin, "run", prompt])
        if proc.returncode != 0:
            raise RalphError(f"pi run failed: {proc.stderr.strip()}")
        return proc.stdout

    def _run_checks(self) -> None:
        for cmd in self.check_cmds:
            proc = self.runner(["bash", "-lc", cmd])
            if proc.returncode != 0:
                raise RalphError(f"Check failed ({cmd}): {proc.stderr.strip() or proc.stdout.strip()}")

    def _run_merge(self) -> None:
        if not self.confirm_merge:
            return
        for cmd in (
            ["git", "fetch", "origin", "main"],
            ["git", "merge", "--ff-only", "origin/main"],
            ["git", "push", "origin", "HEAD"],
        ):
            proc = self.runner(cmd)
            if proc.returncode != 0:
                raise RalphError(f"Merge step failed ({' '.join(cmd)}): {proc.stderr.strip()}")

    def _append_ampa_comment_once(self, work_item_id: str, audit_text: str) -> None:
        digest = _comment_hash(audit_text)
        marker = f"audit-hash:{digest}"
        for existing in self._wl_comment_list(work_item_id):
            if marker in (existing.get("comment") or ""):
                return
        comment = "\n".join(
            [
                "# AMPA Audit Result",
                f"{marker}",
                "",
                audit_text,
            ]
        )
        self._wl_comment_add(work_item_id, comment)

    def _scope_ids(self, target_id: str) -> list[str]:
        data = self._wl_show(target_id, children=True)
        scope = [target_id]
        scope.extend(child["id"] for child in data.get("children", []))
        return scope

    def _assert_precondition(self, target_id: str) -> None:
        item = self._wl_show(target_id).get("workItem", {})
        if item.get("stage") != "plan_complete":
            raise RalphError(
                f"Target {target_id} must be stage plan_complete before running ralph; "
                f"current stage is {item.get('stage', 'unknown')}."
            )

    def _scope_in_review(self, scope_ids: Iterable[str]) -> bool:
        allowed = {"in_review", "done", "completed", "closed"}
        for item_id in scope_ids:
            item = self._wl_show(item_id).get("workItem", {})
            stage = item.get("stage", "")
            status = item.get("status", "")
            if stage not in allowed and status not in {"closed", "completed"}:
                return False
        return True

    def run(self, target_id: str) -> dict:
        self._assert_precondition(target_id)
        scope_ids = self._scope_ids(target_id)
        remediation = ""

        logger.info("ralph.loop.start target=%s scope=%s max_attempts=%d", target_id, scope_ids, self.max_attempts)

        for attempt in range(1, self.max_attempts + 1):
            if self.cancel_file and os.path.exists(self.cancel_file):
                logger.info("ralph.loop.cancelled target=%s attempt=%d", target_id, attempt)
                return {"status": "cancelled", "attempt": attempt, "scope": scope_ids}

            logger.info("ralph.loop.attempt.start target=%s attempt=%d", target_id, attempt)

            prompt_parts = [
                f"implement {target_id}",
                f"Target scope includes direct children only: {', '.join(scope_ids[1:]) or '(none)'}.",
                "Continue until scope items are in_review, but do not merge.",
            ]
            if remediation:
                prompt_parts.append(remediation)
            self._run_pi("\n".join(prompt_parts))

            logger.info("ralph.loop.audit.start target=%s attempt=%d", target_id, attempt)
            audit_output = self._run_pi(f"/audit {target_id}")
            self._wl_update_audit(target_id, audit_output)
            self._append_ampa_comment_once(target_id, audit_output)
            audit = parse_audit_report(audit_output)

            logger.info(
                "ralph.loop.audit.complete target=%s attempt=%d ready=%s unmet=%d",
                target_id, attempt, audit.ready_to_close, len(audit.unmet_or_partial),
            )

            if audit.ready_to_close and self._scope_in_review(scope_ids):
                logger.info("ralph.loop.checks.start target=%s", target_id)
                self._run_checks()
                logger.info("ralph.loop.merge target=%s confirm=%s", target_id, self.confirm_merge)
                self._run_merge()
                return {
                    "status": "success",
                    "attempt": attempt,
                    "scope": scope_ids,
                    "merge_offered": True,
                    "merge_executed": self.confirm_merge,
                }

            remediation = _build_remediation_prompt(audit.unmet_or_partial)
            logger.info("ralph.loop.remediate target=%s attempt=%d unmet_count=%d", target_id, attempt, len(audit.unmet_or_partial))

        logger.warning("ralph.loop.max_attempts target=%s", target_id)
        return {"status": "max_attempts", "attempt": self.max_attempts, "scope": scope_ids}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Ralph implement→audit orchestration loop")
    parser.add_argument("work_item_id", help="Target Worklog item id")
    parser.add_argument("--max-attempts", type=int, default=10)
    parser.add_argument("--check-cmd", action="append", default=[], help="Build/test command to run on success")
    parser.add_argument("--confirm-merge", action="store_true", help="Execute merge/push steps after successful audit")
    parser.add_argument("--cancel-file", default=None, help="Path checked each attempt; if present, stop loop")
    parser.add_argument("--pi-bin", default="pi")
    parser.add_argument("--wl-bin", default="wl")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    loop = RalphLoop(
        pi_bin=args.pi_bin,
        wl_bin=args.wl_bin,
        check_cmds=args.check_cmd,
        max_attempts=args.max_attempts,
        confirm_merge=args.confirm_merge,
        cancel_file=args.cancel_file,
    )
    try:
        result = loop.run(args.work_item_id)
    except RalphError as exc:
        print(f"ralph: {exc}")
        return 2

    print(json.dumps(result, indent=2))
    if result.get("status") == "success":
        return 0
    if result.get("status") == "cancelled":
        return 3
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
